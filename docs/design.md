# IcecreamAgent — Technical Design

IcecreamAgent is a small HTTP server that runs on a Mac and gives a remote
coding agent hands on that machine: run commands, move files, drive
applications through their accessibility trees, and take screenshots.
This document describes how it is built, how it is reached, and what it
deliberately does not protect against. It is written for the public
repository; it contains no private identifiers — addresses, hostnames,
and tokens below are placeholders only.

All implementation claims trace to `desktop-agent.py` (v0.6.3, a single
file), `install.sh`, `uninstall.sh`, and `README.md`. Where this document
says "the code", it means those files.

## 1. System overview: why this agent exists

### 1.1 The problem

Muse (the app) runs its coding agent on its own Linux VM. That VM is
where the coding happens — but a Linux VM cannot directly touch a Mac.
It cannot test a Mac desktop app, it cannot test a web app *on the Mac*,
it cannot click anything in macOS, and it cannot run Mac-only tooling.
Everything it does stops at the edge of the VM.

### 1.2 The chain

```
You (Muse app, anywhere: phone, iPad)
  -> coding agent on its Linux VM
    -> Tailscale tunnel (WireGuard, private tailnet)
      -> IcecreamAgent on the Mac (port 8765, bearer token)
        -> macOS: shell, files, accessibility tree, screenshots
```

The VM does the thinking and the building; the Mac does the touching
and the proving. A desktop app is only truly tested when it has been
installed, launched, and driven on the machine it ships for — that last
part is what this agent exists for. The same chain covers web apps
running on the Mac: the remote agent opens the app in the Mac's browser
and drives it there, instead of testing a lookalike environment
somewhere else.

### 1.3 Design principles

- **One file, stdlib-only core.** The server is a single Python file.
  Everything except the accessibility endpoints uses only the Python
  standard library, so there is almost nothing to install and almost
  nothing that can rot. The accessibility endpoints need one optional
  dependency (pyobjc); without it they answer `501` with an install
  hint instead of failing mysteriously.
- **Direct TCP, no relay, no polling.** The agent is a plain HTTP
  server. The caller sends a request and gets an answer. Nothing is
  queued through a third party, and the agent never phones home.
- **Fail loudly, never silently act on the wrong thing.** Unknown
  keys, unknown modifiers, ambiguous app matches, and mismatched
  process identities are errors, not guesses. (This is the v0.6.3
  resolution rule; see §2.6.)
- **The token is the only gate — and it is a full gate.** Behind the
  bearer token there are no further restrictions. That is stated
  plainly in §4, not hidden.

## 2. Component map

The server is `ThreadingHTTPServer` (one thread per connection,
`daemon_threads = True`) serving a single `Handler` class. Its
`server_version` string reports the agent version. The entry point
(`main()`) reads the config, applies `--bind`/`--port` CLI overrides,
restores the keep-awake preference if enabled, and serves forever.

### 2.1 HTTP layer and routing

- **Request limits.** A request body larger than 32 MB is rejected
  with `413`. JSON parse failures are `400`. Unknown routes are
  `404`. Handler errors are `500` with the message (only ever seen by
  an authenticated caller).
- **Routes.** `GET` serves the status page (`/`, `/index.html`),
  `/api/health`, `/api/ax/status`, `/api/selfcheck`, `/api/keepawake`,
  and `/api/jobs/<id>`. `POST` serves `/api/exec`, `/api/keepawake`,
  `/api/read`, `/api/write`, `/api/stat`, `/api/fetch`, `/api/list`,
  `/api/screenshot`, `/api/open`, `/api/launch`, `/api/ax/tree`,
  `/api/ax/click`, `/api/ax/type`, `/api/ax/key`, `/api/ax/focus`,
  `/api/ax/set-value`, `/api/ax/wait`, and `/api/jobs/<id>/kill`.
- **Logging.** The request log records a timestamp plus the request
  line and status. It never records headers, so the `Authorization`
  header never lands in `agent.log`.

