from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import dump_json, load_json, sanitize_filename
from .config import DEFAULT_CONFIG
from .imap_client import connect_imap, ensure_seen, fetch_mail_by_uid, search_seen_by_name, search_unread_by_name, search_unread_header_items
from .io_ops import ensure_runtime_dirs, load_jds
from .matching import choose_band, prefilter_candidate
from .models import CandidateResult, ParsedCandidate
from .resume_parser import parse_mail_item
from .reviewer import build_prompt, call_interviewer

MOBILE_PATTERNS = [
    re.compile(r'(?<!\d)(1[3-9]\d{9})(?!\d)'),
    re.compile(r'(?<!\d)(?:86[- ]?)?(1[3-9]\d{9})(?!\d)'),
]
EMAIL_PATTERN = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
YEARS_PATTERN = re.compile(r'(\d+)\s*年')


def load_pipeline_context(config_path: Path = DEFAULT_CONFIG) -> tuple[dict[str, Any], dict[str, Path], list[Any], list[dict[str, Any]], int, int]:
    config = load_json(config_path)
    runtime_dir = Path(config['pipeline']['runtimeDir'])
    dirs = ensure_runtime_dirs(runtime_dir)
    jds = load_jds(Path(config['pipeline']['jdDir']))
    bands = config['pipeline']['scoreBands']
    llm_top_k = int(config['pipeline'].get('llmTopKPerResume', 2) or 2)
    min_llm_score = int(config['pipeline'].get('minLLMScore', 18) or 18)
    return config, dirs, jds, bands, llm_top_k, min_llm_score


def _find_mobile(text: str) -> str:
    for pattern in MOBILE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ''


def _find_email(text: str) -> str:
    match = EMAIL_PATTERN.search(text)
    return match.group(0) if match else ''


def _find_years(text: str) -> str:
    years = [int(x) for x in YEARS_PATTERN.findall(text)]
    return str(max(years)) if years else ''


def _primary_resume_filename(candidate: ParsedCandidate) -> str:
    if candidate.documents:
        file_name = str(candidate.documents[0].get('file') or '').strip()
        if file_name:
            return file_name
    if candidate.attachments:
        return candidate.attachments[0].name
    return ''


def _build_candidate_result(
    candidate: ParsedCandidate,
    eval_result: dict[str, Any],
    *,
    evaluation_path: Path,
    processed_at: str | None = None,
    source_task: str = 'recruiter-pipeline',
    work_dir: Path | None = None,
) -> CandidateResult:
    now = processed_at or datetime.now().isoformat()
    candidate_name = sanitize_filename(str(eval_result.get('candidate_name') or candidate.candidate_name), candidate.uid)
    material = candidate.candidate_text
    passed = bool(eval_result.get('passed'))
    return CandidateResult(
        mail_uid=candidate.uid,
        candidate_key=candidate.uid,
        sender=candidate.sender,
        subject=candidate.subject,
        matched_jd_title=str(eval_result.get('matched_jd_title') or '').strip(),
        score=int(eval_result.get('score') or 0),
        band=str(eval_result['band']) if eval_result.get('band') else None,
        passed=passed,
        fail_reason=str(eval_result.get('reason') or ''),
        prefilter_passed=bool((eval_result.get('prefilter') or {}).get('should_review')),
        candidate_name=candidate_name,
        resume_filename=_primary_resume_filename(candidate),
        phone=_find_mobile(material),
        email=_find_email(material),
        years_of_experience=_find_years(material),
        summary=str(eval_result.get('summary') or '').strip(),
        recommendation=str(eval_result.get('recommendation') or '').strip(),
        processed_at=now,
        updated_at=now,
        source_task=source_task,
        status='passed' if passed else 'rejected',
        notified=False,
        notes='',
        archive_dir=str(work_dir) if work_dir else '',
        raw_attachment_paths=[str(p) for p in candidate.attachments],
        evaluation_json=json.dumps(eval_result, ensure_ascii=False),
        raw_result=eval_result,
        evaluation_path=evaluation_path,
        work_dir=work_dir,
    )


