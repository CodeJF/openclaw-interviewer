from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 清理无法用 UTF-8 编码的字符（surrogates）
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    # 移除无法编码的字符
    json_str = json_str.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    path.write_text(json_str, encoding='utf-8')


def sanitize_filename(name: str, fallback: str = 'item') -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', '_', name).strip()
    cleaned = cleaned.strip('.')
    return cleaned or fallback


def decode_text(value: str | None) -> str:
    return value.strip() if value else ''
