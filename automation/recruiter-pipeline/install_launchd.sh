#!/bin/bash
set -euo pipefail
SRC="/Users/jianfengxu/.openclaw/workspace-interviewer/automation/recruiter-pipeline/com.hichs.interviewer-recruiter-pipeline.plist"
DST="$HOME/Library/LaunchAgents/com.hichs.interviewer-recruiter-pipeline.plist"
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "/Users/jianfengxu/.openclaw/workspace-interviewer/automation/recruiter-pipeline/runtime/logs"
cp "$SRC" "$DST"
launchctl bootout "gui/$UID/com.hichs.interviewer-recruiter-pipeline" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$DST"
launchctl enable "gui/$UID/com.hichs.interviewer-recruiter-pipeline"
launchctl kickstart -k "gui/$UID/com.hichs.interviewer-recruiter-pipeline" >/dev/null 2>&1 || true
echo "Installed launchd job: com.hichs.interviewer-recruiter-pipeline"
launchctl print "gui/$UID/com.hichs.interviewer-recruiter-pipeline" | head -40 || true
