# Icecream Desktop Agent (Mac)

Remote hands for your Mac mini. A tiny stdlib-only Python HTTP server that
exposes tool endpoints — shell exec, file read/write/list, screenshots, app
launching, background jobs — behind a bearer token. Icecream (the assistant)
reaches it directly over Tailscale: direct TCP, immediate execution, no
polling, no relay server.

Mac-only. Needs `python3` (Xcode Command Line Tools or Homebrew).

## Why this exists: hands for a VM-based coding agent

Muse (the app) runs its coding agent on its own Linux VM. That VM is
where the coding happens — but a Linux VM cannot directly touch your
Mac. It cannot test a Mac desktop app, it cannot test a web app *on
your Mac*, it cannot click anything in macOS, and it cannot run
Mac-only tooling. Everything it does stops at the edge of the VM.

IcecreamAgent closes that gap. It runs on the Mac itself (a launchd
service, started on login) and acts as the coding agent's hand on the
machine. The agent on the VM drives it remotely, and through it the
VM can do anything a person sitting at the Mac could do:

- **Run any command** — shell exec, foreground or background, with
  stdin, timeouts, and job control.
- **Drive any application's UI through its accessibility tree** —
  dump an app's AX tree, find an element by role and title, click it,
  type into it, set a field's value, send keys, wait for elements to
  appear.
- **Mouse and keyboard automation** — coordinate clicks and key
  chords with explicit modifier flags.
- **See the screen** — screenshots on demand (the caller crops to a
  window or region), so the remote agent can verify what actually
  rendered instead of trusting a log line.
- **Move files around** — read, write (chunked, any size), list,
  stat with hashes, and fetch URLs straight to disk.

So the full chain is: you on the Muse app, anywhere (phone, iPad,
traveling, half asleep) → the coding agent on its Linux VM →
IcecreamAgent on your Mac → real apps on real hardware. The VM does
the thinking and the building; the Mac does the touching and the
proving. A desktop app is only truly tested when it has been
installed, launched, and driven on the machine it ships for — and
that last part is what this agent exists for.

## How the connection works: Tailscale tunnel + bearer token

There is no relay server and no polling. The path is a private
Tailscale network (a tailnet) with WireGuard encryption end to end:

1. **Both sides join the same tailnet.** The Mac runs Tailscale and
   signs in. The VM joins as a client-only device: it makes outgoing
   connections and hosts nothing itself, so nothing on the VM is
   reachable from the tailnet.
2. **Ordinary networking cannot reach either side.** Tailnet
   addresses are not routable from the public internet, and the
   agent's port is never exposed to it (see the deployment warning
   under Security notes). The only way in is through the tailnet.
3. **The coding agent calls the Mac like any HTTP API.** It sends
   requests to `http://<mac-tailnet-ip>:8765` — the Mac's tailnet
   address, port 8765 — over the tunnel. TCP only; the agent answers
   immediately, no polling loop, no intermediary storing commands.
4. **Every API call is authenticated.** Each request carries
   `Authorization: Bearer <token>`, where the token is generated at
   install time, stored mode 600 in `~/.ice-agent/config.json`, and
   compared in constant time. Without it, every data endpoint
   refuses. The only unauthenticated surface is the `/` status page,
   which reveals nothing but "running" and the version.

In short: Tailscale provides the private road, the bearer token
provides the gate, and the HTTP API provides the hands. Lose the
road and nothing connects; lose the token and nothing answers.

## Install

```bash
cd /path/to/desktop-agent
./install.sh
```

The installer copies the agent to `~/.ice-agent`, generates a bearer token,
and registers a launchd agent (`com.icecream.desktop-agent`) so it starts on
login and restarts if it crashes. It prints your token at the end — paste it
into Icecream's secure vault when asked. The token lives in
`~/.ice-agent/config.json` (mode 600).

Then on the Mac:

1. Install Tailscale, sign in to your tailnet.
2. If macOS asks "Do you want to allow incoming network connections?" for
   python3, click Allow.
