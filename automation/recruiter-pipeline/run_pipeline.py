#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any

ROOT = Path('/Users/jianfengxu/.openclaw/workspace-interviewer/automation/recruiter-pipeline')
DEFAULT_CONFIG = ROOT / 'config.local.json'
OPENCLAW_CONFIG = Path('/Users/jianfengxu/.openclaw/openclaw.json')


class PipelineError(RuntimeError):
    pass


@dataclass
class JDEntry:
    title: str
    path: Path
    content: str


@dataclass
class CandidateResult:
    mail_uid: str
    sender: str
    subject: str
    matched_jd_title: str
    score: int
    band: str
    candidate_name: str
    summary: str
    recommendation: str
    raw_result: dict[str, Any]
    work_dir: Path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def sanitize_filename(name: str, fallback: str = 'item') -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', '_', name).strip()
    cleaned = cleaned.strip('.')
    return cleaned or fallback


def ensure_runtime_dirs(runtime_dir: Path) -> dict[str, Path]:
    dirs = {
        'incoming': runtime_dir / 'incoming',
        'processed': runtime_dir / 'processed',
        'reports': runtime_dir / 'reports',
        'state': runtime_dir / 'state',
        'outbox': runtime_dir / 'outbox',
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def load_state(state_path: Path) -> dict[str, Any]:
    if state_path.exists():
        return load_json(state_path)
    return {'processed_uids': []}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    dump_json(state_path, state)


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


def connect_imap(cfg: dict[str, Any]):
    host = cfg['host']
    port = int(cfg['port'])
    use_ssl = bool(cfg.get('ssl'))
    user = cfg['username']
    password = cfg['password']
    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    client.login(user, password)
    return client


def fetch_unseen_messages(client: imaplib.IMAP4, processed_uids: set[str], max_emails: int | None = None) -> list[tuple[str, Message]]:
    status, _ = client.select('INBOX')
    if status != 'OK':
        raise PipelineError('Unable to select INBOX')
    status, data = client.uid('search', None, 'UNSEEN')
    if status != 'OK':
        raise PipelineError('Unable to search unseen messages')
    uids = [u.decode() for u in data[0].split() if u]
    uids = list(reversed(uids))
    if max_emails and max_emails > 0:
        uids = uids[:max_emails]
    messages = []
    for uid in uids:
        if uid in processed_uids:
            continue
        status, parts = client.uid('fetch', uid, '(RFC822)')
        if status != 'OK' or not parts or not parts[0]:
            continue
        raw = parts[0][1]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        messages.append((uid, msg))
    return messages


def decode_text(value: str | None) -> str:
    return value.strip() if value else ''


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


def ensure_pdf_support() -> Any:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise PipelineError(
            'Missing dependency pypdf. Run: python3 -m pip install -r automation/recruiter-pipeline/requirements.txt'
        ) from exc
    return PdfReader


def extract_text_from_pdf(path: Path) -> str:
    PdfReader = ensure_pdf_support()
    reader = PdfReader(str(path))
    text_parts: list[str] = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or '')
    return '\n'.join(text_parts).strip()


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
        texts.append(f"## 文件：{path.name}\n{text}")
    return '\n\n'.join(texts).strip(), documents


def choose_band(score: int, bands: list[dict[str, Any]]) -> str | None:
    for band in bands:
        if int(band['min']) <= score <= int(band['max']):
            return str(band['name'])
    return None


def build_prompt(subject: str, sender: str, candidate_text: str, jds: list[JDEntry]) -> str:
    jd_block = []
    for jd in jds:
        jd_block.append(f"# JD: {jd.title}\n{jd.content}")
    jd_text = '\n\n'.join(jd_block)
    schema = {
        'candidate_name': 'string',
        'matched_jd_title': 'string, must exactly equal one JD title above',
        'route_confidence': 'number 0-1',
        'score': 'integer 0-99',
        'summary': 'short Chinese summary',
        'recommendation': 'short Chinese recommendation',
        'strengths': ['list of strings'],
        'risks': ['list of strings'],
    }
    return textwrap.dedent(
        f'''
        你是专业 AI 面试官。请先在下面的 JD 集合中为候选人自动路由到最匹配的岗位，再按该岗位 JD 打分。

        要求：
        1. 只能从给定 JD 中选择一个最匹配岗位。
        2. 分数范围 0-99。
        3. 80-89 代表较强匹配，90-99 代表高匹配。
        4. 如果不匹配任何岗位，也要选出“最接近”的一个岗位，但分数可以低于 80。
        5. 只输出 JSON，不要输出 markdown、解释或代码块。

        JSON schema:
        {json.dumps(schema, ensure_ascii=False)}

        邮件主题：{subject}
        发件人：{sender}

        候选人材料：
        {candidate_text}

        JD 集合：
        {jd_text}
        '''
    ).strip()


