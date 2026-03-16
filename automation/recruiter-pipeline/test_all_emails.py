#!/usr/bin/env python3
"""测试脚本：处理所有邮件（不管已读未读），用于测试 pipeline 性能"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.common import dump_json, load_json
from core.config import DEFAULT_CONFIG
from core.imap_client import connect_imap
from core.io_ops import ensure_runtime_dirs, load_jds
from core.matching import prefilter_candidate
from core.models import MailItem
from core.pipeline_ops import evaluate_candidate
from core.resume_parser import parse_mail_item


def fetch_all_messages(client, max_emails: int = 20):
    """获取所有邮件（不管已读未读）"""
    import email
    import email.policy
    import imaplib
    
    status, _ = client.select('INBOX')
    if status != 'OK':
        raise Exception('Unable to select INBOX')
    
    # 关键：改成 ALL 而不是 UNSEEN
    status, data = client.uid('search', None, 'ALL')
    if status != 'OK':
        raise Exception('Unable to search all messages')
    
    uids = [u.decode() for u in data[0].split() if u]
    uids = list(reversed(uids))
    uids = uids[:max_emails]
    
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
    
    # 获取测试数量（默认 20）
    max_emails = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    
    print(f"🧪 测试模式：获取最近 {max_emails} 封邮件（已读+未读）")
    print(f"⚡ 并发数: {llm_jobs}")
    
    client = connect_imap(config['mail'])
    messages = fetch_all_messages(client, max_emails=max_emails)
    client.logout()
    
    if not messages:
        print("❌ 没有找到邮件")
        return
    
    # 解析简历
    print(f"📝 解析 {len(messages)} 封邮件...")
    parsed = []
    for item in messages:
        candidate = parse_mail_item(item.uid, item.message, dirs['incoming'], dirs['cache'] / 'parsed')
        if candidate:
            parsed.append(candidate)
    
    print(f"✅ 解析完成：{len(parsed)} 个候选人")
    
    # 预筛
    print(f"🔍 预筛中...")
    review_candidates = []
    for candidate in parsed:
        shortlist_jds, prefilter_meta = prefilter_candidate(candidate, jds, top_k=llm_top_k, min_llm_score=min_llm_score)
        if prefilter_meta.get('should_review'):
            review_candidates.append(candidate)
            print(f"  ✓ {candidate.candidate_name or candidate.sender} - 进入 LLM 评审")
        else:
            print(f"  ✗ {candidate.candidate_name or candidate.sender} - 预筛跳过")
    
    print(f"🎯 预筛完成：{len(review_candidates)} 个进入 LLM 评审")
    
    if not review_candidates:
        print("❌ 没有需要 LLM 评审的候选人")
        return
    
    # 检查已有评分，跳过已评分的候选人
    candidates_to_evaluate = []
    for candidate in review_candidates:
        eval_path = dirs['reports'] / 'single-evaluations' / f'{candidate.uid}.json'
        if eval_path.exists():
            try:
                existing = load_json(eval_path)
                print(f"  ⏭️ {candidate.candidate_name or candidate.sender} - 已有评分({existing.get('score', 'N/A')}分)，跳过")
                candidates_to_evaluate.append((candidate, existing))
            except Exception:
                candidates_to_evaluate.append((candidate, None))
        else:
            candidates_to_evaluate.append((candidate, None))
    
    # 分离需要新评分的和已有评分的
    new_candidates = [c for c, e in candidates_to_evaluate if e is None]
    existing_results = [e for c, e in candidates_to_evaluate if e is not None]
    
    if new_candidates:
        print(f"🔄 需要新评分: {len(new_candidates)} 个")
    if existing_results:
        print(f"�复用已有评分: {len(existing_results)} 个")
    
    # LLM 并发评审（只评需要新评分的）
    if new_candidates:
        print(f"🤖 LLM 并发评审中 ({llm_jobs} 个并发)...")
        t0 = time.perf_counter()
        results = []
        errors = []
        
        with ThreadPoolExecutor(max_workers=llm_jobs) as executor:
            futures = {
                executor.submit(evaluate_candidate, candidate, dirs, jds, bands, llm_top_k=llm_top_k, min_llm_score=min_llm_score):
                candidate.uid
                for candidate in new_candidates
            }
            for future in as_completed(futures):
                uid = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    score = result.get('score', 0)
                    name = result.get('candidate_name', uid)
                    print(f"  ✓ {name}: {score}分 - {result.get('matched_jd_title', 'N/A')}")
                except Exception as exc:
                    errors.append(f"{uid}: {exc}")
                    print(f"  ✗ {uid}: {exc}")
        
        t1 = time.perf_counter()
        duration = round(t1 - t0, 1)
    else:
        results = []
        errors = []
        duration = 0
    
    # 合并结果（已有评分 + 新评分）
    all_results = existing_results + results
    
    print(f"\n📊 测试结果:")
    print(f"  处理邮件: {len(messages)} 封")
    print(f"  解析成功: {len(parsed)} 个")
    print(f"  进入评审: {len(review_candidates)} 个")
    print(f"  复用评分: {len(existing_results)} 个")
    print(f"  LLM 完成: {len(results)} 个")
    print(f"  错误: {len(errors)} 个")
    print(f"  总耗时: {duration}秒")
    
    if results:
        passed = [r for r in results if r.get('passed')]
        print(f"  通过筛选: {len(passed)} 个")
    
    # 保存测试报告
    report = {
        'testMode': True,
        'fetchAllEmails': True,
        'timestamp': datetime.now().isoformat(),
        'maxEmails': max_emails,
        'llmJobs': llm_jobs,
        'stats': {
            'totalEmails': len(messages),
            'parsed': len(parsed),
            'reviewCandidates': len(review_candidates),
            'reuseScores': len(existing_results),
            'llmCompleted': len(results),
            'errors': len(errors),
            'durationSeconds': duration,
        },
        'results': all_results,
        'errors': errors,
    }
    report_path = dirs['reports'] / 'test-run-report.json'
    dump_json(report_path, report)
    print(f"\n📄 报告已保存: {report_path}")


if __name__ == '__main__':
    main()
