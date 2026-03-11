#!/bin/bash
set -euo pipefail
cd /Users/jianfengxu/.openclaw/workspace-interviewer
if [ ! -x automation/recruiter-pipeline/.venv/bin/python ]; then
  python3 -m venv automation/recruiter-pipeline/.venv
fi
automation/recruiter-pipeline/.venv/bin/python -m pip install -r automation/recruiter-pipeline/requirements.txt >/tmp/recruiter-pipeline-pip.log 2>&1
automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/run_pipeline.py "$@"
