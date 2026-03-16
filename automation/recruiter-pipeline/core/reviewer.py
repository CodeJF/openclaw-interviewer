"""
Reviewer module - evaluates candidates against job descriptions.

Supports two modes:
1. call_interviewer(): Via OpenClaw Gateway (serial, session-based)
2. call_interviewer_direct(): Direct LLM API call (parallel-capable)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
import uuid
from pathlib import Path
from typing import Any

from .models import JDEntry, ParsedCandidate, PipelineError


# Check for API key in env or config file
def _get_api_key() -> str:
    """Get API key from environment or config file."""
    api_key = os.environ.get('MINIMAX_API_KEY', '')
    if api_key:
        return api_key
    
    # Try loading from config file
    config_path = os.environ.get('RECRUITER_CONFIG', Path(__file__).parent.parent / 'config.local.json')
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
                return config.get('llm', {}).get('apiKey', '')
        except Exception:
            pass
    return ''

_HAS_API_KEY = bool(_get_api_key())

# Feature flag: Use direct LLM API instead of Gateway
# Default: auto-enable if API key is available in env or config
# Set RECRUITER_USE_DIRECT_LLM=0 to disable explicitly
_use_direct_env = os.environ.get('RECRUITER_USE_DIRECT_LLM', '')
if _use_direct_env.lower() in ('0', 'false', 'no'):
    _USE_DIRECT_LLM = False
elif _use_direct_env.lower() in ('1', 'true', 'yes'):
    _USE_DIRECT_LLM = True
else:
    # Auto-detect: use direct mode if API key is available
    _USE_DIRECT_LLM = _HAS_API_KEY

_HAS_API_KEY = bool(_get_api_key())


def trim_jd_content(content: str, limit: int = 2500) -> str:
    normalized = '\n'.join(line.strip() for line in content.splitlines() if line.strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + '\n...[JD内容已截断以提速]'


def build_prompt(candidate: ParsedCandidate, jds: list[JDEntry], prefilter_meta: dict[str, Any]) -> str:
    jd_block = []
    for jd in jds:
        jd_block.append(f'# JD: {jd.title}\n{trim_jd_content(jd.content)}')
    jd_text = '\n\n'.join(jd_block)
    schema = {
        'candidate_name': 'string',
        'matched_jd_title': 'string, must exactly equal one JD title above',
        'route_confidence': 'number 0-1',
        'score': 'integer 0-99',
        'summary': 'short Chinese summary',
        'recommendation': 'short Chinese recommendation',
        'strengths': ['list of strings'],
        'risks': ['list of strings'],
    }
    return textwrap.dedent(
        f'''
        你是专业 AI 面试官。请根据候选人材料，从给定 JD 集合里选择最匹配的岗位并打分。

        额外上下文（来自系统预筛，不是最终结论）：
        {json.dumps(prefilter_meta, ensure_ascii=False)}

        要求：
        1. 只能从给定 JD 中选择一个最匹配岗位。
        2. 优先参考预筛给出的 top_jds，除非候选人材料明确显示另一个 JD 更合适。
        3. 分数范围 0-99。
        4. 80-89 代表较强匹配，90-99 代表高匹配。
        5. 如果不匹配任何岗位，也要选出最接近的一个岗位，但分数可以低于 80。
        6. 只输出 JSON，不要输出 markdown、解释或代码块。

        JSON schema:
        {json.dumps(schema, ensure_ascii=False)}

        邮件主题：{candidate.subject}
        发件人：{candidate.sender}

        候选人材料：
        {candidate.candidate_text}

        JD 集合：
        {jd_text}
        '''
    ).strip()


def _call_interviewer_gateway(prompt: str) -> dict[str, Any]:
    """Original Gateway-based implementation (serial processing)."""
    session_id = f"recruiter-review-{uuid.uuid4()}"
    cmd = [
        'openclaw', 'agent',
        '--agent', 'interviewer',
        '--session-id', session_id,
        '--message', prompt,
        '--json',
        '--timeout', '600',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f'interviewer agent failed: {proc.stderr.strip() or proc.stdout.strip()}')

    stdout = proc.stdout.strip()
    data = json.loads(stdout)
    result_block = data.get('result') if isinstance(data.get('result'), dict) else {}
    agent_meta = result_block.get('meta', {}).get('agentMeta', {}) if isinstance(result_block, dict) else {}
    provider = str(agent_meta.get('provider') or '')
    model = str(agent_meta.get('model') or '')
    if provider == 'openai-codex' or model == 'gpt-5.4':
        raise PipelineError('interviewer agent unexpectedly fell back to OpenAI Codex; refusing result')
    if model and model != 'MiniMax-M2.5':
        raise PipelineError(f'interviewer agent used unexpected model: {model}; expected MiniMax-M2.5')

    content = data.get('reply') or data.get('text') or data.get('message') or ''
    if not content and isinstance(result_block, dict):
        payloads = result_block.get('payloads') or []
        if payloads and isinstance(payloads[0], dict):
            content = payloads[0].get('text') or ''
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    if not content and isinstance(result_block, str):
        content = result_block
    if not content:
        raise PipelineError(f'Unexpected interviewer output: {stdout[:500]}')
    match = re.search(r'\{.*\}', content, re.S)
    if not match:
        raise PipelineError(f'No JSON object found in interviewer output: {content[:500]}')
    return json.loads(match.group(0))


def call_interviewer_direct(prompt: str) -> dict[str, Any]:
    """
    Call LLM directly via API (bypasses Gateway for parallel processing).
    
    This function is used when RECRUITER_USE_DIRECT_LLM=true is set.
    It allows true parallel execution in Pipeline batch processing.
    
    Args:
        prompt: The evaluation prompt
        
    Returns:
        Parsed evaluation result as dict
    """
    from .llm_client import call_llm_with_retry
    
    result = call_llm_with_retry(
        prompt=prompt,
        model='MiniMax-M2.5',
        temperature=0.0,
        max_tokens=4096,
        timeout=600,
    )
    
    # Validate response structure
    required_fields = ['candidate_name', 'matched_jd_title', 'score', 'summary', 'recommendation']
    for field in required_fields:
        if field not in result:
            raise PipelineError(f'Missing required field in LLM response: {field}')
    
    return result


def call_interviewer(prompt: str) -> dict[str, Any]:
    """
    Evaluate a candidate against job descriptions.
    
    Supports two backends (selected via RECRUITER_USE_DIRECT_LLM env var):
    - Direct API call (parallel-capable, recommended for Pipeline)
      Requires: RECRUITER_USE_DIRECT_LLM=1 AND MINIMAX_API_KEY=xxx
    - OpenClaw Gateway (session-based, for interactive use)
    
    Args:
        prompt: The evaluation prompt
        
    Returns:
        Parsed evaluation result as dict
    """
    # Use direct LLM if enabled (default when API key is available)
    if _USE_DIRECT_LLM:
        if not _HAS_API_KEY:
            import warnings
            warnings.warn(
                "Direct LLM mode enabled but no API key found. "
                "Falling back to Gateway mode. "
                "To use direct mode, set MINIMAX_API_KEY in config.local.json"
            )
            return _call_interviewer_gateway(prompt)
        return call_interviewer_direct(prompt)
    
    # Default: use Gateway (session-based)
    return _call_interviewer_gateway(prompt)
