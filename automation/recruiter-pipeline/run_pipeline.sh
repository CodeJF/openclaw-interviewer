#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$WORKSPACE_DIR"
if [ ! -x automation/recruiter-pipeline/.venv/bin/python ]; then
  python3 -m venv automation/recruiter-pipeline/.venv
fi
automation/recruiter-pipeline/.venv/bin/python -m pip install -r automation/recruiter-pipeline/requirements.txt >/tmp/recruiter-pipeline-pip.log 2>&1
automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/run_pipeline.py "$@"
