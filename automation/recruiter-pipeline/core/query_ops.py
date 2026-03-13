from __future__ import annotations

import email.header
import email.utils
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .common import load_json
from .imap_client import connect_imap
from .io_ops import load_jds
from .notifier import send_message


@dataclass
class ProcessedCandidateRecord:
    candidate_name: str
    matched_jd_title: str
    score: int
    band: str
    summary: str
    recommendation: str
    sender: str
    subject: str
    uid: str
    date: str
    work_dir: str


JD_ALIASES = {
    'app主管': 'APP研发主管经理',
    'app 主管': 'APP研发主管经理',
    'app研发主管': 'APP研发主管经理',
    'app经理': 'APP研发主管经理',
    '研发主管': 'APP研发主管经理',
    '采购助理': '供应链采购助理',
    '采购': '供应链采购助理',
    '会计': '应收应付会计',
    '应收应付': '应收应付会计',
    '画质': '软件工程师（画质）',
    '品质': '品质工程师',
    '测试组长': '测试组长（智能穿戴）',
    '背光pm': 'PM（背光显示）',
    '大客户经理': '大客户经理（TV）',
}


def normalize_jd_query(text: str, jd_dir: Path | None = None) -> str | None:
    raw = text.strip().lower()
    for alias, target in JD_ALIASES.items():
        if alias in raw:
            return target
    if jd_dir and jd_dir.exists():
        for jd in load_jds(jd_dir):
            if jd.title.lower() in raw or raw in jd.title.lower():
                return jd.title
    return None



def load_processed_candidates(processed_root: Path) -> list[ProcessedCandidateRecord]:
    records: list[ProcessedCandidateRecord] = []
    if not processed_root.exists():
        return records
    for result_path in processed_root.rglob('result.json'):
        try:
            work_dir = result_path.parent
            result = load_json(result_path)
            mail = load_json(work_dir / 'mail.json') if (work_dir / 'mail.json').exists() else {}
            parts = work_dir.parts
            date = parts[-4] if len(parts) >= 4 else ''
            matched_jd_title = str(result.get('matched_jd_title') or parts[-3] if len(parts) >= 3 else '')
            band = str(result.get('band') or parts[-2] if len(parts) >= 2 else '')
            candidate_name = str(result.get('candidate_name') or parts[-1])
            records.append(ProcessedCandidateRecord(
                candidate_name=candidate_name,
                matched_jd_title=matched_jd_title,
                score=int(result.get('score') or 0),
                band=band,
                summary=str(result.get('summary') or ''),
                recommendation=str(result.get('recommendation') or ''),
                sender=str(mail.get('sender') or ''),
                subject=str(mail.get('subject') or ''),
                uid=str(mail.get('uid') or ''),
                date=date,
                work_dir=str(work_dir),
            ))
        except Exception:
            continue
    records.sort(key=lambda r: (r.date, r.score), reverse=True)
    return records



def search_processed_candidates(
    records: list[ProcessedCandidateRecord],
    *,
    jd_title: str | None = None,
    keyword: str | None = None,
    keywords: list[str] | None = None,
    min_score: int | None = None,
    date_after: str | None = None,
    limit: int | None = 20,
    offset: int = 0,
    sort_by: str = 'date',
    sort_desc: bool = True,
) -> dict[str, Any]:
    items = records
    if jd_title:
        items = [r for r in items if r.matched_jd_title == jd_title]
    if keyword:
        key = keyword.lower()
        items = [
            r for r in items
            if key in r.candidate_name.lower() or key in r.subject.lower() or key in r.summary.lower()
        ]
    if keywords:
        normalized = [k.strip().lower() for k in keywords if k and k.strip()]
        for key in normalized:
            items = [
                r for r in items
                if key in r.candidate_name.lower()
                or key in r.subject.lower()
                or key in r.summary.lower()
                or key in r.recommendation.lower()
            ]
    if min_score is not None:
        items = [r for r in items if r.score >= min_score]
    if date_after:
        items = [r for r in items if r.date >= date_after]

    if sort_by == 'score':
        items = sorted(items, key=lambda r: (r.score, r.date, r.candidate_name), reverse=sort_desc)
    else:
        items = sorted(items, key=lambda r: (r.date, r.score, r.candidate_name), reverse=sort_desc)

    total = len(items)
    safe_offset = max(offset, 0)
    paged = items[safe_offset:] if safe_offset else items
    shown = paged if limit is None else paged[:limit]
    return {
        'total': total,
        'shown': len(shown),
        'limit': limit,
        'offset': safe_offset,
        'sortBy': sort_by,
        'sortDesc': sort_desc,
        'items': shown,
    }