3. System Settings → Energy → never sleep (a sleeping Mac doesn't answer).
4. Optional, for screenshots: System Settings → Privacy & Security →
   Screen Recording → enable for the app that runs the agent.

## Build from source (no binary download)

There is no prebuilt binary to fetch — the repository itself is the
build input, and `install.sh` is the build script. Cloning the repo
and running it builds the app from source on your Mac:

```bash
git clone https://github.com/is-cream/icecream-desktop-agent.git
cd icecream-desktop-agent
./install.sh
```

What the script does, in order:

1. Checks it is on macOS with `python3` available (`xcode-select
   --install` if it is missing) and installs pyobjc if the
   accessibility endpoints need it.
2. Builds `IcecreamAgent.app` at `~/.ice-agent/IcecreamAgent.app`
   from the `desktop-agent.py` in this repo — the bundle's executable
   is a copy of your real Python interpreter, and the script inside
   the bundle is byte-identical to the repo's file. It ad-hoc signs
   the bundle (copying the interpreter breaks its original signature).
3. Boots the freshly staged bundle on a side port and runs a
   self-test against it. Only if that passes does it swap the new
   bundle into place — a bad build can never take a working agent down.
4. Generates the bearer token on first install only (an existing
   token is kept on rebuilds) and registers the launchd service
   `com.icecream.desktop-agent`, so the agent starts on login.

Verify the build with the token the installer prints:

```bash
TOKEN="$(python3 -c "import json; print(json.load(open('$HOME/.ice-agent/config.json'))['token'])")"
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/health
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/selfcheck
```

To rebuild after pulling new source, just run `./install.sh` again —
same steps, token preserved, swap only after the self-test passes.

One consequence of building from source: macOS ties Accessibility
and Screen Recording grants to the bundle's signing identity, so a
freshly built (re-signed) bundle is a new identity. After any rebuild
you must re-grant **Icecream Agent** in System Settings → Privacy &
Security → Accessibility and → Screen Recording by hand — no script
can do that step for you. See "App bundle identity" below.

## API

Base URL: `http://<mac-tailnet-ip>:8765`
Every API request needs `Authorization: Bearer <token>`. All bodies and
responses are JSON.

**Status page:** opening `http://localhost:8765/` on the Mac itself in a
browser shows a small human status page ("the agent is running"), which
proves liveness at a glance. Paste the bearer token into the page and it
renders the agent's live self-check. The API itself stays token-only —
the page is just a view, served without auth because it contains no data.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Liveness: version, platform, hostname |
| POST | `/api/exec` | Run a command. `{"argv": [...], "cwd": "...", "timeout": 600, "background": false, "env": {...}}` or `{"command": "...", "shell": true, ...}`. Optional `"stdin": "..."` is fed to the process (prompts, interactive tools). Foreground waits up to `timeout` (max 3600s); `background: true` returns a `job_id` immediately |
| GET | `/api/jobs/<id>` | Poll a background job: running, exit_code, stdout, stderr |
| POST | `/api/jobs/<id>/kill` | SIGTERM (escalates to SIGKILL) a job |
| POST | `/api/read` | `{"path": "..."}` → content as utf8 text, or base64 if binary. Add `"offset"`/`"max_bytes"` for a ranged base64 slice of a large file (`size`, `eof` included) |
| POST | `/api/write` | `{"path": "...", "content": "...", "encoding": "utf8"|"base64"}` (creates parent dirs). `"append": true` appends — send a large file as sequential base64 chunks (first without, rest with `append`) to install files of any size |
| POST | `/api/stat` | `{"path": "..."}` → `exists`, `size`, `mtime`, `is_dir`, and a streamed `sha256` — verify a large upload without reading it back |
| POST | `/api/fetch` | `{"url": "...", "path": "...", "sha256": "..."(optional)}` — the agent downloads a URL straight to disk itself (streamed); a hash mismatch deletes the file and errors |
| POST | `/api/list` | `{"path": "..."}` → directory entries |
| POST | `/api/screenshot` | macOS screenshot → `{"png_base64": ..., "width": ..., "height": ...}` |
| POST | `/api/open` | `{"target": "/path/to/App.app"}` — macOS `open` |
| POST | `/api/launch` | `{"argv": [...], "cwd": "..."}` — detached process, returns pid + log path |
| GET | `/api/ax/status` | pyobjc present? Accessibility permission granted? |
| POST | `/api/ax/tree` | `{"app": "Name"}` or `{"pid": n}` (+ `max_depth`, `max_nodes`) — accessibility tree as JSON |
| POST | `/api/ax/click` | `{"app": "...", "role": "AXButton", "title": "OK"}` → AXPress; or `{"x": n, "y": n}` → mouse click. Both accept `"modifiers": ["cmd", ...]` (v0.6.3) |
| POST | `/api/ax/type` | `{"app": "...", "text": "hello"}` — activate + type |
| POST | `/api/ax/key` | `{"app": "...", "key": "return"}` — return/tab/escape/space/delete/up/down/left/right. Add `"modifiers": ["cmd", "shift", "option", "control"]` for chords (v0.6.3) |
| POST | `/api/ax/focus` | `{"app": "..."}` — bring to front |
| POST | `/api/ax/set-value` | `{"app": "...", "role": "...", "title": "...", "value": "..."}` — set a field's value directly via AXValue (no keystroke simulation) |
| POST | `/api/ax/wait` | `{"app": "...", "role": "...", "title": "...", "timeout": 30}` — poll until the element (or the app) appears; returns `found` true/false |
| GET | `/api/selfcheck` | prerequisite report: pyobjc, Accessibility, Screen Recording, Tailscale, screencapture — the agent tracks its own setup |
| GET | `/api/keepawake` | keep-awake option state |
| POST | `/api/keepawake` | `{enabled}` — while on, the Mac never sleeps or locks (see below) |

Example:

```bash
TOKEN="$(python3 -c "import json; print(json.load(open('$HOME/.ice-agent/config.json'))['token'])")"
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"argv": ["sw_vers"]}' http://127.0.0.1:8765/api/exec
```

## Prerequisites & self-check

The installer automates everything a script can: bearer token, launchd
service, pyobjc (for AX), and it verifies the agent answers before
printing a self-check summary. What macOS reserves for your hands:
Accessibility + Screen Recording for python3, never-sleep, Tailscale
install + sign-in, allowing python3's incoming connections.

The agent keeps track of its own prerequisites: `GET /api/selfcheck`
reports each one as ok/missing/unknown, an overall `ready` flag, and
`manual_steps` listing exactly what still needs a human.

## UI automation via the accessibility tree

The `/api/ax/*` endpoints drive real UI: find a button by role+title and
press it, dump an app's AX tree, type into the focused app, send keys.
Needs on the Mac:

1. `python3 -m pip install pyobjc` (install.sh tries this automatically).
2. System Settings → Privacy & Security → Accessibility → enable python3,
   then restart the agent. Check with `GET /api/ax/status`.

Workflow for testing an app: `/api/ax/tree` on the app → find the element
(role/title) → `/api/ax/click` it, or `/api/ax/type` text into it.
For a canvas-drawn grid (like a spreadsheet), the app itself must expose
AX elements — standard AppKit/SwiftUI controls and semantic DOM do this
automatically; custom canvases need explicit accessibility roles
(e.g. ARIA `grid`/`row`/`gridcell` in the web UI).

## Security notes

- Token auth on every endpoint, compared in constant time.
- Traffic over Tailscale is already WireGuard-encrypted end to end.
- The agent binds `0.0.0.0:8765`, but a home Mac behind NAT exposes nothing
  to the public internet; only your tailnet can reach it.

> **Deployment warning — read before you run this.** This agent has no TLS and no brute-force protection (no rate limiting, no lockout on failed auth). It is safe ONLY behind a private tunnel such as Tailscale (WireGuard-encrypted end to end). NEVER port-forward port 8765, never place the Mac in a DMZ, and never expose this port to any network beyond your tailnet. Anyone who can reach the port and obtains the token has full command execution, file access, screenshots, and UI control as your macOS user — treat the token as a full account password, not as an API key.
>
> **Token strength.** Keep the installer-generated token. The agent accepts any non-empty token from `~/.ice-agent/config.json` and enforces no minimum strength — replacing it with a short or guessable token removes the only authentication barrier.
>
> **What the token grants.** Every endpoint is token-gated, but behind that gate there are no further restrictions: shell exec, arbitrary file read/write, URL fetch to disk, screenshots, and accessibility control of any app. Token compromise equals full user-account compromise.
>
> **Status page.** The `/` page is unauthenticated by design and reveals only that the agent is running and its version; all data (hostname, self-check, tailnet IPs) requires the token.
- Anyone with the token can run arbitrary commands as your user. Guard the
  token like a password. Rotate it by deleting `~/.ice-agent/config.json`
  and re-running `install.sh`.
- `uninstall.sh` stops the service and deletes `~/.ice-agent` (token
  destroyed).

## App bundle identity (v0.6.0)

The agent installs as **IcecreamAgent.app** (in `~/.ice-agent/`) — a
Python app: the bundle's main executable is a copy of the real Python
interpreter, and the complete agent script lives inside the bundle at
`Contents/Resources/desktop-agent.py` (byte-identical to the shipped
`desktop-agent.py`). The running agent process is therefore the app
itself. A bare script has no TCC identity of its own — Screen Recording
grants made to interpreter files (python3.9, Python.app) never stick to
it (proven the hard way on 2026-10-22). Grant **Icecream Agent** in
System Settings > Privacy & Security > Accessibility and > Screen
Recording; it is a normal app entry, selectable in the standard picker.
The installer resolves the real interpreter path (never the
/usr/bin/python3 stub), ad-hoc signs the bundle (copying the
interpreter breaks its original signature), and points the launchd
plist at the app executable + the in-bundle script. Two v0.6.1
guards: the bundle carries a `Contents/Python3` symlink to the real
framework library (the CLT interpreter loads it via the relative
path `@executable_path/../Python3` — v0.6.0 died in dyld without
it), and the installer **boots the staged bundle on a side port and
aborts without touching the live agent if the self-test fails**.

## Input + resolution fixes (v0.6.3)

Three field fixes from v0.6.2 use:

- **Key holds**: synthetic key/mouse presses now hold ~50 ms between
  down and up (`KEY_HOLD_S` in the script) — instant pairs were too
  fast for some apps to register modifiers/transitions.
- **Explicit modifier flags**: `ax/key` and `ax/click` accept
  `"modifiers": ["cmd", "shift", "option", "control"]`, applied as
  CGEvent flags on the events themselves, never as synthesized
  modifier key presses. An unknown modifier name is a 400, not a
  silently unmodified key.
- **Fresh app resolution**: every AX action re-resolves its target.
  A pid is verified live and against the caller's name; a name match
  must be corroborated by the bundle's own declared name, terminated
  processes are skipped, and among equals the most recently launched
  wins (the survivor of a relaunch). Ambiguity fails loudly — the
  agent never silently acts on the wrong process.

## Keep-awake (v0.5.0)

An option on the status page (and via `/api/keepawake`), **default off**.
While enabled, the agent keeps the Mac awake and unlocked for as long as
it runs: it holds `caffeinate -dims` power assertions, declares user
activity every 4 minutes, and posts a mouse-move event to the current
cursor position (the pointer never moves) as an HID-level signal. The preference persists in
`~/.ice-agent/keepawake.json` and is re-applied when the agent restarts;
the caffeinate process is re-adopted by pidfile, never duplicated.

## Logs

`~/.ice-agent/agent.log` (launchd stdout/stderr). Detached launches log to
`~/.ice-agent/launched/`.
