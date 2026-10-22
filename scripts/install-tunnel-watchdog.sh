#!/usr/bin/env bash
# Install (or remove, with --uninstall) a macOS launchd agent that runs
# scripts/tunnel-watchdog.sh every 5 minutes and when you log in. Rerun it
# after editing either tunnel script, so the installed copy is refreshed.
# Log: ~/Library/Logs/sales-forecasting-tunnel-watchdog.log
set -euo pipefail

LABEL="com.sales-forecasting.tunnel-watchdog"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/Library/Logs/sales-forecasting-tunnel-watchdog.log"
# launchd jobs may not read ~/Desktop or ~/Documents (macOS privacy), so the
# scripts run from a copy here, and the Vercel project is passed by id
# instead of being read from frontend/.vercel.
INSTALL_DIR="$HOME/Library/Application Support/sales-forecasting-tunnel"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$PLIST"
  rm -rf "$INSTALL_DIR"
  echo "Removed $LABEL"
  exit 0
fi

# launchd starts with a bare PATH; carry over the directories holding docker,
# vercel, and node so the watchdog finds them.
TOOL_PATH="$(for tool in docker vercel node curl; do dirname "$(command -v "$tool")"; done | awk '!seen[$0]++' | paste -sd: -):/usr/bin:/bin"

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")" "$INSTALL_DIR"
cp "$ROOT/scripts/tunnel-watchdog.sh" "$ROOT/scripts/publish-tunnel.sh" "$INSTALL_DIR/"
read -r ORG_ID PROJECT_ID < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["orgId"], d["projectId"])' "$ROOT/frontend/.vercel/project.json")
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$INSTALL_DIR/tunnel-watchdog.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$TOOL_PATH</string>
    <key>VERCEL_ORG_ID</key><string>$ORG_ID</string>
    <key>VERCEL_PROJECT_ID</key><string>$PROJECT_ID</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL; log: $LOG"