def evaluate_candidate(candidate: ParsedCandidate, dirs: dict[str, Path], jds, bands, *, llm_top_k: int, min_llm_score: int) -> dict[str, Any]:
    review_dir = dirs['reports'] / 'single-evaluations'
    review_dir.mkdir(parents=True, exist_ok=True)
    shortlist_jds, prefilter_meta = prefilter_candidate(candidate, jds, top_k=llm_top_k, min_llm_score=min_llm_score)
    if not prefilter_meta.get('should_review'):
        prefilter_dir = dirs['reports'] / 'prefilter-skipped'
        prefilter_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            'uid': candidate.uid,
            'subject': candidate.subject,
            'sender': candidate.sender,
            'candidate_name': candidate.candidate_name,
            'prefilter': prefilter_meta,
            'passed': False,
            'reason': 'prefilter-skipped',
            'prefilter_passed': False,
            'matched_jd_title': prefilter_meta.get('top_jds', ['未命中岗位'])[0] if prefilter_meta.get('top_jds') else '未命中岗位',
            'score': 0,
            'band': None,
            'summary': '规则预筛未通过，未进入正式评审。',
            'recommendation': '当前不建议推进，除非人工认为该候选人值得特殊复核。',
        }
        dump_json(prefilter_dir / f'{candidate.uid}.json', payload)
        dump_json(review_dir / f'{candidate.uid}.json', payload)
        return payload

    prompt = build_prompt(candidate, shortlist_jds, prefilter_meta)
    result = call_interviewer(prompt)
    score = int(result['score'])
    band = choose_band(score, bands)
    jd_title = str(result['matched_jd_title']).strip()
    summary = str(result.get('summary') or '').strip()
    recommendation = str(result.get('recommendation') or '').strip()
    candidate_name = sanitize_filename(str(result.get('candidate_name') or candidate.candidate_name), candidate.uid)
    passed = band is not None

    eval_result = dict(result)
    eval_result.update({
        'candidate_name': candidate_name,
        'matched_jd_title': jd_title,
        'score': score,
        'band': band,
        'passed': passed,
        'prefilter': prefilter_meta,
        'prefilter_passed': True,
        'reason': 'passed' if passed else 'score-out-of-band',
    })

    dump_json(review_dir / f'{candidate.uid}.json', eval_result)
    return eval_result


def archive_candidate_result(candidate: ParsedCandidate, dirs: dict[str, Path], result: CandidateResult) -> CandidateResult:
    if not result.passed:
        return result

    band = result.band or 'unbanded'
    work_dir = dirs['processed'] / datetime.now().strftime('%Y-%m-%d') / sanitize_filename(result.matched_jd_title) / band / result.candidate_name
    work_dir.mkdir(parents=True, exist_ok=True)
    dump_json(work_dir / 'result.json', result.raw_result)
    dump_json(work_dir / 'mail.json', {
        'uid': candidate.uid,
        'subject': candidate.subject,
        'sender': candidate.sender,
        'documents': candidate.documents,
        'prefilter': result.raw_result.get('prefilter', {}),
    })
    (work_dir / 'candidate_material.txt').write_text(candidate.candidate_text, encoding='utf-8')
    for attachment in candidate.attachments:
        if attachment.exists():
            shutil.copy2(attachment, work_dir / attachment.name)

    result.work_dir = work_dir
    result.archive_dir = str(work_dir)
    return result


