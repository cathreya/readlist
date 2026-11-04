#!/bin/zsh
# Install the Mac-side Obsidian note flusher as a LaunchAgent.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$HOME/Library/Application Support/Flashcap"
AGENT="$AGENT_DIR/note_agent.py"
REMOTE="${FLASHCAP_REMOTE_URL:-https://hermes.taila17c7c.ts.net}"
PLIST="$HOME/Library/LaunchAgents/com.flashcap.noteagent.plist"
LOG="$HOME/Library/Logs/flashcap-note-agent.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" "$AGENT_DIR"
# launchd has no Desktop TCC access; run the agent from Application Support.
cp "$PROJECT_DIR/capture/note_agent.py" "$AGENT"
chmod 755 "$AGENT"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.flashcap.noteagent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$AGENT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>FLASHCAP_REMOTE_URL</key><string>$REMOTE</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>30</integer>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed com.flashcap.noteagent"
echo "  remote: $REMOTE"
echo "  log:    $LOG"
