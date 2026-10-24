#!/bin/bash
# Icecream Desktop Agent -- Mac installer (v0.6.0: Python app bundle).
# Installs the agent as IcecreamAgent.app: the bundle's main executable
# IS the Python interpreter, and the complete agent script lives inside
# the bundle (Contents/Resources/desktop-agent.py), so the agent process
# carries the app's own macOS permission identity and grants stick.
# Config/token/state stay in ~/.ice-agent as before.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is Mac-only." >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. Install Xcode Command Line Tools first:" >&2
  echo "  xcode-select --install" >&2
  exit 1
}

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$HOME/.ice-agent"
PLIST="$HOME/Library/LaunchAgents/com.icecream.desktop-agent.plist"
LABEL="com.icecream.desktop-agent"
APP="$AGENT_DIR/IcecreamAgent.app"

mkdir -p "$AGENT_DIR"
# Keep a plain copy too: status tooling and humans look here, and the
# pre-bundle agent launched this path.
cp "$SRC_DIR/desktop-agent.py" "$AGENT_DIR/desktop-agent.py"
chmod 700 "$AGENT_DIR"

if [[ ! -f "$AGENT_DIR/config.json" ]]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  python3 - "$AGENT_DIR/config.json" "$TOKEN" <<'EOF'
import json, sys
path, token = sys.argv[1], sys.argv[2]
with open(path, "w") as f:
    json.dump({"token": token, "bind": "0.0.0.0", "port": 8765}, f, indent=2)
EOF
  chmod 600 "$AGENT_DIR/config.json"
  echo "Generated a new bearer token."
else
  echo "Keeping existing token in $AGENT_DIR/config.json"
fi

# Prerequisites the installer handles itself, BEFORE the agent starts
# so the running agent sees them.
if python3 -c "import ApplicationServices" 2>/dev/null; then
  echo "[OK] pyobjc present: accessibility-tree endpoints enabled."
else
  echo "Installing pyobjc for accessibility-tree automation..."
  python3 -m ensurepip --user >/dev/null 2>&1 || true
  if python3 -m pip install --user pyobjc >/dev/null 2>&1; then
    echo "[OK] pyobjc installed."
  else
    echo "[TODO] pip unavailable; AX endpoints will report how to enable."
  fi
fi

# --- Python app bundle --------------------------------------------------
# Resolve the REAL interpreter binary (never the /usr/bin/python3 stub).
PYREAL="$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))')"
echo "Interpreter: $PYREAL"
VER="$(grep -m1 'VERSION =' "$SRC_DIR/desktop-agent.py" | sed 's/.*"\(.*\)".*/\1/')"

# Build the bundle at a staging path first; it is swapped into place
# only after the self-test below passes, so a bad build can never take
# the working agent down.
APP_NEW="$AGENT_DIR/IcecreamAgent.app.new"
rm -rf "$APP_NEW"
mkdir -p "$APP_NEW/Contents/MacOS" "$APP_NEW/Contents/Resources"
# The app's main executable is Python itself; the full agent script is
# inside the bundle. The running process is therefore the app.
cp "$PYREAL" "$APP_NEW/Contents/MacOS/IcecreamAgent"
chmod +x "$APP_NEW/Contents/MacOS/IcecreamAgent"
cp "$SRC_DIR/desktop-agent.py" "$APP_NEW/Contents/Resources/desktop-agent.py"
# (v0.6.1) The CLT interpreter loads its framework library via the RELATIVE
# path @executable_path/../Python3 (v0.6.0 died on exactly this: dyld
# could not find it from the copied location). Give the bundle that
# relative neighbour as a symlink to the real framework library.
PYLIB="$(dirname "$(dirname "$PYREAL")")/Python3"
if [[ -f "$PYLIB" ]]; then
  ln -s "$PYLIB" "$APP_NEW/Contents/Python3"
fi
cat > "$APP_NEW/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>com.icecream.desktopagent</string>
  <key>CFBundleName</key>
  <string>Icecream Agent</string>
  <key>CFBundleExecutable</key>
  <string>IcecreamAgent</string>
  <key>CFBundleVersion</key>
  <string>$VER</string>
  <key>CFBundleShortVersionString</key>
  <string>$VER</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
EOF
# Copying the interpreter breaks its signature; re-sign the bundle
# ad-hoc so the identity is the bundle's own (id + this build).
if codesign --force --deep --sign - "$APP_NEW" >/dev/null 2>&1; then
  echo "[OK] IcecreamAgent.app built (Python app, ad-hoc signed)."
else
  echo "[WARN] codesign failed; app is unsigned (grants key on its path)."
fi