def process_candidate(
    candidate: ParsedCandidate,
    dirs: dict[str, Path],
    jds,
    bands,
    *,
    llm_top_k: int,
    min_llm_score: int,
    evaluation: dict[str, Any] | None = None,
    archive_passed: bool = True,
    source_task: str = 'recruiter-pipeline',
) -> CandidateResult:
    eval_result = evaluation or evaluate_candidate(candidate, dirs, jds, bands, llm_top_k=llm_top_k, min_llm_score=min_llm_score)
    evaluation_path = dirs['reports'] / 'single-evaluations' / f'{candidate.uid}.json'
    result = _build_candidate_result(
        candidate,
        eval_result,
        evaluation_path=evaluation_path,
        source_task=source_task,
    )
    if archive_passed:
        result = archive_candidate_result(candidate, dirs, result)
    return result


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
        eval_result = evaluate_candidate(candidate, dirs, jds, bands, llm_top_k=llm_top_k, min_llm_score=min_llm_score)
        if mark_seen_on_fetch:
            try:
                ensure_seen(config['mail'], uid, client)
            except Exception:
                pass
        result = process_candidate(candidate, dirs, jds, bands, llm_top_k=llm_top_k, min_llm_score=min_llm_score, evaluation=eval_result)
        if result.passed:
            return {
                'status': 'processed',
                'uid': uid,
                'workDir': str(result.work_dir) if result.work_dir else None,
                'attachments': [str(p) for p in candidate.attachments],
                'allFiles': [str(p) for p in candidate.all_files],
                'evaluation': eval_result,
            }
        return {
            'status': 'processed-no-pass',
            'uid': uid,
            'mailDir': str(candidate.mail_dir),
            'attachments': [str(p) for p in candidate.attachments],
            'allFiles': [str(p) for p in candidate.all_files],
            'evaluation': eval_result,
        }
    finally:
        try:
            client.logout()
        except Exception:
            pass



def find_unread_candidate_by_name(name: str, *, config_path: Path = DEFAULT_CONFIG, limit: int | None = None) -> list[dict[str, Any]]:
    """Find unread candidate by name. Searches ALL unread emails by name.

    Args:
        name: Candidate name to search for
        config_path: Path to config file
        limit: Maximum results to return (default None = return all matches)
    """
    config = load_json(config_path)
    client = connect_imap(config['mail'])
    try:
        items = search_unread_by_name(client, name, limit=limit)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return items



def find_seen_candidate_by_name(name: str, *, config_path: Path = DEFAULT_CONFIG, limit: int | None = None) -> list[dict[str, Any]]:
    """Find already-read candidate emails by name.

    This is the fallback when a candidate is no longer present locally and is no longer unread.
    """
    config = load_json(config_path)
    client = connect_imap(config['mail'])
    try:
        items = search_seen_by_name(client, name, limit=limit)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return items



def find_local_download_by_name(name: str, *, config_path: Path = DEFAULT_CONFIG) -> dict[str, Any] | None:
    config, dirs, *_ = load_pipeline_context(config_path)
    key = name.strip().lower()
    incoming_root = dirs['incoming']
    eval_root = dirs['reports'] / 'single-evaluations'
    for uid_dir in sorted(incoming_root.iterdir(), reverse=True):
        if not uid_dir.is_dir():
            continue
        raw_dir = uid_dir / 'raw'
        if not raw_dir.exists():
            continue
        matched_files = [str(p) for p in raw_dir.iterdir() if p.is_file() and key in p.name.lower()]
        if matched_files:
            evaluation = None
            eval_path = eval_root / f'{uid_dir.name}.json'
            if eval_path.exists():
                try:
                    evaluation = load_json(eval_path)
                except Exception:
                    evaluation = None
            return {'uid': uid_dir.name, 'attachments': matched_files, 'mailDir': str(uid_dir), 'evaluation': evaluation}
    return None



def find_local_download_by_uid(uid: str, *, config_path: Path = DEFAULT_CONFIG) -> dict[str, Any] | None:
    if not uid:
        return None
    config, dirs, *_ = load_pipeline_context(config_path)
    uid_dir = dirs['incoming'] / str(uid)
    raw_dir = uid_dir / 'raw'
    if not raw_dir.exists() or not uid_dir.exists():
        return None
    attachments = [str(p) for p in sorted(raw_dir.iterdir()) if p.is_file()]
    if not attachments:
        return None
    evaluation = None
    eval_path = dirs['reports'] / 'single-evaluations' / f'{uid}.json'
    if eval_path.exists():
        try:
            evaluation = load_json(eval_path)
        except Exception:
            evaluation = None
    return {'uid': str(uid), 'attachments': attachments, 'mailDir': str(uid_dir), 'evaluation': evaluation}
