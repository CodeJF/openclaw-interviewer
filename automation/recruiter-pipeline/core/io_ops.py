from __future__ import annotations

import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .common import dump_json, load_json
from .models import JDEntry, PipelineError


def ensure_runtime_dirs(runtime_dir: Path) -> dict[str, Path]:
    dirs = {
        'incoming': runtime_dir / 'incoming',
        'processed': runtime_dir / 'processed',
        'reports': runtime_dir / 'reports',
        'state': runtime_dir / 'state',
        'outbox': runtime_dir / 'outbox',
        'parsed': runtime_dir / 'parsed',
        'cache': runtime_dir / 'cache',
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



def decode_mime_header(value: str) -> str:
    import email.header
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or 'utf-8', errors='ignore'))
        else:
            parts.append(chunk)
    return ''.join(parts).strip()


def build_mail_header_index(mail_cfg: dict[str, Any], state_dir: Path, limit: int = 200) -> dict[str, Any]:
    import imaplib
    import email.utils
    from .imap_client import connect_imap
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / 'mail-header-index.json'

    client = connect_imap(mail_cfg)
    try:
        status, _ = client.select('INBOX')
        if status != 'OK':
            return {'updatedAt': datetime.now().isoformat(), 'count': 0, 'items': []}

        all_items = []
        for criterion, seen_flag in (('UNSEEN', False), ('SEEN', True)):
            try:
                status, data = client.uid('search', None, criterion)
                if status != 'OK':
                    continue
                uids = [u.decode() for u in data[0].split() if u]
                uids = list(reversed(uids[:limit]))
                for uid in uids:
                    status, parts = client.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
                    if status != 'OK' or not parts or not parts[0]:
                        continue
                    raw = parts[0][1].decode('utf-8', errors='ignore')
                    from_match = next((line[5:].strip() for line in raw.splitlines() if line.lower().startswith('from:')), '')
                    subject_match = next((line[8:].strip() for line in raw.splitlines() if line.lower().startswith('subject:')), '')
                    date_match = next((line[5:].strip() for line in raw.splitlines() if line.lower().startswith('date:')), '')
                    from_decoded = decode_mime_header(from_match)
                    subject_decoded = decode_mime_header(subject_match)
                    name_from_header, _addr = email.utils.parseaddr(from_decoded)
                    all_items.append({
                        'uid': uid,
                        'sender': from_decoded,
                        'candidate_name': name_from_header,
                        'subject': subject_decoded,
                        'date': date_match,
                        'seen': seen_flag,
                    })
            except Exception:
                continue
    finally:
        try:
            client.logout()
        except Exception:
            pass

    all_items.sort(key=lambda x: int(x.get('uid', 0)), reverse=True)
    index_data = {
        'updatedAt': datetime.now().isoformat(),
        'count': len(all_items),
        'items': all_items,
    }
    dump_json(index_path, index_data)
    return index_data
