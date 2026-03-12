from __future__ import annotations

import re
from typing import Any

from .models import JDEntry, ParsedCandidate

KEYWORD_GROUPS: dict[str, list[str]] = {
    '供应链采购助理': ['采购', '供应商', '跟单', 'ERP', 'Excel', '物料', 'PMC', '供应链'],
    '软件工程师（画质）': ['画质', '图像', '视频', 'codec', 'ISP', 'C++', '算法', '渲染'],
    '应收应付会计': ['应收', '应付', '对账', '总账', '财务', '会计', '发票', '税'],
    '品质工程师': ['品质', '质量', 'QE', 'QA', '8D', '客诉', '检验', '可靠性'],
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


def keyword_score(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    score = 0
    for keyword in keywords:
        if keyword.lower() in lowered:
            score += 1
    return score


def prefilter_candidate(candidate: ParsedCandidate, jds: list[JDEntry]) -> tuple[list[JDEntry], dict[str, Any]]:
    text = candidate.candidate_text
    years = estimate_years(text)
    ranked: list[tuple[int, JDEntry]] = []
    reasons: list[str] = []

    for jd in jds:
        keywords = KEYWORD_GROUPS.get(jd.title, [])
        score = keyword_score(text, keywords)
        if score > 0:
            ranked.append((score, jd))

    ranked.sort(key=lambda item: item[0], reverse=True)
    shortlisted = [jd for _, jd in ranked[:3]]
    if ranked:
        reasons.append('关键词初筛命中')
    if years is not None:
        reasons.append(f'简历文本识别到约 {years} 年经验')
    if not shortlisted:
        shortlisted = jds[:1]
        reasons.append('未命中已知关键词，回退到首个 JD 兜底')

    return shortlisted, {
        'estimated_years': years,
        'prefilter_reasons': reasons,
        'top_jds': [jd.title for jd in shortlisted],
    }