### 2.2 Authentication

- **Token source.** The token comes only from the runtime config
  file, `~/.ice-agent/config.json` (`{"token": ..., "bind": "0.0.0.0",
  "port": 8765}`). `load_config` exits (status 2) when the file is
  missing, unparseable, or has an empty/missing token. There is no
  default, fallback, or placeholder token that would authenticate, so
  the agent cannot start unauthenticated.
- **Comparison.** Every `/api/*` route checks
  `Authorization: Bearer <token>` with `hmac.compare_digest`
  (constant time; stray surrounding whitespace is tolerated).
  Failures get `401 {"error": "unauthorized"}`.
- **The one unauthenticated surface.** `GET /` and `/index.html`
  serve a static status page without auth. It reveals only that the
  agent is running and its version. All data — hostname, self-check
  details, tailnet addresses — is fetched by the page's own script
  from token-protected endpoints after the user pastes the token into
  the page, and the token is used only in that browser, never saved.
- **Token strength is not enforced.** The agent accepts any non-empty
  token from the config file. Security therefore depends on keeping
  the installer-generated token (see §4).

### 2.3 Commands and background jobs

`POST /api/exec` takes either an `argv` list or a `command` string
with `"shell": true` (run under `/bin/bash` when present); passing
both is a `400`. Optional fields: `cwd` (must exist), `env` (merged
over the agent's environment, values stringified), `stdin` (fed to
the process on a writer thread), `label`, and `timeout` (clamped to
1–3600 seconds, default 600).

- **Foreground (default).** The handler waits up to `timeout` for the
  job, then returns its snapshot. On timeout the job is marked
  `timed_out`, killed, waited on briefly, and the snapshot is still
  returned.
- **Background (`"background": true`).** Returns `202` with a
  `job_id` immediately; the caller polls `GET /api/jobs/<id>`.
  `POST /api/jobs/<id>/kill` terminates the job.
- **Job mechanics.** Each job spawns pump threads for stdout and
  stderr, a waiter thread, and (when stdin is given) a feeder thread.
  Children start in their own session. Captured output is capped at
  4 MB per stream; beyond that the snapshot reports
  `truncated: true` and further output is dropped. A job's snapshot
  carries `job_id`, `label`, `running`, `exit_code`, `timed_out`,
  `truncated`, `started`, `ended`, `duration_s`, `stdout`, and
  `stderr` (decoded as UTF-8 with replacement).
- **Job lifecycle.** The registry prunes finished jobs older than
  1 hour and caps the registry at 100 jobs, evicting the oldest
  finished job first; running jobs are never evicted.
- **Kill semantics.** Kill sends SIGTERM and escalates to SIGKILL
  after 5 seconds if the process is still alive, on a daemon thread,
  so the kill call itself returns immediately.

### 2.4 Files

- **`POST /api/read`.** Reads a file whole: UTF-8 text when
  decodable, base64 otherwise. With `offset`/`max_bytes` it returns a
  single base64 slice (slice size clamped to 1–16 MB, default 8 MB)
  plus `size`, `offset`, `bytes_returned`, and `eof` — the ranged
  form is how large files are inspected without pulling them whole.
- **`POST /api/write`.** Writes UTF-8 or base64 content, creating
  parent directories. `"append": true` appends instead of
  overwriting — the large-file protocol is sequential base64 chunks:
  first chunk without `append`, the rest with it. The caller verifies
  with `/api/stat` (see below); that is how multi-hundred-megabyte
  files are installed reliably over this API.
- **`POST /api/stat`.** Returns `exists`, `is_dir`, `size`, and
  `mtime`, plus a streamed SHA-256 (1 MB chunks) for files up to
  2 GB unless the caller opts out. Hashing on the Mac side means a
  large upload is verified without reading it back over the API.
