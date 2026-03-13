from __future__ import annotations

import email
import email.policy
import imaplib
from typing import Any

from .models import MailItem, PipelineError


def connect_imap(cfg: dict[str, Any]):
    host = cfg['host']
    port = int(cfg['port'])
    use_ssl = bool(cfg.get('ssl'))
    user = cfg['username']
    password = cfg['password']
    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    client.login(user, password)
    return client


def fetch_mail_by_uid(client: imaplib.IMAP4, uid: str) -> MailItem | None:
    status, _ = client.select('INBOX')
    if status != 'OK':
        raise PipelineError('Unable to select INBOX')
    status, parts = client.uid('fetch', uid, '(RFC822)')
    if status != 'OK' or not parts or not parts[0]:
        return None
    raw = parts[0][1]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return MailItem(uid=uid, message=msg)



def search_unread_header_items(client: imaplib.IMAP4, limit: int = 20) -> list[dict[str, Any]]:
    status, _ = client.select('INBOX')
    if status != 'OK':
        raise PipelineError('Unable to select INBOX')
    status, data = client.uid('search', None, 'UNSEEN')
    if status != 'OK':
        raise PipelineError('Unable to search unseen messages')
    uids = [u.decode() for u in data[0].split() if u]
    items: list[dict[str, Any]] = []
    for uid in list(reversed(uids))[:limit]:
        status, parts = client.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
        if status != 'OK' or not parts or not parts[0]:
            continue
        raw = parts[0][1].decode('utf-8', errors='ignore')
        from_match = next((line[5:].strip() for line in raw.splitlines() if line.lower().startswith('from:')), '')
        subject_match = next((line[8:].strip() for line in raw.splitlines() if line.lower().startswith('subject:')), '')
        date_match = next((line[5:].strip() for line in raw.splitlines() if line.lower().startswith('date:')), '')
        name, _addr = email.utils.parseaddr(from_match)
        items.append({'uid': uid, 'sender': from_match, 'candidate_name': name, 'subject': subject_match, 'date': date_match})
    return items



def fetch_unseen_messages(client: imaplib.IMAP4, max_emails: int | None = None, cfg: dict[str, Any] | None = None) -> list[MailItem]:
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

    messages: list[MailItem] = []
    current_client = client
    reconnects = 0
    for uid in uids:
        try:
            status, parts = current_client.uid('fetch', uid, '(RFC822)')
        except imaplib.IMAP4.abort as exc:
            if cfg is None or reconnects >= 2:
                raise PipelineError(f'IMAP fetch aborted at uid {uid}: {exc}') from exc
            reconnects += 1
            try:
                current_client = connect_imap(cfg)
                status, _ = current_client.select('INBOX')
                if status != 'OK':
                    raise PipelineError('Unable to re-select INBOX after reconnect')
                status, parts = current_client.uid('fetch', uid, '(RFC822)')
            except Exception as retry_exc:
                raise PipelineError(f'IMAP reconnect/fetch failed at uid {uid}: {retry_exc}') from retry_exc

        if status != 'OK' or not parts or not parts[0]:
            continue
        raw = parts[0][1]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        messages.append(MailItem(uid=uid, message=msg))
    return messages


def fetch_mail_flags(client: imaplib.IMAP4, uid: str) -> str:
    status, data = client.uid('fetch', uid, '(FLAGS)')
    if status != 'OK' or not data:
        raise PipelineError(f'Failed to fetch flags for uid {uid}: {data}')
    payload = b' '.join(part for part in data if isinstance(part, bytes)).decode('utf-8', errors='ignore')
    return payload


def mark_seen(client: imaplib.IMAP4, uid: str) -> None:
    try:
        status, _ = client.select('INBOX')
    except Exception as exc:
        raise PipelineError(f'IMAP connection lost before marking uid {uid} as seen') from exc
    if status != 'OK':
        raise PipelineError(f'Unable to re-select INBOX before marking uid {uid} as seen')

    status, data = client.uid('store', uid, '+FLAGS.SILENT', '(\\Seen)')
    if status != 'OK':
        raise PipelineError(f'Failed to mark uid {uid} as seen: {data}')

    flags_payload = fetch_mail_flags(client, uid)
    if '\\Seen' not in flags_payload:
        raise PipelineError(f'IMAP store succeeded but uid {uid} is still not seen: {flags_payload}')


def ensure_seen(cfg: dict[str, Any], uid: str, client: imaplib.IMAP4 | None = None) -> None:
    last_error: Exception | None = None
    if client is not None:
        try:
            mark_seen(client, uid)
            return
        except Exception as exc:
            last_error = exc

    retry_client = None
    try:
        retry_client = connect_imap(cfg)
        mark_seen(retry_client, uid)
        return
    except Exception as exc:
        last_error = exc
    finally:
        if retry_client is not None:
            try:
                retry_client.logout()
            except Exception:
                pass

    raise PipelineError(f'Failed to reliably mark uid {uid} as seen: {last_error}')


def get_remaining_unread(cfg: dict[str, Any]) -> int:
    client = connect_imap(cfg)
    try:
        client.select('INBOX')
        status, data = client.uid('search', None, 'UNSEEN')
        if status != 'OK':
            return -1
        return len(data[0].split()) if data and data[0] else 0
    finally:
        try:
            client.logout()
        except Exception:
            pass
