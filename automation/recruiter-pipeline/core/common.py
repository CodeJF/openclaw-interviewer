from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def sanitize_filename(name: str, fallback: str = 'item') -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', '_', name).strip()
    cleaned = cleaned.strip('.')
    return cleaned or fallback


def decode_text(value: str | None) -> str:
    return value.strip() if value else ''
