#!/usr/bin/env python3
"""
Icecream Desktop Agent -- remote hands for a Mac.

A tiny stdlib-only HTTP server that exposes tool endpoints (shell exec,
file read/write/list, screenshots, app launching, background jobs) behind
a bearer token. Built to be reached over Tailscale from the Icecream
assistant: direct TCP, immediate execution, no polling, no relay server.

Mac-only in practice (launchd installer, `screencapture`, `open`), but the
core server is portable Python 3.7+ so it can be smoke-tested on Linux.

Config: ~/.ice-agent/config.json  {"token": "...", "bind": "0.0.0.0", "port": 8765}
Auth: every request needs  Authorization: Bearer <token>
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Optional: macOS accessibility-tree automation needs pyobjc.
# Everything else in this file is stdlib-only; AX endpoints return
# 501 with an install hint when pyobjc is missing.
try:
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
        AXValueGetValue,
        kAXValueCGPointType,
        kAXValueCGSizeType,
        AXIsProcessTrusted,
    )
    import Quartz
    from Cocoa import NSWorkspace
    HAVE_AX = True
    _AX_IMPORT_ERROR = None
except Exception as _ax_import_err:  # noqa: BLE001
    HAVE_AX = False
    _AX_IMPORT_ERROR = str(_ax_import_err)

VERSION = "0.6.3"

START_TIME = time.time()
MAX_BODY = 32 * 1024 * 1024          # max request body: 32 MB
STREAM_CAP = 4 * 1024 * 1024         # max captured stdout/stderr per job: 4 MB
JOB_TTL = 3600                       # keep finished jobs for 1 hour
MAX_JOBS = 100


# ---------------------------------------------------------------- config

def default_config_path():
    return os.path.join(os.path.expanduser("~"), ".ice-agent", "config.json")


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.stderr.write(
            "config not found: %s\nRun install.sh to generate one.\n" % path
        )
        sys.exit(2)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("bad config %s: %s\n" % (path, e))
        sys.exit(2)
    if not cfg.get("token"):
        sys.stderr.write("config %s has no token; run install.sh.\n" % path)
        sys.exit(2)
    cfg.setdefault("bind", "0.0.0.0")
    cfg.setdefault("port", 8765)
    return cfg


# ---------------------------------------------------------------- jobs

class Job:
    """A background (or foreground-waited) child process with capped output."""

    def __init__(self, job_id, proc, label, stdin_data=None):
        self.id = job_id
        self.proc = proc
        self.label = label
        self.started = time.time()
        self.end_time = None
        self.exit_code = None
        self.timed_out = False
        self.truncated = False
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._lock = threading.Lock()
        self._done = threading.Event()
        threading.Thread(target=self._pump, args=(proc.stdout, self._stdout),
                         daemon=True, name="pump-%s-out" % job_id).start()
        threading.Thread(target=self._pump, args=(proc.stderr, self._stderr),
                         daemon=True, name="pump-%s-err" % job_id).start()
        threading.Thread(target=self._wait, daemon=True,
                         name="wait-%s" % job_id).start()
        if stdin_data is not None and proc.stdin is not None:
            threading.Thread(target=self._feed_stdin, args=(stdin_data,),
                             daemon=True, name="stdin-%s" % job_id).start()

    def _feed_stdin(self, data):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    def _pump(self, pipe, buf):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                with self._lock:
                    room = STREAM_CAP - len(buf)
                    if room <= 0:
                        self.truncated = True
                        continue
                    buf.extend(chunk[:room])
                    if len(chunk) > room:
                        self.truncated = True
        finally:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001
                pass

    def _wait(self):
        try:
            self.exit_code = self.proc.wait()
        finally:
            self.end_time = time.time()
            self._done.release() if False else None
            self._done.set()

    def wait(self, timeout):
        return self._done.wait(timeout)

    def kill(self):
        """SIGTERM, escalate to SIGKILL after 5s. Returns immediately."""
        def _escalate():
            if self._done.wait(5):
                return
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=_escalate, daemon=True).start()

    def snapshot(self):
        with self._lock:
            out = bytes(self._stdout)
            err = bytes(self._stderr)
            truncated = self.truncated
        running = self.end_time is None
        return {
            "job_id": self.id,
            "label": self.label,
            "running": running,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "truncated": truncated,
            "started": self.started,
            "ended": self.end_time,
            "duration_s": round((self.end_time or time.time()) - self.started, 2),
            "stdout": out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace"),
        }


class JobRegistry:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def add(self, job):
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def _prune_locked(self):
        now = time.time()
        stale = [jid for jid, j in self._jobs.items()
                 if j.end_time is not None and now - j.end_time > JOB_TTL]
        for jid in stale:
            del self._jobs[jid]
        while len(self._jobs) > MAX_JOBS:
            oldest = min(self._jobs, key=lambda jid: self._jobs[jid].started)
            if self._jobs[oldest].end_time is None:
                break
            del self._jobs[oldest]


# ---------------------------------------------------------------------------
# keep-awake (user option, default OFF, persisted in ~/.ice-agent/keepawake.json)
#
# While enabled, the agent holds macOS power assertions via `caffeinate`
# (prevents idle sleep, display sleep, disk sleep and system sleep) and
# periodically declares user activity so the screen never locks. The
# caffeinate child runs in its own session, so it survives agent restarts;
# the agent re-adopts it by pidfile instead of spawning duplicates.
# ---------------------------------------------------------------------------

_KA_DIR = os.path.join(os.path.expanduser("~"), ".ice-agent")
_KA_STATE = os.path.join(_KA_DIR, "keepawake.json")
_KA_PIDFILE = os.path.join(_KA_DIR, "caffeinate.pid")
_ka_lock = threading.Lock()
_ka_touch_thread = None
_ka_touch_stop = threading.Event()


def _ka_enabled():
    try:
        with open(_KA_STATE) as fh:
            return bool(json.load(fh).get("enabled"))
    except Exception:
        return False


def _ka_save(enabled):
    try:
        os.makedirs(_KA_DIR, exist_ok=True)
        with open(_KA_STATE, "w") as fh:
            json.dump({"enabled": bool(enabled)}, fh)
    except Exception:
        pass


def _ka_pid():
    try:
        with open(_KA_PIDFILE) as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def _ka_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _ka_nudge_mouse():
    """Post a mouse-move event to the CURRENT position (user's design):
    the pointer does not move at all, but the posted HID event registers
    as user activity, so the idle/lock timer resets. No-op without
    Quartz (pyobjc)."""
    try:
        import Quartz
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        ev = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (loc.x, loc.y),
            Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:
        return False


def _ka_touch_loop():
    # Reset the idle/lock timer every 4 minutes while enabled: declare
    # user activity through the official assertion API and post a
    # zero-distance mouse-move event as a second, HID-level signal.
    while not _ka_touch_stop.is_set():
        try:
            subprocess.run(["caffeinate", "-u", "-t", "300"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except Exception:
            pass
        _ka_nudge_mouse()
        _ka_touch_stop.wait(240)


def _ka_start():
    """Idempotently ensure caffeinate is holding assertions."""
    global _ka_touch_thread
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return (False, "caffeinate unavailable (macOS only)")
    pid = _ka_pid()
    if _ka_alive(pid):
        return (True, "caffeinate running (pid %d)" % pid)
    proc = subprocess.Popen(["caffeinate", "-dims"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        os.makedirs(_KA_DIR, exist_ok=True)
        with open(_KA_PIDFILE, "w") as fh:
            fh.write(str(proc.pid))
    except Exception:
        pass
    if _ka_touch_thread is None or not _ka_touch_thread.is_alive():
        _ka_touch_stop.clear()
        _ka_touch_thread = threading.Thread(target=_ka_touch_loop,
                                            daemon=True)
        _ka_touch_thread.start()
    return (True, "caffeinate started (pid %d)" % proc.pid)


def _ka_stop():
    _ka_touch_stop.set()
    pid = _ka_pid()
    if _ka_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        os.unlink(_KA_PIDFILE)
    except Exception:
        pass


def _ka_status():
    enabled = _ka_enabled()
    pid = _ka_pid()
    active = bool(enabled and _ka_alive(pid))
    if not enabled:
        detail = "disabled (default)"
    elif active:
        detail = "holding power assertions (caffeinate pid %d)" % pid
    elif sys.platform != "darwin":
        detail = "enabled, but caffeinate is macOS-only"
    else:
        detail = "enabled, but caffeinate is not running"
    return {"enabled": enabled, "active": active, "detail": detail}


def _ka_apply():
    """Bring the world in line with the persisted preference."""
    with _ka_lock:
        if _ka_enabled():
            return _ka_start()
        _ka_stop()
        return (True, "off")


JOBS = JobRegistry()


def spawn_job(label, argv=None, command=None, shell=False, cwd=None,
              env=None, timeout=600, stdin_text=None):
    """Start a child process tracked as a Job. Returns the Job."""
    if argv is not None and command is not None:
        raise ValueError("pass argv or command, not both")
    if argv is None and command is None:
        raise ValueError("pass argv or command")
    if cwd is not None and not os.path.isdir(cwd):
        raise ValueError("cwd does not exist: %s" % cwd)
    merged_env = dict(os.environ)
    if env:
        if not isinstance(env, dict):
            raise ValueError("env must be an object")
        for k, v in env.items():
            merged_env[str(k)] = str(v)
    stdin_data = None
    if stdin_text is not None:
        if not isinstance(stdin_text, str):
            raise ValueError("stdin must be a string")
        stdin_data = stdin_text.encode("utf-8")
    try:
        if argv is not None:
            proc = subprocess.Popen(
                [str(a) for a in argv],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_data is not None else None,
                cwd=cwd, env=merged_env, start_new_session=True)
        else:
            proc = subprocess.Popen(
                str(command), shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_data is not None else None,
                cwd=cwd, env=merged_env, start_new_session=True,
                executable="/bin/bash" if os.path.exists("/bin/bash") else None)
    except FileNotFoundError as e:
        raise ValueError("executable not found: %s" % e)
    except OSError as e:
        raise ValueError("could not start process: %s" % e)
    job = Job(secrets.token_hex(8), proc, label, stdin_data=stdin_data)
    JOBS.add(job)
    return job


# ---------------------------------------------------------------- status page

STATUS_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Icecream Desktop Agent</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 2rem auto;
         max-width: 640px; padding: 0 1rem; color: #1c1c1e; }
  h1 { font-size: 1.4rem; } .ok { color: #0a7d2c; font-weight: 600; }
  .miss { color: #b3261e; font-weight: 600; } .unk { color: #8a6d00; font-weight: 600; }
  input { width: 100%; padding: .5rem; font-size: 1rem; box-sizing: border-box; }
  button { margin-top: .6rem; padding: .5rem 1.2rem; font-size: 1rem; }
  table { border-collapse: collapse; margin-top: 1rem; width: 100%; }
  td, th { border-bottom: 1px solid #ddd; padding: .4rem .3rem; text-align: left; }
  .small { color: #555; font-size: .85rem; }
</style></head><body>
<h1>Icecream Desktop Agent <span class="small">v__VERSION__</span></h1>
<p class="ok">The agent is running on this machine.</p>
<p>This page is only a status view. The agent itself is an API for Icecream:
paste the bearer token (printed by install.sh, stored in
<code>~/.ice-agent/config.json</code>) to see its live self-check.
The token is used only by this page, in this browser, and is not saved.</p>
<input id="tok" type="password" placeholder="Bearer token" autocomplete="off">
<button onclick="check()">Check status</button>
<p style="margin-top:1.2rem"><label>
<input id="ka" type="checkbox" style="width:auto" onchange="setKa()">
<b>Keep this Mac awake</b> &mdash; never sleep or lock while the agent runs
</label><br><span id="kaState" class="small">Enter the token and Check status to see its state. Default: off.</span></p>
<div id="out"></div>
<script>
async function call(path, tok) {
  const r = await fetch(path, {headers: {"Authorization": "Bearer " + tok}});
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
async function check() {
  const tok = document.getElementById("tok").value.trim();
  const out = document.getElementById("out");
  out.textContent = "Checking...";
  try {
    const h = await call("/api/health", tok);
    const s = await call("/api/selfcheck", tok);
    let html = "<p>Host: <b>" + h.hostname + "</b> &middot; platform: " + h.platform +
               " &middot; uptime: " + Math.round(s.uptime_s) + "s &middot; ready: <b>" +
               s.ready + "</b></p><table><tr><th>Check</th><th>Status</th><th></th></tr>";
    for (const [name, c] of Object.entries(s.checks)) {
      const cls = c.status === "ok" ? "ok" : (c.status === "missing" ? "miss" : "unk");
      html += "<tr><td>" + name + "</td><td class='" + cls + "'>" + c.status +
              "</td><td class='small'>" + (c.status === "ok" ? "" : (c.detail || "")) + "</td></tr>";
    }
    out.innerHTML = html + "</table>";
    if (s.keep_awake) {
      document.getElementById("ka").checked = !!s.keep_awake.enabled;
      document.getElementById("kaState").textContent =
        "Keep-awake: " + s.keep_awake.detail;
    }
  } catch (e) {
    out.innerHTML = "<p class='miss'>Could not read status: " + e.message +
                    " (wrong token?)</p>";
  }
}
async function setKa() {
  const tok = document.getElementById("tok").value.trim();
  const box = document.getElementById("ka");
  const st = document.getElementById("kaState");
  if (!tok) { st.textContent = "Paste the token above first, then toggle."; box.checked = false; return; }
  try {
    const r = await fetch("/api/keepawake", {method: "POST",
      headers: {"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
      body: JSON.stringify({enabled: box.checked})});
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    box.checked = !!s.enabled;
    st.textContent = "Keep-awake: " + s.detail;
  } catch (e) {
    st.textContent = "Could not change keep-awake: " + e.message;
  }
}
</script></body></html>
"""


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "IcecreamDesktopAgent/" + VERSION

    # -- plumbing -------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: N802
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                      fmt % args))

    def _send(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self):
        self._send(401, {"error": "unauthorized"})

    def _check_auth(self):
        want = "Bearer " + self.server.token
        got = self.headers.get("Authorization", "")
        # tolerate stray whitespace, but compare in constant time
        if not hmac.compare_digest(got.strip(), want):
            self._unauthorized()
            return False
        return True

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY:
            self._send(413, {"error": "body too large (max 32 MB)"})
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, {"error": "body is not valid JSON"})
            return None

    def _send_html(self, status, html):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing --------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            # Human status page: loading it proves the agent is alive.
            # Details are fetched from the token-protected API by the
            # page's own script once the user pastes the token into it.
            self._send_html(200, STATUS_PAGE.replace("__VERSION__", VERSION))
            return
        if not self._check_auth():
            return
        if path == "/api/ax/status":
            self._handle_ax_status()
            return
        if path == "/api/selfcheck":
            self._send(200, _do_selfcheck())
            return
        if path == "/api/health":
            self._send(200, {"ok": True, "version": VERSION,
                             "platform": sys.platform,
                             "hostname": socket.gethostname()})
            return
        if path == "/api/keepawake":
            self._send(200, _ka_status())
            return
        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            job = JOBS.get(job_id)
            if job is None:
                self._send(404, {"error": "unknown job"})
                return
            self._send(200, job.snapshot())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        if body is None:
            return
        try:
            if path == "/api/exec":
                self._handle_exec(body)
            elif path == "/api/keepawake":
                self._handle_keepawake(body)
            elif path == "/api/read":
                self._handle_read(body)
            elif path == "/api/write":
                self._handle_write(body)
            elif path == "/api/stat":
                self._handle_stat(body)
            elif path == "/api/fetch":
                self._handle_fetch(body)
            elif path == "/api/list":
                self._handle_list(body)
            elif path == "/api/screenshot":
                self._handle_screenshot(body)
            elif path == "/api/open":
                self._handle_open(body)
            elif path == "/api/launch":
                self._handle_launch(body)
            elif path == "/api/ax/tree":
                self._handle_ax_tree(body)
            elif path == "/api/ax/click":
                self._handle_ax_click(body)
            elif path == "/api/ax/type":
                self._handle_ax_type(body)
            elif path == "/api/ax/key":
                self._handle_ax_key(body)
            elif path == "/api/ax/focus":
                self._handle_ax_focus(body)
            elif path == "/api/ax/set-value":
                self._handle_ax_set_value(body)
            elif path == "/api/ax/wait":
                self._handle_ax_wait(body)
            elif path.startswith("/api/jobs/") and path.endswith("/kill"):
                job_id = path[len("/api/jobs/"):-len("/kill")]
                job = JOBS.get(job_id)
                if job is None:
                    self._send(404, {"error": "unknown job"})
                    return
                job.kill()
                self._send(200, {"ok": True, "job_id": job_id})
            else:
                self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.log_message("handler error: %r", e)
            self._send(500, {"error": "internal error: %s" % e})

    # -- tools ----------------------------------------------------
    def _handle_exec(self, body):
        argv = body.get("argv")
        command = body.get("command")
        shell = bool(body.get("shell", False))
        if command is not None and not shell and argv is None:
            self._send(400, {"error": "string command needs \"shell\": true; "
                                      "or pass \"argv\" list"})
            return
        timeout = body.get("timeout", 600)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            self._send(400, {"error": "timeout must be a number"})
            return
        timeout = min(max(timeout, 1), 3600)
        background = bool(body.get("background", False))
        label = body.get("label") or (command if isinstance(command, str)
                                      else " ".join(argv or []))[:120]
        job = spawn_job(label, argv=argv, command=command, shell=shell,
                        cwd=body.get("cwd"), env=body.get("env"),
                        timeout=timeout, stdin_text=body.get("stdin"))
        if background:
            self._send(202, {"job_id": job.id, "label": job.label,
                             "note": "poll GET /api/jobs/%s" % job.id})
            return
        finished = job.wait(timeout)
        if not finished:
            job.timed_out = True
            job.kill()
            job.wait(10)
        snap = job.snapshot()
        self._send(200, snap)

    def _handle_read(self, body):
        path = body.get("path")
        if not path:
            self._send(400, {"error": "path is required"})
            return
        if "offset" in body or "max_bytes" in body:
            # Ranged read for large files: returns one base64 slice.
            try:
                offset = max(int(body.get("offset", 0)), 0)
                max_bytes = int(body.get("max_bytes", 8 * 1024 * 1024))
            except (TypeError, ValueError):
                self._send(400, {"error": "offset/max_bytes must be integers"})
                return
            max_bytes = min(max(max_bytes, 1), 16 * 1024 * 1024)
            try:
                total = os.path.getsize(path)
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read(max_bytes)
            except FileNotFoundError:
                self._send(404, {"error": "no such file: %s" % path})
                return
            except IsADirectoryError:
                self._send(400, {"error": "is a directory: %s" % path})
                return
            except OSError as e:
                self._send(500, {"error": "cannot read: %s" % e})
                return
            self._send(200, {"path": path, "encoding": "base64",
                             "content": base64.b64encode(data).decode("ascii"),
                             "size": total, "offset": offset,
                             "bytes_returned": len(data),
                             "eof": offset + len(data) >= total})
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self._send(404, {"error": "no such file: %s" % path})
            return
        except IsADirectoryError:
            self._send(400, {"error": "is a directory: %s" % path})
            return
        except OSError as e:
            self._send(500, {"error": "cannot read: %s" % e})
            return
        try:
            text = data.decode("utf-8")
            self._send(200, {"path": path, "encoding": "utf8",
                             "content": text, "size": len(data)})
        except UnicodeDecodeError:
            self._send(200, {"path": path, "encoding": "base64",
                             "content": base64.b64encode(data).decode("ascii"),
                             "size": len(data)})

    def _handle_write(self, body):
        path = body.get("path")
        content = body.get("content", "")
        encoding = body.get("encoding", "utf8")
        if not path:
            self._send(400, {"error": "path is required"})
            return
        try:
            if encoding == "base64":
                data = base64.b64decode(content)
            elif encoding == "utf8":
                data = content.encode("utf-8")
            else:
                self._send(400, {"error": "encoding must be utf8 or base64"})
                return
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": "bad content: %s" % e})
            return
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            with open(path, "ab" if body.get("append") else "wb") as f:
                f.write(data)
        except OSError as e:
            self._send(500, {"error": "cannot write: %s" % e})
            return
        self._send(200, {"ok": True, "path": path, "bytes": len(data)})

    def _handle_stat(self, body):
        path = body.get("path")
        if not path:
            self._send(400, {"error": "path is required"})
            return
        if not os.path.exists(path):
            self._send(200, {"path": path, "exists": False})
            return
        st = os.stat(path)
        info = {"path": path, "exists": True, "is_dir": os.path.isdir(path),
                "size": st.st_size, "mtime": st.st_mtime}
        if not info["is_dir"] and body.get("sha256", True) \
                and st.st_size <= 2 * 1024 * 1024 * 1024:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            info["sha256"] = h.hexdigest()
        self._send(200, info)

    def _handle_fetch(self, body):
        """Download a URL straight to a file on this machine (streamed,
        so installer-size files never pass through JSON)."""
        import urllib.request
        import urllib.error
        url = body.get("url")
        path = body.get("path")
        if not url or not path:
            self._send(400, {"error": "url and path are required"})
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            self._send(400, {"error": "url must be http(s)"})
            return
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url, headers={"User-Agent": "IcecreamDesktopAgent/" + VERSION})
        h = hashlib.sha256()
        n = 0
        try:
            with opener.open(req, timeout=120) as r, open(path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
        except Exception as e:  # noqa: BLE001
            try:
                os.unlink(path)
            except OSError:
                pass
            self._send(502, {"error": "fetch failed: %s" % e})
            return
        digest = h.hexdigest()
        want = body.get("sha256")
        if want and want.lower() != digest:
            try:
                os.unlink(path)
            except OSError:
                pass
            self._send(400, {"error": "sha256 mismatch",
                             "expected": want, "got": digest})
            return
        self._send(200, {"ok": True, "path": path, "bytes": n,
                         "sha256": digest})

    def _handle_list(self, body):
        path = body.get("path") or os.path.expanduser("~")
        try:
            names = sorted(os.listdir(path))
        except FileNotFoundError:
            self._send(404, {"error": "no such directory: %s" % path})
            return
        except OSError as e:
            self._send(500, {"error": "cannot list: %s" % e})
            return
        entries = []
        for name in names:
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                entries.append({"name": name,
                                "is_dir": os.path.isdir(full),
                                "size": st.st_size,
                                "mtime": st.st_mtime})
            except OSError:
                entries.append({"name": name, "is_dir": False,
                                "size": None, "mtime": None})
        self._send(200, {"path": path, "entries": entries})

    def _handle_screenshot(self, body):  # noqa: ARG002
        if shutil.which("screencapture") is None:
            self._send(500, {"error": "screencapture not found (not a Mac?)",
                             "hint": "this endpoint needs macOS"})
            return
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        try:
            proc = subprocess.run(
                ["screencapture", "-x", "-t", "png", tmp.name],
                capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            self._send(500, {"error": "screencapture timed out"})
            return
        finally:
            pass
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            os.unlink(tmp.name)
            self._send(500, {
                "error": "screencapture failed: %s" % (err or proc.returncode),
                "hint": ("grant Screen Recording permission: System Settings > "
                         "Privacy & Security > Screen Recording, then retry")})
            return
        try:
            with open(tmp.name, "rb") as f:
                data = f.read()
        finally:
            os.unlink(tmp.name)
        if not data:
            self._send(500, {
                "error": "screencapture produced an empty image",
                "hint": ("blank screenshots mean the Screen Recording "
                         "permission is missing: System Settings > Privacy & "
                         "Security > Screen Recording")})
            return
        w, h = _png_size(data)
        self._send(200, {"png_base64": base64.b64encode(data).decode("ascii"),
                         "width": w, "height": h, "bytes": len(data)})

    def _handle_open(self, body):
        target = body.get("target")
        if not target:
            self._send(400, {"error": "target is required"})
            return
        if shutil.which("open") is None:
            self._send(500, {"error": "`open` not found (not a Mac?)"})
            return
        proc = subprocess.run(["open", target], capture_output=True,
                              timeout=30)
        if proc.returncode != 0:
            self._send(500, {
                "error": "open failed: %s"
                         % proc.stderr.decode("utf-8", "replace").strip()})
            return
        self._send(200, {"ok": True, "target": target})

    def _handle_launch(self, body):
        argv = body.get("argv")
        if not argv or not isinstance(argv, list):
            self._send(400, {"error": "argv (list) is required"})
            return
        cwd = body.get("cwd")
        if cwd is not None and not os.path.isdir(cwd):
            self._send(400, {"error": "cwd does not exist: %s" % cwd})
            return
        logdir = os.path.join(os.path.expanduser("~"), ".ice-agent",
                              "launched")
        os.makedirs(logdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(logdir, "proc-%s-%d.out" % (stamp, os.getpid()))
        try:
            out = open(out_path, "wb")
            proc = subprocess.Popen(
                [str(a) for a in argv], stdout=out, stderr=subprocess.STDOUT,
                cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL)
        except (FileNotFoundError, OSError) as e:
            self._send(400, {"error": "cannot launch: %s" % e})
            return
        self._send(200, {"ok": True, "pid": proc.pid, "log": out_path})

    def _handle_keepawake(self, body):
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send(400, {"error": "enabled (bool) is required"})
            return
        _ka_save(enabled)
        _ka_apply()
        self._send(200, _ka_status())

    # -- AX tools -------------------------------------------------
    def _ax_guard(self):
        if not HAVE_AX:
            self._send(501, _ax_unsupported())
            return False
        return True

    def _handle_ax_status(self):
        info = {"pyobjc": HAVE_AX, "platform": sys.platform}
        if not HAVE_AX:
            info.update(_ax_unsupported())
        else:
            try:
                info["trusted"] = bool(AXIsProcessTrusted())
            except Exception:  # noqa: BLE001
                info["trusted"] = None
            if not info.get("trusted"):
                info["hint"] = ("grant Accessibility permission: System "
                                "Settings > Privacy & Security > "
                                "Accessibility > enable Icecream Agent, then "
                                "restart the agent")
        self._send(200, info)

    def _handle_ax_tree(self, body):
        if not self._ax_guard():
            return
        max_depth = min(int(body.get("max_depth", 8)), 20)
        max_nodes = min(int(body.get("max_nodes", MAX_AX_NODES)), 20000)
        el, pid, name = _ax_resolve(body)
        budget = [max_nodes]
        tree = _ax_node(el, max_depth, budget)
        self._send(200, {"app": name, "pid": pid,
                         "nodes": max_nodes - budget[0],
                         "truncated": budget[0] <= 0,
                         "tree": tree})

    def _handle_ax_click(self, body):
        if not self._ax_guard():
            return
        if "x" in body and "y" in body:
            _ax_click_xy(float(body["x"]), float(body["y"]),
                         modifiers=body.get("modifiers"))
            self._send(200, {"ok": True, "x": body["x"], "y": body["y"]})
            return
        el, pid, name = _ax_resolve(body)
        node, target = _ax_find(el, role=body.get("role"),
                               title=body.get("title"))
        if target is None:
            self._send(404, {"error": "no matching element",
                             "role": body.get("role"),
                             "title": body.get("title")})
            return
        err = AXUIElementPerformAction(target, "AXPress")
        if err:
            self._send(500, {"error": "AXPress failed (code %s)" % err,
                             "matched": node})
            return
        self._send(200, {"ok": True, "app": name, "matched": node})

    def _handle_ax_type(self, body):
        if not self._ax_guard():
            return
        text = body.get("text", "")
        if not isinstance(text, str) or not text:
            self._send(400, {"error": "text (string) is required"})
            return
        _, pid, name = _ax_resolve(body)
        _ax_activate(pid=pid)
        time.sleep(0.4)
        _ax_type_text(text)
        self._send(200, {"ok": True, "app": name, "chars": len(text)})

    def _handle_ax_key(self, body):
        if not self._ax_guard():
            return
        _, pid, name = _ax_resolve(body)
        _ax_activate(pid=pid)
        time.sleep(0.3)
        _ax_key(body.get("key", ""), modifiers=body.get("modifiers"))
        self._send(200, {"ok": True, "app": name, "key": body.get("key")})

    def _handle_ax_focus(self, body):
        if not self._ax_guard():
            return
        name = _ax_activate(pid=body.get("pid"), name=body.get("app"))
        self._send(200, {"ok": True, "app": name})

    def _handle_ax_set_value(self, body):
        """Set AXValue on the first element matching role/title — the
        reliable way to put text into a field (no keystroke simulation)."""
        if not self._ax_guard():
            return
        el, pid, name = _ax_resolve(body)
        node, target = _ax_find(el, role=body.get("role"),
                                title=body.get("title"))
        if target is None:
            self._send(404, {"error": "no matching element",
                             "role": body.get("role"),
                             "title": body.get("title")})
            return
        err = AXUIElementSetAttributeValue(target, "AXValue",
                                           body.get("value", ""))
        if err:
            self._send(500, {"error": "set AXValue failed (code %s)" % err,
                             "matched": node})
            return
        self._send(200, {"ok": True, "app": name, "matched": node})

    def _handle_ax_wait(self, body):
        """Poll the AX tree until an element matching role/title appears
        (or the app itself starts), up to timeout seconds. Returns
        found=false on timeout instead of an error — the caller decides."""
        if not self._ax_guard():
            return
        try:
            timeout = float(body.get("timeout", 30))
        except (TypeError, ValueError):
            self._send(400, {"error": "timeout must be a number"})
            return
        timeout = min(max(timeout, 0), 300)
        start = time.time()
        while True:
            try:
                el, pid, name = _ax_resolve(body)
                node, target = _ax_find(el, role=body.get("role"),
                                        title=body.get("title"))
                if target is not None:
                    self._send(200, {"ok": True, "found": True, "app": name,
                                     "matched": node,
                                     "waited_s": round(time.time() - start, 2)})
                    return
            except ValueError:
                pass  # app not running yet — keep waiting
            if time.time() - start >= timeout:
                self._send(200, {"ok": True, "found": False,
                                 "waited_s": round(time.time() - start, 2)})
                return
            time.sleep(0.5)


# ---------------------------------------------------------------- self-check
#
# The agent keeps track of its own prerequisites and reports them on
# GET /api/selfcheck: each check is ok/missing/unknown, required or
# optional, with a human-actionable detail string. install.sh prints
# this summary at the end of every install.

def _check_tailscale_ips():
    """Tailnet IPv4s (100.64.0.0/10) visible on local interfaces."""
    ips = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:  # noqa: BLE001
        return ips
    for m in re.finditer(r"inet (100\.\d+\.\d+\.\d+)", out):
        if m.group(1) not in ips:
            ips.append(m.group(1))
    return ips


def _do_selfcheck():
    checks = {}

    def put(name, status, required, detail=""):
        checks[name] = {"status": status, "required": required,
                        "detail": detail}

    put("python3", "ok", True, sys.version.split()[0])

    if HAVE_AX:
        put("pyobjc", "ok", True, "AX endpoints enabled")
    else:
        put("pyobjc", "missing", True,
            "run: python3 -m pip install pyobjc (%s)"
            % (_AX_IMPORT_ERROR or "import failed"))

    if HAVE_AX:
        try:
            trusted = bool(AXIsProcessTrusted())
        except Exception:  # noqa: BLE001
            trusted = None
        put("accessibility",
            "ok" if trusted else ("unknown" if trusted is None else "missing"),
            True,
            "System Settings > Privacy & Security > Accessibility > "
            "enable Icecream Agent, then restart the agent")
    else:
        put("accessibility", "unknown", True, "needs pyobjc first")

    if HAVE_AX:
        try:
            pre = Quartz.CGPreflightScreenCaptureAccess()
            put("screen_recording", "ok" if pre else "missing", False,
                "System Settings > Privacy & Security > Screen Recording > "
                "enable python3")
        except Exception:  # noqa: BLE001
            put("screen_recording", "unknown", False, "could not query")
    else:
        put("screen_recording", "unknown", False, "needs pyobjc to query")

    put("screencapture",
        "ok" if shutil.which("screencapture") else "missing", False,
        "macOS only; screenshots need it")

    ips = _check_tailscale_ips()
    put("tailscale", "ok" if ips else "missing", True,
        ("on tailnet: %s" % ", ".join(ips)) if ips else
        "install Tailscale for Mac and sign in: "
        "https://tailscale.com/download/mac")

    manual_steps = ["%s: %s" % (n, c["detail"])
                    for n, c in checks.items()
                    if c["required"] and c["status"] != "ok" and c["detail"]]
    return {
        "version": VERSION,
        "platform": sys.platform,
        "hostname": socket.gethostname(),
        "uptime_s": round(time.time() - START_TIME, 1),
        "ready": not manual_steps,
        "checks": checks,
        "manual_steps": manual_steps,
        "keep_awake": _ka_status(),
    }


# ---------------------------------------------------------------- AX (macOS UI automation)
#
# Drives the macOS accessibility tree: query any app's UI elements, click
# buttons/cells, type text, send keys. Needs TWO things on the Mac:
#   1. pyobjc installed  (python3 -m pip install pyobjc)
#   2. Accessibility permission for the python3 process:
#      System Settings > Privacy & Security > Accessibility > enable Icecream Agent
# Without pyobjc the endpoints answer 501 with an install hint.

AX_KEYCODES = {
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
}
MAX_AX_NODES = 3000

# (v0.6.3) Synthetic key/mouse presses HOLD this long between down and
# up. Instant down/up pairs raced some apps -- modifier state and
# transitions never registered. One constant: tune here only.
KEY_HOLD_S = 0.05


def _modifier_flags(modifiers):
    """Modifier names -> Quartz CGEvent flag mask (v0.6.3).

    Callers pass explicit modifier names ("cmd", "shift", "option",
    "control") and the agent sets the flags on the events themselves.
    Synthesizing separate modifier key presses was racy and could leave
    modifiers stuck. None/[] -> 0. An unknown name raises ValueError so
    a typo fails loudly instead of silently sending an unmodified key.
    """
    if not modifiers:
        return 0
    table = {
        "cmd": Quartz.kCGEventFlagMaskCommand,
        "command": Quartz.kCGEventFlagMaskCommand,
        "shift": Quartz.kCGEventFlagMaskShift,
        "option": Quartz.kCGEventFlagMaskAlternate,
        "alt": Quartz.kCGEventFlagMaskAlternate,
        "control": Quartz.kCGEventFlagMaskControl,
        "ctrl": Quartz.kCGEventFlagMaskControl,
    }
    flags = 0
    for m in modifiers:
        key = str(m).strip().lower()
        if key not in table:
            raise ValueError(
                "unknown modifier %r; try: cmd, shift, option, control"
                % (m,))
        flags |= table[key]
    return flags


def _ax_unsupported():
    return {"error": "AX automation needs pyobjc",
            "hint": "on the Mac run: python3 -m pip install pyobjc "
                    "(install.sh tries this automatically)"}


def _ax_attr(el, name):
    try:
        # pyobjc returns (error, value) -- error FIRST. v0.5.0 had this
        # unpacked backwards, which silently broke every AX attribute
        # read (empty trees, reversed(int) crash in ax/wait). Found live
        # on the Mac 2026-10-22 via a direct probe.
        err, val = AXUIElementCopyAttributeValue(el, name, None)
    except Exception:  # noqa: BLE001
        return None
    if err:
        return None
    return val


def _ax_cgpoint(axval):
    """AXValue -> (x, y) or None. Best-effort: never raises."""
    if axval is None or not HAVE_AX:
        return None
    try:
        pt = Quartz.CGPoint(0, 0)
        if AXValueGetValue(axval, kAXValueCGPointType, pt):
            return (float(pt.x), float(pt.y))
    except Exception:  # noqa: BLE001
        pass
    return None


def _ax_cgsize(axval):
    """AXValue -> (w, h) or None. Best-effort: never raises."""
    if axval is None or not HAVE_AX:
        return None
    try:
        sz = Quartz.CGSize(0, 0)
        if AXValueGetValue(axval, kAXValueCGSizeType, sz):
            return (float(sz.width), float(sz.height))
    except Exception:  # noqa: BLE001
        pass
    return None


def _ax_short(s, n=400):
    if isinstance(s, str) and len(s) > n:
        return s[:n] + "...<%d chars>" % len(s)
    return s


def _ax_bundle_name(app):
    """The app bundle's OWN declared name (CFBundleName), or None.

    Cross-check for name resolution (v0.6.3): a running process's
    localizedName can collide with -- or outlive -- the app the caller
    actually means; the bundle's declared identity cannot."""
    try:
        url = app.bundleURL()
        if url is None:
            return None
        from Cocoa import NSBundle
        bundle = NSBundle.bundleWithURL_(url)
        if bundle is None:
            return None
        name = bundle.objectForInfoDictionaryKey_("CFBundleName") \
            or bundle.objectForInfoDictionaryKey_("CFBundleDisplayName")
        return str(name) if name else None
    except Exception:  # noqa: BLE001
        return None


def _ax_bundle_id(app):
    try:
        return str(app.bundleIdentifier() or "") or None
    except Exception:  # noqa: BLE001
        return None


def _ax_launch_ts(app):
    try:
        d = app.launchDate()
        return d.timeIntervalSince1970() if d is not None else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _ax_alive(app):
    try:
        return not app.isTerminated()
    except Exception:  # noqa: BLE001
        return True


def _ax_pick_app(pid=None, name=None):
    """Resolve a running app FRESH on every call (v0.6.3).

    The old resolver took the first name match NSWorkspace returned,
    which after a relaunch could be the dead predecessor or a different
    app with a colliding name -- actions then silently landed on the
    wrong process. Now:
      - the running list is re-scanned and terminated processes are
        excluded, every call;
      - a pid, when given, wins -- but the process behind it is
        verified live, and if a name was also given it must match that
        process's localized name OR its bundle's declared name (a
        recycled pid belonging to another app fails loudly);
      - a name match must be corroborated by the bundle's own declared
        name whenever that is readable;
      - among verified equals, the most recently launched wins (the
        survivor of a relaunch);
      - anything ambiguous or mismatched raises ValueError. Never
        silently act on the wrong process.
    """
    ws = NSWorkspace.sharedWorkspace()
    apps = [a for a in ws.runningApplications() if _ax_alive(a)]
    if pid is not None:
        for a in apps:
            if a.processIdentifier() == int(pid):
                if name:
                    loc = (a.localizedName() or "")
                    bname = _ax_bundle_name(a)
                    if loc.lower() != name.lower() and \
                            (bname is None or bname.lower() != name.lower()):
                        raise ValueError(
                            "pid %s is %r (bundle %s), not %r -- refusing "
                            "to act on the wrong process"
                            % (pid, loc, _ax_bundle_id(a), name))
                return a
        raise ValueError("no live process with pid %s" % (pid,))
    if not name:
        raise ValueError('give "pid" or "app"')
    exact = [a for a in apps
             if (a.localizedName() or "").lower() == name.lower()]
    pool, substring = exact, False
    if not pool:
        pool = [a for a in apps
                if name.lower() in (a.localizedName() or "").lower()]
        substring = True
    if not pool:
        raise ValueError("no running app matching %r" % name)
    verified = []
    for a in pool:
        bname = _ax_bundle_name(a)
        if bname is None:
            verified.append(a)  # identity unreadable: cannot disprove
        elif substring:
            if name.lower() in bname.lower() or \
                    bname.lower() in name.lower():
                verified.append(a)
        elif bname.lower() == name.lower():
            verified.append(a)
    if not verified:
        raise ValueError(
            "apps named like %r are running but none has a matching "
            "bundle identity -- refusing to guess" % name)
    return max(verified, key=_ax_launch_ts)


def _ax_resolve(spec):
    """spec {"pid": n} or {"app": name} -> (ax_app_element, pid, name)."""
    if spec.get("pid") is None and not spec.get("app"):
        raise ValueError('give "pid" or "app"')
    app = _ax_pick_app(pid=spec.get("pid"), name=spec.get("app"))
    pid = app.processIdentifier()
    return AXUIElementCreateApplication(pid), pid, app.localizedName()


def _ax_node(el, depth, budget):
    """AX element -> JSON-able dict. Respects depth and node budget."""
    if budget[0] <= 0 or depth < 0:
        return None
    budget[0] -= 1
    node = {}
    role = _ax_attr(el, "AXRole")
    node["role"] = str(role) if role else None
    for key, attr in (("title", "AXTitle"), ("value", "AXValue"),
                      ("description", "AXDescription")):
        v = _ax_attr(el, attr)
        if v is None or v == "":
            continue
        node[key] = _ax_short(v if isinstance(v, str) else str(v))
    pos = _ax_cgpoint(_ax_attr(el, "AXPosition"))
    size = _ax_cgsize(_ax_attr(el, "AXSize"))
    if pos:
        node["x"], node["y"] = pos
    if size:
        node["w"], node["h"] = size
    if depth > 0:
        kids = []
        for child in _ax_attr(el, "AXChildren") or []:
            cn = _ax_node(child, depth - 1, budget)
            if cn is not None:
                kids.append(cn)
            if budget[0] <= 0:
                break
        if kids:
            node["children"] = kids
    return node


def _ax_find(el, role=None, title=None, depth=12):
    """Depth-first first match on role (exact) and title (substring)."""
    want_title = title.lower() if title else None
    stack = [(el, 0)]
    while stack:
        cur, d = stack.pop()
        if d <= depth:
            r = _ax_attr(cur, "AXRole")
            r = str(r) if r else ""
            hay = "%s %s" % (_ax_attr(cur, "AXTitle") or "",
                             _ax_attr(cur, "AXDescription") or "")
            if (not role or r == role) and \
               (not want_title or want_title in hay.lower()):
                if role or title:
                    return _ax_node(cur, 0, [10 ** 9]), cur
            for child in reversed(_ax_attr(cur, "AXChildren") or []):
                stack.append((child, d + 1))
    return None, None


def _ax_activate(pid=None, name=None):
    app = _ax_pick_app(pid=pid, name=name)
    app.activateWithOptions_(1)  # NSApplicationActivateIgnoringOtherApps
    return app.localizedName()


def _ax_click_xy(x, y, modifiers=None):
    flags = _modifier_flags(modifiers)
    for etype in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(None, etype, (x, y), 0)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        if etype == Quartz.kCGEventLeftMouseDown:
            time.sleep(KEY_HOLD_S)


def _ax_type_text(text):
    for i in range(0, len(text), 64):
        chunk = text[i:i + 64]
        down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(down, len(chunk), chunk)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        time.sleep(KEY_HOLD_S)
        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _ax_key(name, modifiers=None):
    code = AX_KEYCODES.get((name or "").lower())
    if code is None:
        raise ValueError("unknown key %r; try: %s"
                         % (name, ", ".join(sorted(AX_KEYCODES))))
    flags = _modifier_flags(modifiers)
    for is_down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        if is_down:
            time.sleep(KEY_HOLD_S)


def _png_size(data):
    """(width, height) from a PNG IHDR, or (None, None)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return (None, None)
    try:
        return struct.unpack(">II", data[16:24])
    except struct.error:
        return (None, None)


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token):
        self.token = token
        super().__init__(address, Handler)


def main():
    ap = argparse.ArgumentParser(description="Icecream Desktop Agent")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    bind = args.bind or cfg["bind"]
    port = args.port or int(cfg["port"])

    server = AgentServer((bind, port), cfg["token"])
    if _ka_enabled():
        # Restore the keep-awake preference across agent restarts.
        _ka_apply()
    sys.stderr.write(
        "icecream desktop agent %s listening on %s:%d\n"
        % (VERSION, bind, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