- **`POST /api/fetch`.** Downloads a URL straight to a file on the
  Mac, streamed in 1 MB chunks, so installer-size files never pass
  through JSON. Only `http(s)` URLs are accepted, no proxy is used,
  and the download is hashed as it lands. On any failure, or on a
  SHA-256 mismatch against the caller's `sha256` field, the partial
  file is deleted and an error is returned.
- **`POST /api/list`.** Lists a directory (default: the user's home),
  sorted, with per-entry `is_dir`, `size`, and `mtime`; entries whose
  stat fails report nulls rather than failing the whole listing.

### 2.5 Screenshots, open, launch

- **`POST /api/screenshot`.** Runs macOS `screencapture -x -t png`
  into a temporary file (30-second timeout), reads it back, deletes
  the temp file, and returns base64 PNG plus width/height parsed from
  the PNG header. Failure modes are explicit: a missing binary, a
  non-zero exit, and an empty image each produce a distinct error,
  and the empty-image case says so directly — blank screenshots mean
  the Screen Recording permission is missing, not that the screen is
  blank. The caller crops to a window or region; the agent itself
  captures the full screen.
- **`POST /api/open`.** Runs macOS `open` on the target (30-second
  timeout). This is how apps and files are opened by path.
- **`POST /api/launch`.** Starts a detached process (new session,
  stdin closed) with stdout/stderr redirected to a timestamped log
  under `~/.ice-agent/launched/`, returning its pid and log path.
  Unlike `/api/exec`, a launched process is not tracked as a job and
  is not killed when the request ends.

### 2.6 Accessibility subsystem (pyobjc)

The `/api/ax/*` endpoints drive real macOS UI through the
accessibility tree. They require pyobjc and the Accessibility
permission; without pyobjc every AX endpoint answers `501` with the
install hint, and `/api/ax/status` reports `pyobjc`, `platform`,
`trusted` (from `AXIsProcessTrusted`), and a grant hint.

- **Tree queries.** `/api/ax/tree` resolves an app (by `pid` or
  `app` name) and returns its AX tree as JSON: per node, `role`,
  `title`, `value`, `description` (strings truncated at 400
  characters), `x`/`y` position, `w`/`h` size, and children.
  Depth defaults to 8 (clamped to 20) and the node budget defaults to
  3000 (clamped to 20000); exhausting the budget sets
  `truncated: true`.
- **Element addressing.** Click, set-value, and wait address elements
  by `role` (exact match) and `title` (substring match over title
  plus description), found by depth-first search (depth 12). No
  match is a `404` naming the requested role/title.
- **Actions.** `ax/click` presses an element (`AXPress`) or clicks
  coordinates; `ax/type` activates the app, waits briefly, then types
  in 64-character chunks; `ax/key` activates the app, waits briefly,
  then sends one named key (return, tab, space, delete, escape,
  arrows); `ax/focus` brings an app forward; `ax/set-value` writes a
  field's value directly via `AXValue` (no keystroke simulation);
  `ax/wait` polls every 0.5 seconds until an element appears or the
  timeout expires (clamped to 0–300 seconds, default 30) and returns
  `found: false` on timeout instead of an error.
- **Fresh resolution with bundle-identity verification (v0.6.3).**
  Every AX action re-resolves its target from a fresh scan of running
  applications, skipping terminated processes. A pid, when given,
  wins — but the process behind it is verified live, and if a name
  was also given it must match that process's localized name *or*
  the bundle's own declared name (`CFBundleName`/`CFBundleDisplayName`
  read from the bundle). A name match must likewise be corroborated
  by the bundle's declared name whenever it is readable. Among
  verified equals, the most recently launched wins (the survivor of
  a relaunch). Anything ambiguous or mismatched raises an error:
  the agent never silently acts on the wrong process. This replaced
  an older resolver that took the first name match, which after a
  relaunch could be the dead predecessor or a different app with a
  colliding name.
