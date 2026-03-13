#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.common import dump_json, load_json, sanitize_filename
from core.config import DEFAULT_CONFIG
from core.imap_client import connect_imap, ensure_seen, fetch_unseen_messages, get_remaining_unread
from core.io_ops import build_mail_header_index, ensure_runtime_dirs, load_jds, package_results
from core.models import CandidateResult, MailItem, ParsedCandidate, PipelineError
from core.notifier import build_candidate_list, build_processed_mail_list, build_summary, send_message
from core.matching import prefilter_candidate
from core.pipeline_ops import process_candidate
from core.reporting import build_excel_report
from core.resume_parser import parse_mail_item


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return load_json(state_path)
    return {'processed_uids': []}


def save_state(state_path: Path, state: dict) -> None:
    dump_json(state_path, state)


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
    metrics: dict[str, object] = {
        'startedAt': datetime.now().isoformat(),
        'durationsMs': {},
        'counts': {},
    }
    try:
        t0 = time.perf_counter()
        messages = fetch_unseen_messages(client, max_emails=max_emails, cfg=config['mail'])
        metrics['durationsMs']['fetchMail'] = round((time.perf_counter() - t0) * 1000, 2)
        metrics['counts']['messagesFetched'] = len(messages)

        if not args.dry_run:
            t1 = time.perf_counter()
            for item in messages:
                try:
                    ensure_seen(config['mail'], item.uid, client)
                    state['processed_uids'] = sorted(set(state.get('processed_uids', [])) | {item.uid})
                except Exception as exc:
                    seen_failures.append(f'{item.uid}: {exc}')
            save_state(state_path, state)
            metrics['durationsMs']['markSeen'] = round((time.perf_counter() - t1) * 1000, 2)

        t2 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=parse_jobs) as executor:
            futures = {
                executor.submit(parse_mail_item, item.uid, item.message, dirs['incoming'], dirs['cache'] / 'parsed'):
                item.uid for item in messages
            }
            for future in as_completed(futures):
                candidate = future.result()
                if candidate is not None:
                    parsed_candidates.append(candidate)
        metrics['durationsMs']['parseCandidates'] = round((time.perf_counter() - t2) * 1000, 2)
        metrics['counts']['parsedCandidates'] = len(parsed_candidates)

        t3 = time.perf_counter()
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
        metrics['durationsMs']['prefilter'] = round((time.perf_counter() - t3) * 1000, 2)
        metrics['counts']['reviewCandidates'] = len(review_candidates)
        metrics['counts']['skippedByPrefilter'] = skipped_by_prefilter

        t4 = time.perf_counter()
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
        metrics['durationsMs']['llmReview'] = round((time.perf_counter() - t4) * 1000, 2)
        metrics['counts']['resultsPassed'] = len(results)
    finally:
        try:
            client.logout()
        except Exception:
            pass

    t5 = time.perf_counter()
    remaining_unread = get_remaining_unread(config['mail'])
    metrics['durationsMs']['remainingUnreadLookup'] = round((time.perf_counter() - t5) * 1000, 2)
    processed_mail_list = build_processed_mail_list(messages)
    seen_failure_text = ''
    if seen_failures:
        seen_failure_text = '\n\n⚠️ IMAP 已读设置失败：\n' + '\n'.join(f'- {item}' for item in seen_failures)

    if results:
        summary = build_summary(results)
        candidate_list = build_candidate_list(results)
        zip_path = package_results([r.work_dir for r in results], dirs['outbox'])
        excel_path = build_excel_report(
            messages=messages,
            results=results,
            remaining_unread=remaining_unread,
            skipped_by_prefilter=skipped_by_prefilter,
            outbox_dir=dirs['outbox'],
        )
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
🎯 筛选通过：{len(results)} 人
⏭️ 预筛跳过 LLM：{skipped_by_prefilter} 封
📬 剩余未读：{remaining_unread} 封

已附上：
- Excel 报告（查看详细名单、联系方式、摘要与建议）
- ZIP 资源包（查看原始附件与归档材料）

{summary}{seen_failure_text}

如需继续处理下一批，请继续触发。'''
        if not args.dry_run:
            t6 = time.perf_counter()
            text_send = send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], msg)
            excel_send = send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], '', media=str(excel_path))
            file_send = send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], '', media=str(zip_path))
            metrics['durationsMs']['sendFeishu'] = round((time.perf_counter() - t6) * 1000, 2)
            metrics['messageSend'] = {
                'text': text_send,
                'excel': excel_send,
                'file': file_send,
            }
        else:
            metrics['durationsMs']['sendFeishu'] = 0
        metrics['finishedAt'] = datetime.now().isoformat()
        metrics['durationsMs']['total'] = round((time.perf_counter() - t0) * 1000, 2)
        dump_json(dirs['reports'] / 'last-run-metrics.json', metrics)
        print(msg)
    else:
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
⏭️ 预筛跳过 LLM：{skipped_by_prefilter} 封
📬 剩余未读：{remaining_unread} 封

本轮没有评分在 80 分以上的候选人。{seen_failure_text}

如需继续处理下一批，请继续触发。'''
        if not args.dry_run:
            t6 = time.perf_counter()
            text_send = send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], msg)
            metrics['durationsMs']['sendFeishu'] = round((time.perf_counter() - t6) * 1000, 2)
            metrics['messageSend'] = {
                'text': text_send,
            }
        else:
            metrics['durationsMs']['sendFeishu'] = 0
        metrics['finishedAt'] = datetime.now().isoformat()
        metrics['durationsMs']['total'] = round((time.perf_counter() - t0) * 1000, 2)
        dump_json(dirs['reports'] / 'last-run-metrics.json', metrics)
        print(msg)

    if not args.dry_run:
        build_mail_header_index(config['mail'], dirs['state'], limit=200)

    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
