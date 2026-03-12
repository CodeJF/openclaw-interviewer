from __future__ import annotations

import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .common import load_json
from .config import OPENCLAW_CONFIG
from .models import CandidateResult, MailItem, PipelineError


def build_processed_mail_list(messages: list[MailItem]) -> str:
    if not messages:
        return '无'
    lines = []
    for item in messages:
        sender = str(item.message.get('from') or '(unknown sender)').strip()
        subject = str(item.message.get('subject') or '(no subject)').strip()
        lines.append(f'- {sender} | UID {item.uid} | {subject}')
    return '\n'.join(lines)


def build_candidate_list(results: list[CandidateResult]) -> str:
    if not results:
        return '无'
    lines = []
    for r in results:
        lines.append(f'- {r.candidate_name} ({r.matched_jd_title}, {r.score}分)')
    return '\n'.join(lines)


def build_summary(results: list[CandidateResult]) -> str:
    grouped: dict[str, dict[str, int]] = {}
    for r in results:
        grouped.setdefault(r.matched_jd_title, {}).setdefault(r.band, 0)
        grouped[r.matched_jd_title][r.band] += 1
    lines = ['今日邮箱候选人筛选完成：']
    for jd_title in sorted(grouped):
        parts = [f'{band} 分 {count} 人' for band, count in sorted(grouped[jd_title].items())]
        lines.append(f'- {jd_title}：' + '，'.join(parts))
    return '\n'.join(lines)


def http_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))



def load_feishu_credentials(account_id: str) -> tuple[str, str]:
    cfg = load_json(OPENCLAW_CONFIG)
    acct = cfg['channels']['feishu']['accounts'][account_id]
    return acct['appId'], acct['appSecret']



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
    boundary = f'----OpenClawBoundary{uuid.uuid4().hex}'
    parts: list[bytes] = []
    mime_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if file_path.suffix.lower() == '.xlsx' else 'application/zip'

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
        f'Content-Type: {mime_type}\r\n\r\n'.encode(),
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



def send_feishu_file_via_api(account: str, target: str, file_path: str) -> dict[str, Any]:
    app_id, app_secret = load_feishu_credentials(account)
    token = get_feishu_tenant_token(app_id, app_secret)
    file_key = upload_feishu_file(token, Path(file_path), Path(file_path).name)
    payload = {
        'receive_id': target,
        'msg_type': 'file',
        'content': json.dumps({'file_key': file_key}, ensure_ascii=False),
    }
    data = http_json(
        'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
        payload,
        {'Authorization': f'Bearer {token}'},
    )
    if data.get('code') not in (0, None):
        raise PipelineError(f'Failed to send Feishu file via API: {data}')
    return {'method': 'feishu-api', 'fileKey': file_key, 'response': data}



def send_message(channel: str, account: str, target: str, text: str, media: str | None = None) -> dict[str, Any]:
    if media and channel == 'feishu':
        started = time.perf_counter()
        fallback = send_feishu_file_via_api(account, target, media)
        return {
            'elapsedMs': round((time.perf_counter() - started) * 1000, 2),
            'stdout': '',
            'stderr': '',
            'media': media,
            'hasText': bool(text),
            'method': 'feishu-api-direct-file',
            'fallback': fallback,
        }

    cmd = [
        'openclaw', 'message', 'send',
        '--channel', channel,
        '--account', account,
        '--target', target,
        '--json',
    ]
    if text:
        cmd.extend(['--message', text])
    if media:
        cmd.extend(['--media', media])

    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    stderr = proc.stderr.strip()
    stdout = proc.stdout.strip()
    if proc.returncode != 0:
        raise PipelineError(f'openclaw message send failed: {stderr or stdout}')

    return {
        'elapsedMs': elapsed_ms,
        'stdout': stdout,
        'stderr': stderr,
        'media': media,
        'hasText': bool(text),
        'method': 'openclaw-message-send',
    }