def call_interviewer(prompt: str) -> dict[str, Any]:
    cmd = [
        'openclaw', 'agent',
        '--agent', 'interviewer',
        '--message', prompt,
        '--json',
        '--timeout', '600',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f'interviewer agent failed: {proc.stderr.strip() or proc.stdout.strip()}')
    stdout = proc.stdout.strip()
    data = json.loads(stdout)
    content = data.get('reply') or data.get('text') or data.get('message') or ''
    result_block = data.get('result')
    if not content and isinstance(result_block, dict):
        payloads = result_block.get('payloads') or []
        if payloads and isinstance(payloads[0], dict):
            content = payloads[0].get('text') or ''
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    if not content and isinstance(result_block, str):
        content = result_block
    if not content:
        raise PipelineError(f'Unexpected interviewer output: {stdout[:500]}')
    match = re.search(r'\{.*\}', content, re.S)
    if not match:
        raise PipelineError(f'No JSON object found in interviewer output: {content[:500]}')
    return json.loads(match.group(0))


def load_feishu_credentials(account_id: str) -> tuple[str, str]:
    cfg = load_json(OPENCLAW_CONFIG)
    feishu = cfg['channels']['feishu']
    acct = feishu['accounts'][account_id]
    return acct['appId'], acct['appSecret']


def http_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def get_feishu_tenant_token(app_id: str, app_secret: str) -> str:
    data = http_json('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', {
        'app_id': app_id,
        'app_secret': app_secret,
    })
    token = data.get('tenant_access_token')
    if not token:
        raise PipelineError(f'Failed to get Feishu tenant token: {data}')
    return token


