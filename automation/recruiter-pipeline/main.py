#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.common import dump_json, load_json, sanitize_filename
from core.config import DEFAULT_CONFIG
from core.imap_client import connect_imap, ensure_seen, fetch_unseen_messages, get_remaining_unread
from core.io_ops import ensure_runtime_dirs, load_jds, package_results
from core.matching import choose_band, prefilter_candidate
from core.models import CandidateResult, MailItem, ParsedCandidate, PipelineError
from core.notifier import build_candidate_list, build_processed_mail_list, build_summary, send_message
from core.resume_parser import parse_mail_item
from core.reviewer import build_prompt, call_interviewer


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return load_json(state_path)
    return {'processed_uids': []}


def save_state(state_path: Path, state: dict) -> None:
    dump_json(state_path, state)


def process_candidate(candidate: ParsedCandidate, dirs: dict[str, Path], jds, bands, *, llm_top_k: int, min_llm_score: int) -> CandidateResult | None:
    shortlist_jds, prefilter_meta = prefilter_candidate(candidate, jds, top_k=llm_top_k, min_llm_score=min_llm_score)
    if not prefilter_meta.get('should_review'):
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
    jds = load_jds(Path(config['pipeline']['jdDir']))
    bands = config['pipeline']['scoreBands']
    max_emails = int(config['pipeline'].get('maxEmailsPerRun', 10) or 10)
    parse_jobs = int(config['pipeline'].get('parallelParseJobs', 4) or 4)
    llm_jobs = int(config['pipeline'].get('parallelLLMJobs', 1) or 1)
    llm_top_k = int(config['pipeline'].get('llmTopKPerResume', 2) or 2)
    min_llm_score = int(config['pipeline'].get('minLLMScore', 18) or 18)

    client = connect_imap(config['mail'])
    messages: list[MailItem] = []
    seen_failures: list[str] = []
    parsed_candidates: list[ParsedCandidate] = []
    results: list[CandidateResult] = []
    skipped_by_prefilter = 0
    try:
        messages = fetch_unseen_messages(client, max_emails=max_emails)
        if not args.dry_run:
            for item in messages:
                try:
                    ensure_seen(config['mail'], item.uid, client)
                    state['processed_uids'] = sorted(set(state.get('processed_uids', [])) | {item.uid})
                except Exception as exc:
                    seen_failures.append(f'{item.uid}: {exc}')
            save_state(state_path, state)

        with ThreadPoolExecutor(max_workers=parse_jobs) as executor:
            futures = {
                executor.submit(parse_mail_item, item.uid, item.message, dirs['incoming'], dirs['cache'] / 'parsed'):
                item.uid for item in messages
            }
            for future in as_completed(futures):
                candidate = future.result()
                if candidate is not None:
                    parsed_candidates.append(candidate)

        review_candidates: list[ParsedCandidate] = []
        for candidate in parsed_candidates:
            shortlist_jds, prefilter_meta = prefilter_candidate(candidate, jds, top_k=llm_top_k, min_llm_score=min_llm_score)
            if prefilter_meta.get('should_review'):
                review_candidates.append(candidate)
            else:
                skipped_by_prefilter += 1
                prefilter_dir = dirs['reports'] / 'prefilter-skipped'
                prefilter_dir.mkdir(parents=True, exist_ok=True)
                dump_json(prefilter_dir / f'{candidate.uid}.json', {
                    'uid': candidate.uid,
                    'subject': candidate.subject,
                    'sender': candidate.sender,
                    'candidate_name': candidate.candidate_name,
                    'prefilter': prefilter_meta,
                })

        with ThreadPoolExecutor(max_workers=llm_jobs) as executor:
            futures = {
                executor.submit(
                    process_candidate,
                    candidate,
                    dirs,
                    jds,
                    bands,
                    llm_top_k=llm_top_k,
                    min_llm_score=min_llm_score,
                ): candidate.uid
                for candidate in review_candidates
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result is not None:
                        results.append(result)
                except Exception as exc:
                    error_uid = futures[future]
                    error_dir = dirs['reports'] / 'errors'
                    error_dir.mkdir(parents=True, exist_ok=True)
                    (error_dir / f'{error_uid}.log').write_text(str(exc), encoding='utf-8')
    finally:
        try:
            client.logout()
        except Exception:
            pass

    remaining_unread = get_remaining_unread(config['mail'])
    processed_mail_list = build_processed_mail_list(messages)
    seen_failure_text = ''
    if seen_failures:
        seen_failure_text = '\n\n⚠️ IMAP 已读设置失败：\n' + '\n'.join(f'- {item}' for item in seen_failures)

    if results:
        summary = build_summary(results)
        candidate_list = build_candidate_list(results)
        zip_path = package_results([r.work_dir for r in results], dirs['outbox'])
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
📬 剩余未读：{remaining_unread} 封
🎯 筛选通过：{len(results)} 人
⏭️ 预筛跳过 LLM：{skipped_by_prefilter} 封

📨 本次读取名单：
{processed_mail_list}

📋 通过名单：
{candidate_list}

{summary}{seen_failure_text}

是否需要继续处理？'''
        if not args.dry_run:
            send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], msg)
            send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], '', media=str(zip_path))
        print(msg)
        print(zip_path)
    else:
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
📬 剩余未读：{remaining_unread} 封
⏭️ 预筛跳过 LLM：{skipped_by_prefilter} 封

📨 本次读取名单：
{processed_mail_list}

本次处理中没有评分在 80 分以上的候选人。{seen_failure_text}

是否需要继续处理？'''
        if not args.dry_run:
            send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], msg)
        print(msg)

    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
