#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.bitable import parse_bitable_config, upsert_candidates_to_bitable
from core.common import dump_json, load_json
from core.config import DEFAULT_CONFIG
from core.imap_client import connect_imap, ensure_seen, fetch_unseen_messages, get_remaining_unread
from core.io_ops import build_mail_header_index, ensure_runtime_dirs, load_jds, package_results
from core.models import CandidateResult, MailItem, ParsedCandidate, PipelineError
from core.notifier import build_summary, send_message
from core.pipeline_ops import process_candidate
from core.reporting import build_excel_report
from core.resume_parser import parse_mail_item


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return load_json(state_path)
    return {'processed_uids': []}


def save_state(state_path: Path, state: dict) -> None:
    dump_json(state_path, state)


def send_to_targets(targets: list[str], account: str, message: str, media: str | None = None) -> list[dict]:
    """Send message (and optional media) to multiple Feishu targets."""
    results = []
    for target in targets:
        try:
            if media:
                text_send = send_message('feishu', account, target, message)
                file_send = send_message('feishu', account, target, '', media=media)
                results.append({'target': target, 'text': text_send, 'file': file_send})
            else:
                text_send = send_message('feishu', account, target, message)
                results.append({'target': target, 'text': text_send})
        except Exception as e:
            results.append({'target': target, 'error': str(e)})
    return results


def resolve_output_config(config: dict) -> dict[str, bool]:
    outputs = dict(config.get('pipeline', {}).get('outputs') or {})
    return {
        'archivePassed': bool(outputs.get('archivePassed', True)),
        'excelReport': bool(outputs.get('excelReport', True)),
        'zipPackage': bool(outputs.get('zipPackage', True)),
        'notifyFeishu': bool(outputs.get('notifyFeishu', True)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    config = load_json(Path(args.config))
    feishu_cfg = config['feishu']
    targets = feishu_cfg.get('targetIds', [feishu_cfg.get('targetId')])
    targets = [t for t in targets if t]  # filter out None
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
    output_cfg = resolve_output_config(config)
    bitable_cfg = parse_bitable_config(config)

    client = connect_imap(config['mail'])
    messages: list[MailItem] = []
    seen_failures: list[str] = []
    parsed_candidates: list[ParsedCandidate] = []
    results: list[CandidateResult] = []
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
                    archive_passed=output_cfg['archivePassed'],
                    source_task=bitable_cfg.source_task,
                ): candidate.uid
                for candidate in parsed_candidates
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as exc:
                    error_uid = futures[future]
                    error_dir = dirs['reports'] / 'errors'
                    error_dir.mkdir(parents=True, exist_ok=True)
                    (error_dir / f'{error_uid}.log').write_text(str(exc), encoding='utf-8')
        metrics['durationsMs']['llmReview'] = round((time.perf_counter() - t4) * 1000, 2)
        metrics['counts']['resultsTotal'] = len(results)
        metrics['counts']['resultsPassed'] = len([r for r in results if r.passed])
        metrics['counts']['skippedByPrefilter'] = len([r for r in results if not r.prefilter_passed])
    finally:
        try:
            client.logout()
        except Exception:
            pass

    t5 = time.perf_counter()
    remaining_unread = get_remaining_unread(config['mail'])
    metrics['durationsMs']['remainingUnreadLookup'] = round((time.perf_counter() - t5) * 1000, 2)
    passed_results = [result for result in results if result.passed]
    skipped_by_prefilter = len([result for result in results if not result.prefilter_passed])
    seen_failure_text = ''
    if seen_failures:
        seen_failure_text = '\n\n⚠️ IMAP 已读设置失败：\n' + '\n'.join(f'- {item}' for item in seen_failures)

    bitable_sync = None
    if results and not args.dry_run and bitable_cfg.enabled:
        t_sync = time.perf_counter()
        bitable_sync = upsert_candidates_to_bitable(results, config)
        metrics['durationsMs']['syncBitable'] = round((time.perf_counter() - t_sync) * 1000, 2)
        metrics['bitableSync'] = bitable_sync
    else:
        metrics['durationsMs']['syncBitable'] = 0

    excel_path = build_excel_report(
        messages=messages,
        results=passed_results,
        remaining_unread=remaining_unread,
        skipped_by_prefilter=skipped_by_prefilter,
        outbox_dir=dirs['outbox'],
    ) if passed_results and output_cfg['excelReport'] else None
    zip_path = package_results(
        [r.work_dir for r in passed_results if r.work_dir is not None],
        dirs['outbox'],
    ) if passed_results and output_cfg['zipPackage'] else None

    if passed_results:
        summary = build_summary(passed_results)
        msg = f'''📊 简历筛选完成

✅ 本次处理：{len(messages)} 封
🎯 筛选通过：{len(passed_results)} 人
⏭️ 预筛跳过 LLM：{skipped_by_prefilter} 封
📬 剩余未读：{remaining_unread} 封
📋 总评估结果：{len(results)} 人

{summary}{seen_failure_text}

如需继续处理下一批，请继续触发。'''
        attachment_lines = []
        if excel_path:
            attachment_lines.append('- Excel 报告（查看详细名单、联系方式、摘要与建议）')
        if zip_path:
            attachment_lines.append('- ZIP 资源包（查看原始附件与归档材料）')
        if attachment_lines:
            msg = msg.replace(f'{summary}{seen_failure_text}', f"已附上：\n" + '\n'.join(attachment_lines) + f"\n\n{summary}{seen_failure_text}")

        if not args.dry_run and output_cfg['notifyFeishu']:
            t6 = time.perf_counter()
            if excel_path:
                send_results = send_to_targets(
                    targets,
                    feishu_cfg['replyAccount'],
                    msg,
                    media=str(excel_path)
                )
            else:
                send_results = send_to_targets(targets, feishu_cfg['replyAccount'], msg)
            zip_send = 'disabled'
            if zip_path:
                for target in targets:
                    send_message('feishu', feishu_cfg['replyAccount'], target, '', media=str(zip_path))
                zip_send = 'sent to all targets'
            metrics['durationsMs']['sendFeishu'] = round((time.perf_counter() - t6) * 1000, 2)
            metrics['messageSend'] = {
                'excel': send_results,
                'zip': zip_send,
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
📋 总评估结果：{len(results)} 人

本轮没有评分在 {min_llm_score} 分以上的候选人。{seen_failure_text}

如需继续处理下一批，请继续触发。'''
        if not args.dry_run and output_cfg['notifyFeishu']:
            t6 = time.perf_counter()
            send_results = send_to_targets(targets, feishu_cfg['replyAccount'], msg)
            metrics['durationsMs']['sendFeishu'] = round((time.perf_counter() - t6) * 1000, 2)
            metrics['messageSend'] = {
                'text': send_results,
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