- **Key holds (v0.6.3).** Synthetic key and mouse presses hold
  50 ms between down and up (`KEY_HOLD_S`). Instant down/up pairs
  raced some apps — modifier state and transitions never registered.
  One constant controls it.
- **Explicit modifier flags (v0.6.3).** `ax/key` and `ax/click`
  accept `"modifiers": ["cmd", "shift", "option", "control"]`
  (aliases `command`, `alt`, `ctrl` accepted). The flags are set on
  the generated events themselves via `CGEventSetFlags`, on both the
  down and the up event. Synthesizing separate modifier key presses
  was racy and could leave modifiers stuck; there are no synthesized
  modifier presses anywhere. An unknown modifier name is a `400`,
  not a silently unmodified key.

### 2.7 Keep-awake (default off)

An option on the status page and via `/api/keepawake`, persisted in
`~/.ice-agent/keepawake.json` and re-applied when the agent restarts.
While enabled, the agent holds `caffeinate -dims` power assertions
(preventing idle, display, disk, and system sleep) in a child that
runs in its own session, re-adopted by pidfile across restarts rather
than duplicated, and every 4 minutes declares user activity
(`caffeinate -u -t 300`) plus posts a zero-distance mouse-move event
at the current cursor position as an HID-level signal — the pointer
never moves, but the idle/lock timer resets. The status endpoint
reports `enabled`, `active`, and a human detail string. Disabling
SIGTERMs the caffeinate child and removes its pidfile.

### 2.8 Self-check and prerequisites

`GET /api/selfcheck` reports what the agent needs and what is still
missing, so setup failures are diagnosable without guessing. Each
check is `ok`/`missing`/`unknown`, flagged required or optional, with
a human-actionable detail string; `ready` is true only when no
required check is missing, and `manual_steps` lists exactly what
still needs a human. Checks: `python3` (required), `pyobjc`
(required), `accessibility` (required, via `AXIsProcessTrusted`),
`screen_recording` (optional, via screen-capture preflight),
`screencapture` (optional, binary presence), and `tailscale`
(required — tailnet addresses visible on local interfaces, matched
as CGNAT-range addresses). The response also carries the version,
platform, hostname, uptime, and keep-awake state. Note that
`hostname` and tailnet addresses are behind the token; only the
bare status page is public (see §2.2).

## 3. Install, uninstall, and bundle identity

### 3.1 Install flow

`install.sh` is Mac-only (it aborts on any other OS) and requires
`python3` (Xcode Command Line Tools). In order:

1. Copies `desktop-agent.py` to `~/.ice-agent/desktop-agent.py` and
   sets mode 700 on `~/.ice-agent`.
2. Generates the config *only if none exists*: a bearer token from
   `secrets.token_urlsafe(32)`, written to
   `~/.ice-agent/config.json` with mode 600, bind `0.0.0.0`, port
   8765. An existing config is kept untouched — reinstalling never
   rotates a working token.
3. Ensures pyobjc: if `ApplicationServices` does not import, it runs
   `ensurepip` and `pip install --user pyobjc`; if pip is
   unavailable, AX endpoints will report the install hint instead.
4. Builds the app bundle at a staging path (see §3.3), then
   **self-tests it before touching the live agent**: the staged
   bundle boots on a loopback-only side port with an ephemeral
   token, and the installer polls its health endpoint for up to 10
   seconds. On failure it aborts, deletes the staging bundle, and
   the existing agent keeps running untouched. On success it swaps
   the bundle in.
5. Writes the launchd plist (`com.icecream.desktop-agent`,
   `RunAtLoad` + `KeepAlive`, stdout/stderr to
   `~/.ice-agent/agent.log`), reloads it for the login session, and
   warns (only warns) if Tailscale.app is not installed.
