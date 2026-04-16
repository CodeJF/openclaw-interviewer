from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / 'config.local.json'
OPENCLAW_CONFIG = Path(os.path.expanduser('~/.openclaw/openclaw.json'))