def decode_mime_header(value: str) -> str:
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or 'utf-8', errors='ignore'))
        else:
            parts.append(chunk)
    return ''.join(parts).strip()



def list_unread_resumes(mail_cfg: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    client = connect_imap(mail_cfg)
    try:
        status, _ = client.select('INBOX')
        if status != 'OK':
            return {'count': -1, 'items': [], 'error': 'Unable to select INBOX'}
        status, data = client.uid('search', None, 'UNSEEN')
        if status != 'OK':
            return {'count': -1, 'items': [], 'error': 'Unable to search UNSEEN'}
        uids = [u.decode() for u in data[0].split() if u]
        items = []
        for uid in list(reversed(uids))[:limit]:
            status, parts = client.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])')
            if status != 'OK' or not parts or not parts[0]:
                continue
            raw = parts[0][1].decode('utf-8', errors='ignore')
            from_match = re.search(r'^From:\s*(.+)$', raw, re.M)
            subject_match = re.search(r'^Subject:\s*(.+)$', raw, re.M)
            sender = decode_mime_header(from_match.group(1).strip()) if from_match else ''
            subject = decode_mime_header(subject_match.group(1).strip()) if subject_match else ''
            name, _addr = email.utils.parseaddr(sender)
            items.append({'uid': uid, 'sender': sender, 'candidate_name': name, 'subject': subject})
        return {'count': len(uids), 'items': items}
    finally:
        try:
            client.logout()
        except Exception:
            pass



def latest_run_summary(report_path: Path) -> dict[str, Any]:
    if not report_path.exists():
        return {'exists': False}
    data = load_json(report_path)
    return {
        'exists': True,
        'startedAt': data.get('startedAt'),
        'finishedAt': data.get('finishedAt'),
        'counts': data.get('counts', {}),
        'durationsMs': data.get('durationsMs', {}),
    }



def run_pipeline_batch(script_path: Path) -> dict[str, Any]:
    proc = subprocess.run(['bash', str(script_path)], capture_output=True, text=True)
    return {
        'returncode': proc.returncode,
        'stdout': proc.stdout.strip(),
        'stderr': proc.stderr.strip(),
    }



def format_candidates(records: list[ProcessedCandidateRecord], *, start: int = 1) -> str:
    if not records:
        return '没有找到符合条件的候选人。'
    lines = []
    for idx, rec in enumerate(records, start=start):
        lines.append(f"{idx}. {rec.candidate_name}｜{rec.matched_jd_title}｜{rec.score}分｜{rec.date}")
    return '\n'.join(lines)



def summarize_jobs(records: list[ProcessedCandidateRecord], *, min_score: int | None = None) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for rec in records:
        if min_score is not None and rec.score < min_score:
            continue
        bucket = grouped.setdefault(rec.matched_jd_title, {
            'jd_title': rec.matched_jd_title,
            'count': 0,
            'highScoreCount': 0,
            'scoreTotal': 0,
            'latestDate': '',
        })
        bucket['count'] += 1
        bucket['scoreTotal'] += rec.score
        if rec.score >= 90:
            bucket['highScoreCount'] += 1
        if rec.date > bucket['latestDate']:
            bucket['latestDate'] = rec.date

    jobs = []
    for bucket in grouped.values():
        count = int(bucket['count'])
        avg_score = round(bucket['scoreTotal'] / count, 1) if count else 0
        jobs.append({
            'jd_title': bucket['jd_title'],
            'count': count,
            'highScoreCount': int(bucket['highScoreCount']),
            'avgScore': avg_score,
            'latestDate': bucket['latestDate'],
        })
    jobs.sort(key=lambda item: (item['count'], item['highScoreCount'], item['avgScore'], item['jd_title']), reverse=True)
    return {
        'jobs': jobs,
        'topJob': jobs[0]['jd_title'] if jobs else None,
    }



