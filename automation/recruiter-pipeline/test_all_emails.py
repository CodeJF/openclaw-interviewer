#!/usr/bin/env python3
"""测试脚本：处理所有邮件（不管已读未读），并验证 Bitable 同步结果。"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.bitable import ensure_bitable_ready, parse_bitable_config, upsert_candidates_to_bitable
from core.common import dump_json, load_json
from core.config import DEFAULT_CONFIG
from core.imap_client import connect_imap
from core.io_ops import ensure_runtime_dirs, load_jds
from core.models import MailItem
from core.pipeline_ops import process_candidate
from core.resume_parser import parse_mail_item


def fetch_all_messages(client, max_emails: int = 20):
    """获取所有邮件（不管已读未读）"""
    import email
    import email.policy

    status, _ = client.select('INBOX')
    if status != 'OK':
        raise Exception('Unable to select INBOX')

    status, data = client.uid('search', None, 'ALL')
    if status != 'OK':
        raise Exception('Unable to search all messages')

    uids = [u.decode() for u in data[0].split() if u]
    uids = list(reversed(uids))[:max_emails]

    print(f"📬 找到 {len(uids)} 封邮件（已读+未读）")

    messages = []
    for uid in uids:
        status, parts = client.uid('fetch', uid, '(RFC822)')
        if status != 'OK' or not parts or not parts[0]:
            continue
        raw = parts[0][1]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        messages.append(MailItem(uid=uid, message=msg))

    return messages


def main():
    config = load_json(Path(DEFAULT_CONFIG))
    runtime_dir = Path(config['pipeline']['runtimeDir'])
    dirs = ensure_runtime_dirs(runtime_dir)
    jds = load_jds(Path(config['pipeline']['jdDir']))
    llm_top_k = int(config['pipeline'].get('llmTopKPerResume', 2) or 2)
    min_llm_score = int(config['pipeline'].get('minLLMScore', 18) or 18)
    bands = config['pipeline']['scoreBands']
    llm_jobs = int(config['pipeline'].get('parallelLLMJobs', 2) or 2)
    max_emails = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    bitable_cfg = parse_bitable_config(config)
    if not bitable_cfg.enabled:
        print('⚠️ 当前配置未启用 bitable.enabled，先在配置里开启后再测试。')
        return 1

    print('🧰 校验/初始化 automation-managed Bitable 资源...')
    init_result = ensure_bitable_ready(config)
    app_token = str(init_result.get('appToken') or '')
    table_id = str(init_result.get('tableId') or '')
    print(f"  mode: {init_result.get('mode')}")
    print(f"  app: {app_token or 'N/A'}")
    print(f"  table: {table_id or 'N/A'}")

    print(f"🧪 测试模式：获取最近 {max_emails} 封邮件（已读+未读）")
    print(f"⚡ 并发数: {llm_jobs}")
    print(f"🗂️ Bitable: {app_token} / {table_id}")

    client = connect_imap(config['mail'])
    messages = fetch_all_messages(client, max_emails=max_emails)
    client.logout()

    if not messages:
        print('❌ 没有找到邮件')
        return 1

    print(f"📝 解析 {len(messages)} 封邮件...")
    parsed = []
    for item in messages:
        candidate = parse_mail_item(item.uid, item.message, dirs['incoming'], dirs['cache'] / 'parsed')
        if candidate:
            parsed.append(candidate)

    print(f"✅ 解析完成：{len(parsed)} 个候选人")
    if not parsed:
        print('❌ 没有可评估的候选人')
        return 1

    print(f"🤖 统一评估中（通过 / 不通过都会保留）... ({llm_jobs} 个并发)")
    t0 = time.perf_counter()
    candidate_results = []
    errors = []

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
                archive_passed=True,
                source_task=bitable_cfg.source_task,
            ): candidate.uid
            for candidate in parsed
        }
        for future in as_completed(futures):
            uid = futures[future]
            try:
                result = future.result()
                candidate_results.append(result)
                status = '通过' if result.passed else '不通过'
                print(f"  ✓ {result.candidate_name}: {result.score}分 - {result.matched_jd_title} - {status}")
            except Exception as exc:
                errors.append(f"{uid}: {exc}")
                print(f"  ✗ {uid}: {exc}")

    duration = round(time.perf_counter() - t0, 1)
    passed = [r for r in candidate_results if r.passed]
    rejected = [r for r in candidate_results if not r.passed]

    print('\n📤 开始同步到 Bitable...')
    bitable_result = upsert_candidates_to_bitable(candidate_results, config)
    print(f"  created: {bitable_result.get('created', 0)}")
    print(f"  updated: {bitable_result.get('updated', 0)}")

    report = {
        'testMode': True,
        'fetchAllEmails': True,
        'timestamp': datetime.now().isoformat(),
        'maxEmails': max_emails,
        'llmJobs': llm_jobs,
        'stats': {
            'totalEmails': len(messages),
            'parsed': len(parsed),
            'evaluated': len(candidate_results),
            'passed': len(passed),
            'rejected': len(rejected),
            'errors': len(errors),
            'durationSeconds': duration,
        },
        'bitableInitialization': init_result,
        'bitable': bitable_result,
        'results': [
            {
                'mail_uid': r.mail_uid,
                'candidate_name': r.candidate_name,
                'matched_jd_title': r.matched_jd_title,
                'score': r.score,
                'passed': r.passed,
                'band': r.band,
                'status': r.status,
                'archive_dir': r.archive_dir,
            }
            for r in candidate_results
        ],
        'errors': errors,
    }
    report_path = dirs['reports'] / 'test-run-report.json'
    dump_json(report_path, report)

    print('\n📊 测试结果:')
    print(f"  处理邮件: {len(messages)} 封")
    print(f"  解析成功: {len(parsed)} 个")
    print(f"  统一评估: {len(candidate_results)} 个")
    print(f"  通过筛选: {len(passed)} 个")
    print(f"  未通过: {len(rejected)} 个")
    print(f"  错误: {len(errors)} 个")
    print(f"  总耗时: {duration}秒")
    print(f"  Bitable created: {bitable_result.get('created', 0)}")
    print(f"  Bitable updated: {bitable_result.get('updated', 0)}")
    print(f"\n📄 报告已保存: {report_path}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