# --- self-guard: prove the staged bundle BOOTS before touching the
# live agent. v0.6.0 shipped a bundle whose interpreter died in dyld
# and took the working agent down; that failure mode is now impossible:
# any failure here aborts with the existing setup untouched.
echo "Self-test: booting the staged bundle on a side port..."
SELFTEST_OK=""
if "$APP_NEW/Contents/MacOS/IcecreamAgent" -c "import sys" 2>/dev/null; then
  TEST_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
  TEST_CFG="$AGENT_DIR/selftest-config.json"
  python3 - "$TEST_CFG" "$TEST_TOKEN" <<'EOF'
import json, sys
with open(sys.argv[1], "w") as f:
    json.dump({"token": sys.argv[2], "bind": "127.0.0.1", "port": 18799}, f)
EOF
  "$APP_NEW/Contents/MacOS/IcecreamAgent" \
    "$APP_NEW/Contents/Resources/desktop-agent.py" \
    --config "$TEST_CFG" --port 18799 \
    > "$AGENT_DIR/selftest.log" 2>&1 &
  TEST_PID=$!
  for _ in $(seq 1 10); do
    if curl -s -m 2 -H "Authorization: Bearer $TEST_TOKEN" \
        http://127.0.0.1:18799/api/health 2>/dev/null | grep -q '"ok": *true'; then
      SELFTEST_OK=1; break
    fi
    sleep 1
  done
  kill "$TEST_PID" 2>/dev/null || true
  rm -f "$TEST_CFG"
fi
if [[ "$SELFTEST_OK" != "1" ]]; then
  echo "[ABORT] staged bundle failed its boot self-test (see $AGENT_DIR/selftest.log)." >&2
  echo "        Your existing agent was NOT touched and is still running." >&2
  rm -rf "$APP_NEW"
  exit 1
fi
echo "[OK] self-test passed; swapping the bundle in."
rm -rf "$APP"
mv "$APP_NEW" "$APP"
LAUNCH_ARGS=("$APP/Contents/MacOS/IcecreamAgent" "$APP/Contents/Resources/desktop-agent.py")

mkdir -p "$HOME/Library/LaunchAgents"
ARGS_XML=""
for a in "${LAUNCH_ARGS[@]}"; do
  ARGS_XML="$ARGS_XML    <string>$a</string>
"
done
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$ARGS_XML  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$AGENT_DIR/agent.log</string>
  <key>StandardErrorPath</key>
  <string>$AGENT_DIR/agent.log</string>
</dict>
</plist>
EOF

# (Re)load the agent for this login session.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

# Tailscale.app presence (GUI app + login can't be automated; warn only).
if [[ -d "/Applications/Tailscale.app" ]]; then
  echo "[OK] Tailscale.app is installed."
else
  echo "[TODO] Install Tailscale for Mac: https://tailscale.com/download/mac"
fi

echo ""
echo "Installed and started. The agent listens on port 8765."
echo ""
echo "Your bearer token (keep it secret -- you will paste this into"
echo "Icecream's secure vault so it can call the agent):"
python3 -c "import json; print('  ' + json.load(open('$AGENT_DIR/config.json'))['token'])"
echo ""
# --- verify the agent answers, then print its self-check ---------------
AGENT_TOKEN="$(python3 -c "import json; print(json.load(open('$AGENT_DIR/config.json'))['token'])")"
echo "Waiting for the agent to answer on 127.0.0.1:8765..."
UP=""
for _ in $(seq 1 20); do
  if curl -s -m 2 -H "Authorization: Bearer $AGENT_TOKEN" \
      http://127.0.0.1:8765/api/health 2>/dev/null | grep -q '"ok": *true'; then
    UP=1; break
  fi
  sleep 1
done
if [[ "$UP" == "1" ]]; then
  echo "[OK] agent is serving."
else
  echo "[WARN] agent did not answer within 20s; see $AGENT_DIR/agent.log"
fi
echo ""
echo "Self-check (the agent tracks its own prerequisites):"
curl -s -m 5 -H "Authorization: Bearer $AGENT_TOKEN" \
  http://127.0.0.1:8765/api/selfcheck 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  (self-check unavailable)")
    sys.exit()
for name, c in d["checks"].items():
    mark = {"ok": "OK", "missing": "MISSING", "unknown": "?"}[c["status"]]
    req = "required" if c["required"] else "optional"
    print("  [%s] %-16s (%s)" % (mark, name, req))
    if c["status"] != "ok" and c.get("detail"):
        print("       -> %s" % c["detail"])
print("ready:", d["ready"])
if d.get("manual_steps"):
    print("Needs your hands (macOS lets no script grant these):")
    for s in d["manual_steps"]:
        print("  -", s)
'
echo ""
echo "Manual steps macOS reserves for you:"
echo "  - Privacy & Security > Accessibility: add Icecream Agent (at $APP)"
echo "  - Privacy & Security > Screen Recording: add Icecream Agent (same app)"
echo "  - Energy: never sleep. Allow incoming connections if asked."
echo ""
echo "To uninstall later, run ./uninstall.sh from this folder."