6. Prints the bearer token to the installing user's own terminal
   (a documented local disclosure — the operator pastes it into
   their assistant's secure vault), waits up to 20 seconds for the
   agent to answer on loopback, and prints the self-check summary
   including any manual steps.

### 3.2 Uninstall flow

`uninstall.sh` unloads the launchd job, removes its plist, and
deletes `~/.ice-agent` entirely — which destroys the token. One
explicit non-goal, stated by the script itself: it does not remove
the device from the Tailscale admin console; that is a separate
manual step if the operator wants the device gone entirely.

### 3.3 Why the .app bundle exists: TCC identity stickiness

macOS privacy permissions (Accessibility, Screen Recording) are
granted to an *application identity*, not to a script. A bare Python
script has no identity of its own: grants made to interpreter files
do not stick to it, so every run would re-prompt or silently fail.
That is why the installer builds **IcecreamAgent.app**: the bundle's
main executable is a copy of the real Python interpreter (resolved
via its real path, never the system stub), and the complete agent
script lives inside the bundle at
`Contents/Resources/desktop-agent.py`, so the running agent process
*is* the app. Grants made to "Icecream Agent" in System Settings
therefore attach to something stable and selectable in the standard
picker. Two guards from field experience are baked in: the bundle
carries a `Contents/Python3` symlink to the real framework library
(the interpreter loads it via the relative path
`@executable_path/../Python3` — without it the copied interpreter
dies in the loader), and the bundle is ad-hoc re-signed after the
copy (copying the interpreter breaks its original signature), so the
identity is the bundle's own. The bundle sets `LSUIElement`, so it
has no Dock icon.

One consequence operators must understand: **a rebuilt, re-signed
bundle is a new TCC identity.** macOS does not carry Accessibility
or Screen Recording grants across a signature change, so after any
agent rebuild the grants must be re-made by hand in System Settings.
No script can grant them — that is the operating system's rule, and
it is the reason the self-check reports grants as manual steps.

## 4. Connection and security model

### 4.1 The tunnel

The agent is reached over a Tailscale tailnet: a private network
with WireGuard encryption end to end. Both the Mac and the remote
side join the same tailnet. The remote (VM) side joins as a
client-only device — it makes outgoing connections and hosts
nothing itself. There is no relay server and no polling: the caller
opens a TCP connection to the Mac's tailnet address on port 8765
and speaks plain HTTP to the agent, which answers immediately.
Ordinary networking cannot reach tailnet addresses, and the
agent's port is never exposed to the public internet.

### 4.2 The bind, and what it does and does not mean

The agent binds `0.0.0.0:8765` by default (configurable). That bind
is a listening choice, not an exposure decision: reachability is
determined by the network, and in the intended deployment the only
network that can reach the port is the tailnet. The agent itself
has no TLS and no brute-force protection (no rate limiting, no
lockout on failed auth). Its transport security therefore rests
entirely on the tunnel. Anyone who can reach the port has only one
barrier: the bearer token.

### 4.3 Threat model, stated plainly

- **Token compromise equals full macOS-user compromise.** Behind
  the token there are no further restrictions: shell exec, arbitrary
  file read/write, URL fetch to disk, screenshots, and accessibility
  control of any app. Treat the token as a full account password,
  not as an API key. Rotate it by deleting the config file and
  re-running the installer.
- **Token strength is the operator's job.** The agent accepts any
  non-empty token and enforces no minimum strength. Keep the
  installer-generated token; replacing it with a short or guessable
  one removes the only authentication barrier.
- **Never port-forward port 8765, never place the Mac in a DMZ, and
  never expose this port to any network beyond the tailnet.** A
  home router's NAT is not a guarantee — port-forwarding, DMZ,
  public LANs, and non-NAT networks all defeat it.
- **The status page is intentionally unauthenticated** and reveals
  only that the agent is running and its version. All data requires
  the token.
- **Error responses echo paths and exception text**, but only to
  the already-authenticated caller. The fetch endpoint downloads
  arbitrary URLs, but only for the token holder, who can already
  run arbitrary shell commands — it adds no privilege. The `Server`
  header discloses the agent version on every response, including
  `401`s; version disclosure is not sensitive.

## 5. Failure modes and operating notes

### 5.1 What the operator must do by hand

macOS reserves these for a human; no script can do them, and the
self-check lists them as manual steps until they are done:

- **Accessibility**: System Settings → Privacy & Security →
  Accessibility → enable Icecream Agent, then restart the agent.
  Without it, AX endpoints fail or return empty trees, and
  `/api/ax/status` reports `trusted: false`.
- **Screen Recording**: same panel → Screen Recording → enable
  Icecream Agent. Without it, screenshots come back empty, and the
  agent says so explicitly rather than returning a blank image.
- **Never sleep**: a sleeping Mac does not answer. Either set the
  system never to sleep, or enable the agent's keep-awake option
  (§2.7).
- **Tailscale**: install Tailscale for Mac and sign in to the
  tailnet; allow incoming connections for the agent when macOS asks.
- **Re-grant after every rebuild**: a re-signed bundle is a new
  TCC identity (§3.3). After installing a rebuilt agent, re-make
  the Accessibility and Screen Recording grants before expecting
  automation to work.

### 5.2 Failure modes and how each one shows up

| Failure | Symptom | What to do |
|---|---|---|
| Mac asleep | Connection times out; nothing answers | Wake the Mac; enable never-sleep or keep-awake |
| Agent not running | Connection refused on port 8765 | Check `~/.ice-agent/agent.log`; launchd `KeepAlive` restarts it, but a bad config exits at startup (status 2) and must be fixed by hand |
| Accessibility denied | AX endpoints fail or return empty trees; `ax/status` shows `trusted: false` | Grant Icecream Agent in Accessibility, restart the agent |
| Screen Recording denied | Screenshots empty, with the explicit permission hint | Grant Icecream Agent in Screen Recording |
| App relaunched / pid recycled | Old resolver would act on the wrong process | v0.6.3 resolution fails loudly instead — re-resolve by app name and retry |
| App not running yet | `ax/wait` returns `found: false` after its timeout; other AX calls raise | Start the app first, or use `ax/wait` to wait for it |
| pyobjc missing | All AX endpoints answer `501` with the install hint | Run the pip install the hint names (the installer tries this itself) |
| Command timeout | Foreground exec returns a snapshot with `timed_out: true` and the process killed | Use `"background": true` and poll the job, or raise `timeout` (max 3600 s) |
| Output flood | Snapshot reports `truncated: true` | Read output incrementally via job polling, or redirect to a file and use ranged reads |
| Body too large | `413` | Split the payload; the write endpoint's chunked-append protocol exists for this |
| Fetch hash mismatch | `400`, partial file deleted | Re-fetch; the stored bytes are never left half-written |
| Config missing or tokenless | Agent exits at startup (status 2) | Re-run `install.sh` to generate a config |
| Tunnel down | Tailnet address unreachable from the remote side | Check Tailscale sign-in on both sides; the agent itself cannot fix the tunnel |

### 5.3 Operating notes

- **Logs.** `~/.ice-agent/agent.log` (launchd stdout/stderr).
  Detached launches log under `~/.ice-agent/launched/`. The request
  log never contains credentials (§2.1).
- **No Dock icon.** The bundle sets `LSUIElement`; the agent runs
  without a Dock presence. Its status page on loopback is the human
  liveness check.
- **Core portability.** The server core is portable Python and can
  be smoke-tested on Linux, but screenshots, `open`, launchd,
  caffeinate, and the AX endpoints are macOS-only and fail
  explicitly elsewhere.
- **Uninstall is total for local state** (§3.2) except the
  Tailscale device record, which the operator removes in the
  Tailscale admin console if desired.

---

*Self-scan (redaction): scanned this file for real IPs, hostnames,
MagicDNS names, personal names, emails, tokens, usernames in paths,
and internal URLs — clean; only placeholders (`<mac-tailnet-ip>`,
`<token>`) and public product identifiers appear.*
