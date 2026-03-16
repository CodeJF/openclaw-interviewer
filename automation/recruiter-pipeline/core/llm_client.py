"""
Direct LLM API client for Pipeline - bypasses Gateway session limitations.

Uses MiniMax M2.5 (Anthropic-compatible API) for parallel processing.

Usage:
    # Option 1: Set in config.local.json
    {
      "llm": {
        "apiKey": "your-api-key",
        "baseUrl": "https://api.minimaxi.com/anthropic"
      },
      "pipeline": { ... }
    }
    
    # Option 2: Set via environment variables
    export MINIMAX_API_KEY="your-api-key"
    export RECRUITER_USE_DIRECT_LLM=1
    
    # Run test
    python test_all_emails.py 10
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from httpx import Timeout

# Load config for API key (optional)
_CONFIG_PATH = os.environ.get('RECRUITER_CONFIG', str(Path(__file__).parent.parent / 'config.local.json'))
_llm_config = {}
if os.path.exists(_CONFIG_PATH):
    try:
        with open(_CONFIG_PATH) as f:
            config = json.load(f)
            _llm_config = config.get('llm', {})
    except Exception:
        pass

# Configuration - priority: env var > config file > defaults
MINIMAX_BASE_URL = os.environ.get('MINIMAX_BASE_URL', _llm_config.get('baseUrl', 'https://api.minimaxi.com/anthropic'))
MINIMAX_API_KEY = os.environ.get('MINIMAX_API_KEY', _llm_config.get('apiKey', ''))
_USE_DIRECT_LLM = os.environ.get('RECRUITER_USE_DIRECT_LLM', '').lower() in ('1', 'true', 'yes')

DEFAULT_MODEL = 'MiniMax-M2.5'
DEFAULT_TIMEOUT = 600  # seconds


def call_llm(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """
    Call MiniMax LLM API directly (Anthropic-compatible).
    
    Args:
        prompt: The prompt to send to the model
        model: Model ID (default: MiniMax-M2.5)
        temperature: Sampling temperature
        max_tokens: Maximum tokens to generate
        timeout: Request timeout in seconds
    
    Returns:
        Parsed JSON response from the model
    
    Raises:
        httpx.HTTPError: On network errors
        PipelineError: On API errors or invalid responses
    """
    from .models import PipelineError
    
    url = f"{MINIMAX_BASE_URL}/v1/messages"
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {MINIMAX_API_KEY}',
        'anthropic-version': '2023-06-01',
    }
    
    payload = {
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [
            {'role': 'user', 'content': prompt}
        ],
    }
    
    with httpx.Client(timeout=Timeout(timeout)) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    
    # Parse response - Anthropic format
    content_blocks = data.get('content', [])
    if not content_blocks:
        raise PipelineError('Empty response from LLM')
    
    # Find text block
    text_content = ''
    for block in content_blocks:
        if block.get('type') == 'text':
            text_content = block.get('text', '')
            break
    
    if not text_content:
        raise PipelineError('No text content in LLM response')
    
    # Extract JSON from response
    # The model might wrap JSON in markdown or plain text
    json_match = re.search(r'\{.*\}', text_content, re.DOTALL)
    if not json_match:
        raise PipelineError(f'No JSON found in LLM response: {text_content[:500]}')
    
    try:
        return json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        raise PipelineError(f'Invalid JSON in LLM response: {e}, content: {json_match.group(0)[:200]}')


def call_llm_with_retry(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_retries: int = 3,
    timeout: int = DEFAULT_TIMEOUT,
    **kwargs,
) -> dict[str, Any]:
    """
    Call LLM with exponential backoff retry.
    
    Args:
        prompt: The prompt to send
        model: Model ID
        max_retries: Maximum retry attempts
        timeout: Request timeout
        **kwargs: Additional arguments passed to call_llm
    
    Returns:
        Parsed JSON response
    """
    from .models import PipelineError
    
    last_error = None
    for attempt in range(max_retries):
        try:
            return call_llm(prompt, model=model, timeout=timeout, **kwargs)
        except (httpx.HTTPError, PipelineError) as e:
            last_error = e
            if attempt < max_retries - 1:
                import time
                wait_time = 2 ** attempt  # 1, 2, 4 seconds
                time.sleep(wait_time)
                continue
    
    raise PipelineError(f'LLM call failed after {max_retries} retries: {last_error}')