def upload_feishu_file(token: str, file_path: Path, file_name: str) -> str:
    import uuid
    import urllib.request
    boundary = f'----OpenClawBoundary{uuid.uuid4().hex}'
    parts: list[bytes] = []
    def add_field(name: str, value: str):
        parts.extend([
            f'--{boundary}\r\n'.encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode('utf-8'),
            b'\r\n',
        ])
    add_field('file_type', 'stream')
    parts.extend([
        f'--{boundary}\r\n'.encode(),
        f'Content-Disposition: form-data; name="file_name"\r\n\r\n{file_name}\r\n'.encode(),
        f'--{boundary}\r\n'.encode(),
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode(),
        b'Content-Type: application/zip\r\n\r\n',
        file_path.read_bytes(),
        b'\r\n',
        f'--{boundary}--\r\n'.encode(),
    ])
    body = b''.join(parts)
    req = urllib.request.Request('https://open.feishu.cn/open-apis/im/v1/files', data=body, method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    file_key = data.get('data', {}).get('file_key')
    if not file_key:
        raise PipelineError(f'Failed to upload Feishu file: {data}')
    return file_key


def send_feishu_message(account_id: str, target_type: str, target_id: str, text: str, zip_path: Path | None = None) -> None:
    app_id, app_secret = load_feishu_credentials(account_id)
    token = get_feishu_tenant_token(app_id, app_secret)
    headers = {'Authorization': f'Bearer {token}'}
    receive_id_type = 'open_id' if target_type == 'dm' else 'chat_id'
    text_payload = {
        'receive_id': target_id,
        'msg_type': 'text',
        'content': json.dumps({'text': text}, ensure_ascii=False),
    }
    data = http_json(f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}', text_payload, headers)
    if data.get('code') not in (0, None):
        raise PipelineError(f'Failed to send Feishu text message: {data}')
    if zip_path:
        file_key = upload_feishu_file(token, zip_path, zip_path.name)
        file_payload = {
            'receive_id': target_id,
            'msg_type': 'file',
            'content': json.dumps({'file_key': file_key}, ensure_ascii=False),
        }
        data = http_json(f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}', file_payload, headers)
        if data.get('code') not in (0, None):
            raise PipelineError(f'Failed to send Feishu file message: {data}')


def mark_seen(client: imaplib.IMAP4, uid: str) -> None:
    """Mark a message as seen and verify the IMAP command succeeds."""
    try:
        status, _ = client.select('INBOX')
    except Exception as exc:
        raise PipelineError(f'IMAP connection lost before marking uid {uid} as seen') from exc
    if status != 'OK':
        raise PipelineError(f'Unable to re-select INBOX before marking uid {uid} as seen')

    status, data = client.uid('store', uid, '+FLAGS', '(\\Seen)')
    if status != 'OK':
        raise PipelineError(f'Failed to mark uid {uid} as seen: {data}')


def process_message(uid: str, msg: Message, dirs: dict[str, Path], jds: list[JDEntry], bands: list[dict[str, Any]]) -> CandidateResult | None:
    subject = decode_text(msg.get('subject')) or '(no subject)'
    sender = decode_text(msg.get('from')) or '(unknown sender)'
    mail_dir = dirs['incoming'] / uid
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

    candidate_text, docs = gather_candidate_text(all_files)
    if not candidate_text:
        return None

    prompt = build_prompt(subject, sender, candidate_text, jds)
    result = call_interviewer(prompt)
    score = int(result['score'])
    band = choose_band(score, bands)
    if not band:
        return None
    jd_title = str(result['matched_jd_title']).strip()
    candidate_name = sanitize_filename(str(result.get('candidate_name') or Path(sender).stem or uid), uid)
    summary = str(result.get('summary') or '').strip()
    recommendation = str(result.get('recommendation') or '').strip()

    work_dir = dirs['processed'] / datetime.now().strftime('%Y-%m-%d') / sanitize_filename(jd_title) / band / candidate_name
    work_dir.mkdir(parents=True, exist_ok=True)
    dump_json(work_dir / 'result.json', result)
    dump_json(work_dir / 'mail.json', {
        'uid': uid,
        'subject': subject,
        'sender': sender,
        'documents': docs,
    })
    (work_dir / 'candidate_material.txt').write_text(candidate_text, encoding='utf-8')
    for attachment in attachments:
        shutil.copy2(attachment, work_dir / attachment.name)

    return CandidateResult(
        mail_uid=uid,
        sender=sender,
        subject=subject,
        matched_jd_title=jd_title,
        score=score,
        band=band,
        candidate_name=candidate_name,
        summary=summary,
        recommendation=recommendation,
        raw_result=result,
        work_dir=work_dir,
    )


def package_results(result_dirs: list[Path], outbox_dir: Path) -> Path:
    """Create a zip file containing only this run's candidate result directories."""
    timestamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    zip_name = f"interviewer-shortlist-{timestamp}.zip"
    zip_path = outbox_dir / zip_name

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for src_dir in result_dirs:
            if not src_dir.exists():
                continue
            relative_parts = src_dir.parts[-4:] if len(src_dir.parts) >= 4 else src_dir.parts
            dst_dir = tmp_path.joinpath(*relative_parts)
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=False)
        shutil.make_archive(str(zip_path.with_suffix('')), 'zip', root_dir=tmp_path)

    return zip_path


def build_summary(results: list[CandidateResult]) -> str:
    grouped: dict[str, dict[str, int]] = {}
    for r in results:
        grouped.setdefault(r.matched_jd_title, {}).setdefault(r.band, 0)
        grouped[r.matched_jd_title][r.band] += 1
    lines = ['今日邮箱候选人筛选完成：']
    for jd_title in sorted(grouped):
        parts = [f"{band} 分 {count} 人" for band, count in sorted(grouped[jd_title].items())]
        lines.append(f"- {jd_title}：" + '，'.join(parts))
    return '\n'.join(lines)


def build_candidate_list(results: list[CandidateResult]) -> str:
    """Build a list of shortlisted candidates with their scores."""
    if not results:
        return "无"
    lines = []
    for r in results:
        lines.append(f"- {r.candidate_name} ({r.matched_jd_title}, {r.score}分)")
    return '\n'.join(lines)


def extract_sender_name(sender: str) -> str:
    name, addr = email.utils.parseaddr(sender)
    candidate = (name or addr or sender).strip()
    return sanitize_filename(candidate, 'unknown-candidate')


def build_processed_mail_list(messages: list[tuple[str, Message]]) -> str:
    if not messages:
        return '无'
    lines = []
    for uid, msg in messages:
        sender = decode_text(msg.get('from')) or '(unknown sender)'
        subject = decode_text(msg.get('subject')) or '(no subject)'
        candidate_name = extract_sender_name(sender)
        lines.append(f'- {candidate_name} | UID {uid} | {subject}')
    return '\n'.join(lines)


