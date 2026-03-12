from __future__ import annotations

import email.utils
from email.message import Message
from pathlib import Path

from .common import decode_text, sanitize_filename
from .io_ops import maybe_extract_zip
from .models import ParsedCandidate, PipelineError


def extract_attachments(msg: Message, target_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for part in msg.walk():
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition not in ('attachment', 'inline') or not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        safe_name = sanitize_filename(filename)
        path = target_dir / safe_name
        path.write_bytes(payload)
        paths.append(path)
    return paths


def ensure_pdf_support():
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:
        raise PipelineError('Missing dependency pypdf. Run pip install -r requirements.txt') from exc
    return PdfReader


def extract_text_from_pdf(path: Path) -> str:
    PdfReader = ensure_pdf_support()
    reader = PdfReader(str(path))
    return '\n'.join(page.extract_text() or '' for page in reader.pages).strip()


def gather_candidate_text(files: list[Path]) -> tuple[str, list[dict[str, str]]]:
    documents: list[dict[str, str]] = []
    texts: list[str] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix == '.pdf':
            text = extract_text_from_pdf(path)
        elif suffix in {'.txt', '.md'}:
            text = path.read_text(encoding='utf-8', errors='ignore')
        else:
            continue
        if not text.strip():
            continue
        documents.append({'file': path.name, 'text': text})
        texts.append(f'## 文件：{path.name}\n{text}')
    return '\n\n'.join(texts).strip(), documents


def extract_sender_name(sender: str) -> str:
    name, addr = email.utils.parseaddr(sender)
    candidate = (name or addr or sender).strip()
    return sanitize_filename(candidate, 'unknown-candidate')


def parse_mail_item(uid: str, msg: Message, incoming_dir: Path) -> ParsedCandidate | None:
    subject = decode_text(msg.get('subject')) or '(no subject)'
    sender = decode_text(msg.get('from')) or '(unknown sender)'
    candidate_name = extract_sender_name(sender)

    mail_dir = incoming_dir / uid
    raw_dir = mail_dir / 'raw'
    extracted_dir = mail_dir / 'extracted'
    raw_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    attachments = extract_attachments(msg, raw_dir)
    if not attachments:
        return None

    all_files: list[Path] = []
    for attachment in attachments:
        unpack_dir = extracted_dir / sanitize_filename(attachment.stem, 'unzipped')
        unpack_dir.mkdir(parents=True, exist_ok=True)
        all_files.extend(maybe_extract_zip(attachment, unpack_dir))
        if attachment.suffix.lower() != '.zip':
            all_files.append(attachment)

    candidate_text, documents = gather_candidate_text(all_files)
    if not candidate_text:
        return None

    return ParsedCandidate(
        uid=uid,
        sender=sender,
        subject=subject,
        candidate_name=candidate_name,
        mail_dir=mail_dir,
        attachments=attachments,
        all_files=all_files,
        candidate_text=candidate_text,
        documents=documents,
    )