def format_job_stats(stats: dict[str, Any]) -> str:
    jobs = stats.get('jobs', [])
    if not jobs:
        return '暂时没有可统计的候选人数据。'
    lines = ['岗位投递统计：']
    for item in jobs:
        lines.append(
            f"- {item['jd_title']}：{item['count']}人，90分以上{item['highScoreCount']}人，平均分{item['avgScore']}"
        )
    if stats.get('topJob'):
        lines.append(f"\n投递最多岗位：{stats['topJob']}")
    return '\n'.join(lines)



def find_candidates_by_name(
    records: list[ProcessedCandidateRecord],
    name: str,
    *,
    jd_title: str | None = None,
    limit: int = 10,
) -> list[ProcessedCandidateRecord]:
    query = name.strip().lower()
    if not query:
        return []
    items = records
    if jd_title:
        items = [r for r in items if r.matched_jd_title == jd_title]

    exact = [r for r in items if r.candidate_name.strip().lower() == query]
    if exact:
        return exact[:limit]

    partial = [
        r for r in items
        if query in r.candidate_name.lower() or query in r.subject.lower() or query in r.sender.lower()
    ]
    return partial[:limit]



def format_candidate_detail(record: ProcessedCandidateRecord) -> str:
    return '\n'.join([
        f'候选人：{record.candidate_name}',
        f'匹配岗位：{record.matched_jd_title}',
        f'评分：{record.score} 分',
        f'分档：{record.band}',
        f'投递时间：{record.date}',
        f'发件人：{record.sender or "未知"}',
        f'邮件主题：{record.subject or "未知"}',
        f'推荐摘要：{record.summary or "暂无"}',
        f'推荐理由：{record.recommendation or "暂无"}',
        f'记录位置：{record.work_dir}',
    ])



def build_candidate_message(records: list[ProcessedCandidateRecord], *, detail: bool = False) -> str:
    if not records:
        return '没有可发送的候选人信息。'
    if detail and len(records) == 1:
        return format_candidate_detail(records[0])

    lines = ['候选人信息如下：']
    for idx, rec in enumerate(records, start=1):
        line = f"{idx}. {rec.candidate_name}｜{rec.matched_jd_title}｜{rec.score}分｜{rec.date}"
        if detail:
            line += f"\n   摘要：{rec.summary or '暂无'}"
        lines.append(line)
    return '\n'.join(lines)