def process_message_wrapper(args: tuple) -> tuple[str, CandidateResult | None, str | None]:
    """Thread-safe wrapper for processing a single message."""
    uid, msg, dirs, jds, bands, dry_run = args
    try:
        result = process_message(uid, msg, dirs, jds, bands)
        return (uid, result, None)
    except Exception as exc:
        error_dir = dirs['reports'] / 'errors'
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / f'{uid}.log').write_text(str(exc), encoding='utf-8')
        return (uid, None, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    config = load_json(Path(args.config))
    runtime_dir = Path(config['pipeline']['runtimeDir'])
    dirs = ensure_runtime_dirs(runtime_dir)
    state_path = dirs['state'] / 'processed-mail-ids.json'
    state = load_state(state_path)
    processed_uids = set(state.get('processed_uids', []))
    jds = load_jds(Path(config['pipeline']['jdDir']))
    bands = config['pipeline']['scoreBands']
    max_emails_limit = int(config['pipeline'].get('maxEmailsLimit', 50) or 50)
    max_emails = int(config['pipeline'].get('maxEmailsPerRun', 20) or 20)
    max_emails = min(max_emails, max_emails_limit)  # Ensure doesn't exceed limit
    parallel_jobs = int(config['pipeline'].get('parallelJobs', 3) or 3)

    client = connect_imap(config['mail'])
    results: list[CandidateResult] = []
    newly_processed: list[str] = []
    messages: list[tuple[str, Message]] = []
    try:
        messages = fetch_unseen_messages(client, processed_uids, max_emails=max_emails)

        # Prepare arguments for parallel processing
        task_args = [
            (uid, msg, dirs, jds, bands, args.dry_run)
            for uid, msg in messages
        ]

        # Process messages in parallel
        with ThreadPoolExecutor(max_workers=parallel_jobs) as executor:
            futures = {executor.submit(process_message_wrapper, arg): arg[0] for arg in task_args}
            for future in as_completed(futures):
                uid, result, error = future.result()
                if result:
                    results.append(result)
                if not args.dry_run:
                    newly_processed.append(uid)

        # Mark all fetched mails as seen after processing
        if not args.dry_run:
            for uid in newly_processed:
                try:
                    mark_seen(client, uid)
                except Exception as exc:
                    print(f'Warning: Failed to mark uid {uid} as seen: {exc}')
            processed_uid_set = set(state.get('processed_uids', [])) | set(newly_processed)
            state['processed_uids'] = sorted(processed_uid_set)
            save_state(state_path, state)
        else:
            processed_uid_set = set(state.get('processed_uids', []))
    finally:
        try:
            client.logout()
        except Exception:
            pass

    # Get remaining unread count after processing
    try:
        client = connect_imap(config['mail'])
        client.select('INBOX')
        status, data = client.uid('search', None, 'UNSEEN')
        if status == 'OK':
            all_unseen = set(u.decode() for u in data[0].split() if u)
            remaining_unread = len(all_unseen - processed_uid_set)
        else:
            remaining_unread = -1
        client.logout()
    except Exception:
        remaining_unread = -1  # Unknown

    processed_mail_list = build_processed_mail_list(messages)
    feishu_cfg = config['feishu']
    if results:
        summary = build_summary(results)
        candidate_list = build_candidate_list(results)
        # Only zip the work directories from THIS run's qualified candidates
        result_dirs = [r.work_dir for r in results]
        zip_path = package_results(result_dirs, dirs['outbox'])
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
📬 剩余未读：{remaining_unread} 封
🎯 筛选通过：{len(results)} 人

📨 本次读取名单：
{processed_mail_list}

📋 通过名单：
{candidate_list}

{summary}

是否需要继续处理？'''
        if not args.dry_run:
            send_feishu_message(feishu_cfg['replyAccount'], feishu_cfg['targetType'], feishu_cfg['targetId'], msg, zip_path)
        print(msg)
        print(zip_path)
    else:
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
📬 剩余未读：{remaining_unread} 封

📨 本次读取名单：
{processed_mail_list}

本次处理中没有评分在 80 分以上的候选人。

是否需要继续处理？'''
        if not args.dry_run:
            send_feishu_message(feishu_cfg['replyAccount'], feishu_cfg['targetType'], feishu_cfg['targetId'], msg)
        print(msg)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
