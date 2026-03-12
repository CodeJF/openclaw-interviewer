from __future__ import annotations

import subprocess

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


def send_message(channel: str, account: str, target: str, text: str, media: str | None = None) -> None:
    cmd = [
        'openclaw', 'message', 'send',
        '--channel', channel,
        '--account', account,
        '--target', target,
    ]
    if text:
        cmd.extend(['--message', text])
    if media:
        cmd.extend(['--media', media])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f'openclaw message send failed: {proc.stderr.strip() or proc.stdout.strip()}')