def parse_top_limit(text: str, default: int = 3) -> int:
    lowered = text.lower()
    match = re.search(r'top\s*(\d+)', lowered)
    if match:
        value = int(match.group(1))
        return value if value > 0 else default
    patterns = [
        r'前\s*(\d+)\s*(?:个|人|条)?',
        r'(\d+)\s*(?:个|人|条)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            value = int(m.group(1))
            return value if value > 0 else default
    return default



def build_top_candidates_reply(records: list[ProcessedCandidateRecord], *, jd_title: str | None = None) -> str:
    if not records:
        return '没有找到符合条件的高优先级候选人。'
    title = jd_title or records[0].matched_jd_title
    lines = [f'推荐优先联系候选人（{title}）：']
    for idx, rec in enumerate(records, start=1):
        reason = rec.recommendation or rec.summary or '暂无推荐理由'
        lines.append(f"{idx}. {rec.candidate_name}｜{rec.score}分｜{rec.date}\n   理由：{reason}")
    return '\n'.join(lines)



def parse_candidate_name(text: str, records: list[ProcessedCandidateRecord]) -> str | None:
    sorted_names = sorted({r.candidate_name.strip() for r in records if r.candidate_name.strip()}, key=len, reverse=True)
    for name in sorted_names:
        if name and name in text:
            return name
    explicit = re.search(r'把?\s*([\u4e00-\u9fa5A-Za-z·]{2,20})\s*(?:的)?(?:信息|详情|资料)', text)
    if explicit:
        return explicit.group(1).strip()
    return None



def parse_date_after(text: str) -> str | None:
    if '今天' in text:
        return '2026-03-13'
    if '昨天' in text:
        return '2026-03-12'
    match = re.search(r'最近\s*(\d+)\s*天', text)
    if match:
        days = int(match.group(1))
        mapping = {
            1: '2026-03-13',
            2: '2026-03-12',
            3: '2026-03-11',
            7: '2026-03-07',
        }
        return mapping.get(days)
    return None



def parse_skill_keywords(text: str) -> list[str]:
    known_keywords = [
        '智能穿戴', '蓝牙耳机', '蓝牙', 'wifi', 'iot', '金蝶', '电子料', '采购', 'app', '画质', 'tv',
    ]
    lowered = text.lower()
    hits = []
    for keyword in known_keywords:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return hits



def detect_intent(text: str) -> str:
    lowered = text.lower()
    if '未读' in text and ('简历' in text or '邮件' in text):
        return 'unread'
    if '哪个岗位投递的人最多' in text or '哪个岗位投递最多' in text or '各岗位' in text or '岗位统计' in text:
        return 'job_stats'
    if '发送给我' in text or '发给我' in text or ('发送' in text and ('候选人' in text or '信息' in text or '详情' in text)):
        return 'send'
    if 'top' in lowered or '优先联系' in text or '最合适' in text or '最值得' in text:
        return 'top'
    if '详情' in text or '什么情况' in text or '为什么高分' in text or ('信息' in text and '发送' not in text and '发给我' not in text):
        return 'detail'
    if '继续处理' in text or ('处理' in text and '封' in text):
        return 'run'
    if '最近一次' in text or '刚才' in text or '上次' in text:
        return 'latest'
    if '90分' in text or '高分' in text:
        return 'highscore'
    if '查找' in text or '查' in text or '候选人' in text:
        return 'search'
    return 'unknown'



def parse_limit(text: str, default: int = 20) -> int:
    match = re.search(r'(\d+)\s*封', text)
    return int(match.group(1)) if match else default



def parse_search_limit(text: str, default: int = 20) -> int | None:
    lowered = text.lower()
    if any(token in lowered for token in ['全部', '所有', 'all']):
        return None

    patterns = [
        r'前\s*(\d+)\s*(?:个|人|条)?',
        r'最近\s*(\d+)\s*(?:个|人|条)?',
        r'后\s*(\d+)\s*(?:个|人|条)?',
        r'(\d+)\s*(?:个|人|条)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = int(match.group(1))
            return value if value > 0 else default
    return default



def parse_search_page(text: str) -> int:
    match = re.search(r'第\s*(\d+)\s*页', text, re.I)
    if match:
        return max(int(match.group(1)), 1)
    return 1



def parse_search_offset(text: str, limit: int | None, page: int) -> int:
    if limit is None:
        return 0
    if '下一页' in text:
        return limit
    if '上一页' in text:
        return max(0, limit * (page - 2))
    if '后' in text:
        match = re.search(r'后\s*(\d+)\s*(?:个|人|条)?', text, re.I)
        if match:
            return max(int(match.group(1)), 0)
    return max(0, limit * (page - 1))



def parse_search_sort(text: str) -> tuple[str, bool]:
    lowered = text.lower()
    if '分数最低' in text or '低分' in text:
        return 'score', False
    if '分数最高' in text or '最高分' in text or '高分' in text:
        return 'score', True
    if '最早' in text or '最旧' in text:
        return 'date', False
    if '最新' in text or '最近' in text:
        return 'date', True
    if '按分数' in text:
        return 'score', True
    return 'date', True



def handle_query(text: str, *, config_path: Path) -> dict[str, Any]:
    config = load_json(config_path)
    pipeline_cfg = config['pipeline']
    runtime_dir = Path(pipeline_cfg['runtimeDir'])
    processed_root = runtime_dir / 'processed'
    intent = detect_intent(text)
    records = load_processed_candidates(processed_root)

    if intent == 'unread':
        unread = list_unread_resumes(config['mail'])
        return {
            'intent': intent,
            'data': unread,
            'reply': '当前未读简历：{count} 封\n\n{items}'.format(
                count=unread.get('count', 0),
                items='\n'.join([f"- UID {i['uid']}｜{i.get('candidate_name') or i['sender']}｜{i['subject']}" for i in unread.get('items', [])]) or '暂无样本列表',
            ),
        }

    if intent == 'job_stats':
        stats = summarize_jobs(records)
        return {
            'intent': intent,
            'data': stats,
            'reply': format_job_stats(stats),
        }

    if intent in {'top', 'highscore'}:
        jd_title = normalize_jd_query(text, Path(pipeline_cfg['jdDir']))
        limit = parse_top_limit(text, default=3)
        min_score = 90 if ('90分' in text or '高分' in text) else None
        date_after = parse_date_after(text)
        skill_keywords = parse_skill_keywords(text)
        result = search_processed_candidates(
            records,
            jd_title=jd_title,
            keywords=skill_keywords,
            min_score=min_score,
            date_after=date_after,
            limit=limit,
            offset=0,
            sort_by='score',
            sort_desc=True,
        )
        matches = result['items']
        return {
            'intent': intent,
            'data': {
                'jd_title': jd_title,
                'limit': limit,
                'min_score': min_score,
                'date_after': date_after,
                'keywords': skill_keywords,
                'matches': [asdict(x) for x in matches],
            },
            'reply': build_top_candidates_reply(matches, jd_title=jd_title),
        }

    if intent in {'detail', 'send'}:
        jd_title = normalize_jd_query(text, Path(pipeline_cfg['jdDir']))
        candidate_name = parse_candidate_name(text, records)

        if intent == 'send' and not candidate_name and jd_title:
            limit = parse_top_limit(text, default=3)
            min_score = 90 if ('90分' in text or '高分' in text) else None
            date_after = parse_date_after(text)
            skill_keywords = parse_skill_keywords(text)
            result = search_processed_candidates(
                records,
                jd_title=jd_title,
                keywords=skill_keywords,
                min_score=min_score,
                date_after=date_after,
                limit=limit,
                offset=0,
                sort_by='score',
                sort_desc=True,
            )
            matches = result['items']
            if not matches:
                return {
                    'intent': intent,
                    'data': {'candidate_name': None, 'jd_title': jd_title, 'matches': []},
                    'reply': f'岗位「{jd_title}」暂时没有可发送的候选人。',
                }
            message_text = build_candidate_message(matches, detail=True)
            send_result = send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], message_text)
            return {
                'intent': intent,
                'data': {
                    'candidate_name': None,
                    'jd_title': jd_title,
                    'limit': limit,
                    'min_score': min_score,
                    'date_after': date_after,
                    'keywords': skill_keywords,
                    'matches': [asdict(x) for x in matches],
                    'send': send_result,
                },
                'reply': f'已把岗位「{jd_title}」的候选人信息发送给你。\n\n' + message_text,
            }

        candidates = find_candidates_by_name(records, candidate_name or '', jd_title=jd_title) if candidate_name else []
        if not candidate_name:
            return {
                'intent': intent,
                'data': {'candidate_name': None, 'matches': []},
                'reply': '请告诉我候选人姓名，或直接说“把张三的信息发我”。',
            }
        if not candidates:
            return {
                'intent': intent,
                'data': {'candidate_name': candidate_name, 'matches': []},
                'reply': f'没有找到候选人「{candidate_name}」，你可以再给我岗位名或更完整姓名。',
            }
        if len(candidates) > 1:
            return {
                'intent': intent,
                'data': {'candidate_name': candidate_name, 'matches': [asdict(x) for x in candidates]},
                'reply': f"找到多位候选人「{candidate_name}」，请进一步确认：\n" + format_candidates(candidates),
            }

        target = candidates[0]
        if intent == 'detail':
            return {
                'intent': intent,
                'data': {'candidate_name': candidate_name, 'match': asdict(target)},
                'reply': format_candidate_detail(target),
            }

        message_text = build_candidate_message(candidates, detail=True)
        send_result = send_message('feishu', config['feishu']['replyAccount'], config['feishu']['targetId'], message_text)
        return {
            'intent': intent,
            'data': {
                'candidate_name': candidate_name,
                'match': asdict(target),
                'send': send_result,
            },
            'reply': f'已把候选人「{target.candidate_name}」的信息发送给你。\n\n' + message_text,
        }

    if intent == 'latest':
        latest = latest_run_summary(runtime_dir / 'reports' / 'last-run-metrics.json')
        if not latest.get('exists'):
            return {'intent': intent, 'data': latest, 'reply': '还没有最近一次筛查结果。'}
        counts = latest.get('counts', {})
        durations = latest.get('durationsMs', {})
        return {
            'intent': intent,
            'data': latest,
            'reply': f"最近一次筛查：\n- 开始：{latest.get('startedAt')}\n- 结束：{latest.get('finishedAt')}\n- 读取：{counts.get('messagesFetched', 0)} 封\n- 通过：{counts.get('resultsPassed', 0)} 人\n- 预筛跳过：{counts.get('skippedByPrefilter', 0)} 封\n- 总耗时：{round((durations.get('total', 0) or 0)/1000, 1)} 秒",
        }

    if intent == 'run':
        limit = parse_limit(text, default=int(pipeline_cfg.get('maxEmailsPerRun', 20) or 20))
        result = run_pipeline_batch(Path('/Users/jianfengxu/.openclaw/workspace-interviewer/automation/recruiter-pipeline/run_pipeline.sh'))
        status = '成功' if result['returncode'] == 0 else '失败'
        return {
            'intent': intent,
            'data': result,
            'reply': f'已触发继续处理 {limit} 封，执行{status}。\n\n{result["stdout"][:800] or result["stderr"][:800]}',
        }

    jd_title = normalize_jd_query(text, Path(pipeline_cfg['jdDir']))
    min_score = 90 if intent == 'highscore' else None
    search_limit = parse_search_limit(text, default=20)
    search_page = parse_search_page(text)
    search_offset = parse_search_offset(text, search_limit, search_page)
    sort_by, sort_desc = parse_search_sort(text)
    keyword = None
    if not jd_title:
        cleaned = re.sub(r'[查找帮我是否有的候选人岗位高分前后最近刚才上次全部所有第页个人条按分数最高最低最新最早]+', ' ', text)
        cleaned = re.sub(r'\d+', ' ', cleaned)
        keyword = cleaned.strip() or None
    search_result = search_processed_candidates(
        records,
        jd_title=jd_title,
        keyword=keyword,
        min_score=min_score,
        limit=search_limit,
        offset=search_offset,
        sort_by=sort_by,
        sort_desc=sort_desc,
    )
    matches = search_result['items']
    total = int(search_result['total'])
    shown = int(search_result['shown'])
    raw_limit = search_result['limit']
    limit = int(raw_limit) if raw_limit is not None else None
    offset = int(search_result.get('offset', 0) or 0)
    sort_by = str(search_result.get('sortBy') or 'date')
    sort_desc = bool(search_result.get('sortDesc', True))
    title = jd_title or (f'关键词「{keyword}」' if keyword else '条件')

    if total == 0:
        reply = f"查询结果（{title}）：\n没有找到符合条件的候选人。"
    else:
        sort_label = '按分数' if sort_by == 'score' else '按时间'
        sort_label += '降序' if sort_desc else '升序'

        if shown == 0:
            header = f"查询结果（{title}）：共 {total} 人，当前页无结果，{sort_label}"
            if limit is not None:
                header += f"（偏移 {offset}，每页/本次上限 {limit}）"
            reply = header
        else:
            start_no = offset + 1
            end_no = offset + shown
            header = f"查询结果（{title}）：共 {total} 人，当前第 {start_no}-{end_no} 人，{sort_label}"
            if limit is None:
                header += "，当前展示全部结果"
            elif total > shown + offset:
                header += f"，本页展示 {shown} 人（每页/本次上限 {limit}）"
            reply = header + "\n" + format_candidates(matches)

    return {
        'intent': intent,
        'data': {
            'jd_title': jd_title,
            'keyword': keyword,
            'total': total,
            'shown': shown,
            'limit': limit,
            'offset': offset,
            'sortBy': sort_by,
            'sortDesc': sort_desc,
            'matches': [asdict(x) for x in matches],
        },
        'reply': reply,
    }
