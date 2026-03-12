from __future__ import annotations

import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .models import JDEntry, PipelineError


def ensure_runtime_dirs(runtime_dir: Path) -> dict[str, Path]:
    dirs = {
        'incoming': runtime_dir / 'incoming',
        'processed': runtime_dir / 'processed',
        'reports': runtime_dir / 'reports',
        'state': runtime_dir / 'state',
        'outbox': runtime_dir / 'outbox',
        'parsed': runtime_dir / 'parsed',
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def load_jds(jd_dir: Path) -> list[JDEntry]:
    entries: list[JDEntry] = []
    for path in sorted(jd_dir.iterdir()):
        if path.is_file():
            content = path.read_text(encoding='utf-8').strip()
            if content:
                entries.append(JDEntry(title=path.stem or path.name, path=path, content=content))
    if not entries:
        raise PipelineError(f'JD directory is empty: {jd_dir}')
    return entries


def maybe_extract_zip(path: Path, target_dir: Path) -> list[Path]:
    if path.suffix.lower() != '.zip':
        return [path]
    extracted: list[Path] = []
    with zipfile.ZipFile(path) as zf:
        zf.extractall(target_dir)
    for item in target_dir.rglob('*'):
        if item.is_file():
            extracted.append(item)
    return extracted


def package_results(result_dirs: list[Path], outbox_dir: Path) -> Path:
    timestamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    zip_name = f'interviewer-shortlist-{timestamp}.zip'
    zip_path = outbox_dir / zip_name
    base_name = zip_path.with_suffix('')

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for src_dir in result_dirs:
            if not src_dir.exists():
                continue
            relative_parts = src_dir.parts[-4:] if len(src_dir.parts) >= 4 else src_dir.parts
            dst_dir = tmp_path.joinpath(*relative_parts)
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=False)
        shutil.make_archive(str(base_name), 'zip', root_dir=tmp_path)
    return zip_path
