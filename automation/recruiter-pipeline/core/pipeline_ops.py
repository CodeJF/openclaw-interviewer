from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import dump_json, load_json, sanitize_filename
from .config import DEFAULT_CONFIG
from .imap_client import connect_imap, ensure_seen, fetch_mail_by_uid, search_unread_header_items
from .io_ops import ensure_runtime_dirs, load_jds
from .matching import choose_band, prefilter_candidate
from .models import CandidateResult, ParsedCandidate
from .resume_parser import parse_mail_item
from .reviewer import build_prompt, call_interviewer


def load_pipeline_context(config_path: Path = DEFAULT_CONFIG) -> tuple[dict[str, Any], dict[str, Path], list[Any], list[dict[str, Any]], int, int]:
    config = load_json(config_path)
    runtime_dir = Path(config['pipeline']['runtimeDir'])
    dirs = ensure_runtime_dirs(runtime_dir)
    jds = load_jds(Path(config['pipeline']['jdDir']))
    bands = config['pipeline']['scoreBands']
    llm_top_k = int(config['pipeline'].get('llmTopKPerResume', 2) or 2)
    min_llm_score = int(config['pipeline'].get('minLLMScore', 18) or 18)
    return config, dirs, jds, bands, llm_top_k, min_llm_score



def process_candidate(candidate: ParsedCandidate, dirs: dict[str, Path], jds, bands, *, llm_top_k: int, min_llm_score: int) -> CandidateResult | None:
    shortlist_jds, prefilter_meta = prefilter_candidate(candidate, jds, top_k=llm_top_k, min_llm_score=min_llm_score)
    if not prefilter_meta.get('should_review'):
        prefilter_dir = dirs['reports'] / 'prefilter-skipped'
        prefilter_dir.mkdir(parents=True, exist_ok=True)
        dump_json(prefilter_dir / f'{candidate.uid}.json', {
            'uid': candidate.uid,
            'subject': candidate.subject,
            'sender': candidate.sender,
            'candidate_name': candidate.candidate_name,
            'prefilter': prefilter_meta,
        })
        return None

    prompt = build_prompt(candidate, shortlist_jds, prefilter_meta)
    result = call_interviewer(prompt)
    score = int(result['score'])
    band = choose_band(score, bands)
    if not band:
        return None

    jd_title = str(result['matched_jd_title']).strip()
    summary = str(result.get('summary') or '').strip()
    recommendation = str(result.get('recommendation') or '').strip()
    candidate_name = sanitize_filename(str(result.get('candidate_name') or candidate.candidate_name), candidate.uid)

    work_dir = dirs['processed'] / datetime.now().strftime('%Y-%m-%d') / sanitize_filename(jd_title) / band / candidate_name
    work_dir.mkdir(parents=True, exist_ok=True)
    dump_json(work_dir / 'result.json', result)
    dump_json(work_dir / 'mail.json', {
        'uid': candidate.uid,
        'subject': candidate.subject,
        'sender': candidate.sender,
        'documents': candidate.documents,
        'prefilter': prefilter_meta,
    })
    (work_dir / 'candidate_material.txt').write_text(candidate.candidate_text, encoding='utf-8')
    for attachment in candidate.attachments:
        if attachment.exists():
            shutil.copy2(attachment, work_dir / attachment.name)

    return CandidateResult(
        mail_uid=candidate.uid,
        sender=candidate.sender,
        subject=candidate.subject,
        matched_jd_title=jd_title,
        score=score,
        band=band,
        candidate_name=candidate_name,
        summary=summary,
        recommendation=recommendation,
        raw_result=result,
        work_dir=work_dir,
    )



def find_processed_result_by_uid(processed_root: Path, uid: str) -> Path | None:
    for mail_path in processed_root.rglob('mail.json'):
        try:
            data = load_json(mail_path)
            if str(data.get('uid') or '') == str(uid):
                return mail_path.parent
        except Exception:
            continue
    return None



def ensure_candidate_local_by_uid(uid: str, *, config_path: Path = DEFAULT_CONFIG, mark_seen_on_fetch: bool = True) -> dict[str, Any]:
    config, dirs, jds, bands, llm_top_k, min_llm_score = load_pipeline_context(config_path)
    processed_root = dirs['processed']
    existing = find_processed_result_by_uid(processed_root, uid)
    if existing:
        return {'status': 'existing', 'workDir': str(existing), 'uid': uid}

    client = connect_imap(config['mail'])
    try:
        item = fetch_mail_by_uid(client, uid)
        if item is None:
            return {'status': 'missing', 'uid': uid}
        candidate = parse_mail_item(item.uid, item.message, dirs['incoming'], dirs['cache'] / 'parsed')
        if candidate is None:
            return {'status': 'no-parse', 'uid': uid}
        result = process_candidate(candidate, dirs, jds, bands, llm_top_k=llm_top_k, min_llm_score=min_llm_score)
        if mark_seen_on_fetch:
            try:
                ensure_seen(config['mail'], uid, client)
            except Exception:
                pass
        if result is None:
            return {
                'status': 'processed-no-pass',
                'uid': uid,
                'mailDir': str(candidate.mail_dir),
                'attachments': [str(p) for p in candidate.attachments],
                'allFiles': [str(p) for p in candidate.all_files],
            }
        return {
            'status': 'processed',
            'uid': uid,
            'workDir': str(result.work_dir),
            'attachments': [str(p) for p in candidate.attachments],
            'allFiles': [str(p) for p in candidate.all_files],
        }
    finally:
        try:
            client.logout()
        except Exception:
            pass



def find_unread_candidate_by_name(name: str, *, config_path: Path = DEFAULT_CONFIG, limit: int = 20) -> list[dict[str, Any]]:
    config = load_json(config_path)
    client = connect_imap(config['mail'])
    try:
        items = search_unread_header_items(client, limit=limit)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    key = name.strip().lower()
    return [item for item in items if key and (key in str(item.get('candidate_name') or '').lower() or key in str(item.get('subject') or '').lower())]



def find_local_download_by_name(name: str, *, config_path: Path = DEFAULT_CONFIG) -> dict[str, Any] | None:
    config, dirs, *_ = load_pipeline_context(config_path)
    key = name.strip().lower()
    incoming_root = dirs['incoming']
    for uid_dir in sorted(incoming_root.iterdir(), reverse=True):
        if not uid_dir.is_dir():
            continue
        raw_dir = uid_dir / 'raw'
        if not raw_dir.exists():
            continue
        matched_files = [str(p) for p in raw_dir.iterdir() if p.is_file() and key in p.name.lower()]
        if matched_files:
            return {'uid': uid_dir.name, 'attachments': matched_files, 'mailDir': str(uid_dir)}
    return None
