#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/../com.hichs.interviewer-recruiter-pipeline.plist"
DST="$HOME/Library/LaunchAgents/com.hichs.interviewer-recruiter-pipeline.plist"
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$SCRIPT_DIR/../runtime/logs"
cp "$SRC" "$DST"
launchctl bootout "gui/$UID/com.hichs.interviewer-recruiter-pipeline" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$DST"
launchctl enable "gui/$UID/com.hichs.interviewer-recruiter-pipeline"
echo "Installed launchd job: com.hichs.interviewer-recruiter-pipeline"
launchctl print "gui/$UID/com.hichs.interviewer-recruiter-pipeline" | head -40 || true
