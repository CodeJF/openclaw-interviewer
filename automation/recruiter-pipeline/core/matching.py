from __future__ import annotations

import re
from typing import Any

from .models import JDEntry, ParsedCandidate

JOB_PROFILES: dict[str, dict[str, Any]] = {
    '供应链采购助理': {
        'must_any': ['采购', '供应商', '跟单', 'ERP', '物料', '供应链'],
        'bonus': ['Excel', '交期', '催货', '物流', '订单', '元器件'],
        'negative': ['Java', 'Android', 'iOS', '测试开发', '审计'],
        'min_years': 2,
        'max_years': 12,
    },
    '软件工程师（画质）': {
        'must_any': ['C++', 'ISP', '图像', '视频', '画质', '嵌入式'],
        'bonus': ['Python', '算法', '背光', 'HDR', '显示', '驱动', '仿真'],
        'negative': ['会计', '采购', '供应商', '发票'],
        'min_years': 3,
        'max_years': 10,
    },
    '应收应付会计': {
        'must_any': ['应收', '应付', '会计', '财务', '发票', '金蝶'],
        'bonus': ['对账', '凭证', '银行流水', '账龄', '总账', '进销存'],
        'negative': ['Java', 'C++', '采购工程师', '画质'],
        'min_years': 1,
        'max_years': 8,
    },
    '品质工程师': {
        'must_any': ['品质', '质量', 'QE', 'QA', '检验', '8D'],
        'bonus': ['客诉', '可靠性', '异常分析', '来料', '制程', 'FMEA'],
        'negative': ['会计', '采购', 'Java', '销售'],
        'min_years': 2,
        'max_years': 12,
    },
    'APP研发主管经理': {
        'must_any': ['APP', 'Android', 'iOS', '蓝牙', '后端', '团队管理'],
        'bonus': ['UniApp', 'Vue', 'Golang', 'Java', 'IoT', '穿戴', '架构设计'],
        'negative': ['会计', '采购', '应付', '仓储'],
        'min_years': 5,
        'max_years': 15,
    },
    '测试组长（智能穿戴）': {
        'must_any': ['测试', 'APP测试', '自动化测试', 'Python', '智能穿戴'],
        'bonus': ['Monkey', 'Jmeter', 'Postman', '固件', '嵌入式', '测试用例'],
        'negative': ['会计', '采购', '销售'],
        'min_years': 3,
        'max_years': 12,
    },
    'PM（背光显示）': {
        'must_any': ['PM', '产品', '背光', '显示', 'Mini BLU', '原厂'],
        'bonus': ['市场调研', '推广', 'Design In', '英文', '半导体'],
        'negative': ['会计', '采购助理', 'Java'],
        'min_years': 2,
        'max_years': 10,
    },
    '大客户经理（TV）': {
        'must_any': ['销售', '客户', 'TV', 'MNT', '背光', '电子元器件'],
        'bonus': ['项目立项', '回款', '市场', '大客户', 'Mini LED'],
        'negative': ['会计', '采购助理', '软件开发'],
        'min_years': 3,
        'max_years': 12,
    },
}


def choose_band(score: int, bands: list[dict[str, Any]]) -> str | None:
    for band in bands:
        if int(band['min']) <= score <= int(band['max']):
            return str(band['name'])
    return None


def estimate_years(text: str) -> int | None:
    matches = re.findall(r'(\d+)\s*年', text)
    if not matches:
        return None
    return max(int(x) for x in matches)


def count_hits(text: str, keywords: list[str]) -> tuple[int, list[str]]:
    lowered = text.lower()
    hits: list[str] = []
    for keyword in keywords:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return len(hits), hits


def score_jd_match(text: str, years: int | None, profile: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    score = 0
    details: dict[str, Any] = {}

    must_score, must_hits = count_hits(text, profile.get('must_any', []))
    bonus_score, bonus_hits = count_hits(text, profile.get('bonus', []))
    negative_score, negative_hits = count_hits(text, profile.get('negative', []))

    score += must_score * 15
    score += bonus_score * 5
    score -= negative_score * 10

    min_years = profile.get('min_years')
    max_years = profile.get('max_years')
    if years is not None:
        if min_years is not None and years >= min_years:
            score += 10
        elif min_years is not None:
            score -= 8
        if max_years is not None and years <= max_years:
            score += 3

    details['must_hits'] = must_hits
    details['bonus_hits'] = bonus_hits
    details['negative_hits'] = negative_hits
    details['score'] = score
    return score, details



def prefilter_candidate(
    candidate: ParsedCandidate,
    jds: list[JDEntry],
    *,
    top_k: int = 2,
    min_llm_score: int = 18,
) -> tuple[list[JDEntry], dict[str, Any]]:
    text = candidate.candidate_text
    years = estimate_years(text)
    ranked: list[tuple[int, JDEntry, dict[str, Any]]] = []

    for jd in jds:
        profile = JOB_PROFILES.get(jd.title, {'must_any': [], 'bonus': [], 'negative': []})
        score, details = score_jd_match(text, years, profile)
        ranked.append((score, jd, details))

    ranked.sort(key=lambda item: item[0], reverse=True)
    shortlisted = [jd for score, jd, _ in ranked[:top_k] if score >= min_llm_score]

    top_scores = [
        {
            'jd_title': jd.title,
            'score': score,
            'must_hits': details['must_hits'],
            'bonus_hits': details['bonus_hits'],
            'negative_hits': details['negative_hits'],
        }
        for score, jd, details in ranked[: max(top_k, 3)]
    ]

    should_review = bool(shortlisted)
    reasons = []
    if years is not None:
        reasons.append(f'简历文本识别到约 {years} 年经验')
    if should_review:
        reasons.append(f'规则打分已完成，送审 Top {len(shortlisted)} JD')
    else:
        reasons.append(f'最高规则分低于阈值 {min_llm_score}，跳过 LLM 精筛')

    return shortlisted, {
        'estimated_years': years,
        'prefilter_reasons': reasons,
        'top_jds': [jd.title for jd in shortlisted],
        'top_scores': top_scores,
        'should_review': should_review,
        'min_llm_score': min_llm_score,
    }
