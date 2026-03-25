from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import CandidateResult, PipelineError
from .notifier import get_feishu_tenant_token, load_feishu_credentials


DEFAULT_FIELD_MAPPING = {
    'mail_uid': '邮件UID',
    'candidate_key': '候选键',
    'candidate_name': '候选人姓名',
    'sender': '发件人',
    'subject': '邮件主题',
    'resume_filename': '简历文件名',
    'phone': '手机号',
    'email': '邮箱',
    'years_of_experience': '工作年限',
    'matched_jd_title': '匹配岗位',
    'score': '分数',
    'band': '分档',
    'passed': '是否通过',
    'fail_reason': '未通过原因',
    'prefilter_passed': '预筛是否通过',
    'summary': '推荐摘要',
    'recommendation': '推荐建议',
    'first_processed_at': '首次处理时间',
    'updated_at': '最近处理时间',
    'source_task': '来源任务',
    'status': '当前状态',
    'notified': '是否已通知',
    'notes': '备注',
    'archive_dir': '归档目录',
    'raw_attachment_paths': '原始附件路径',
    'evaluation_json': '评估原始JSON',
}


@dataclass
class BitableConfig:
    enabled: bool
    app_token: str
    table_id: str
    account: str
    unique_field: str
    source_task: str
    field_mapping: dict[str, str]


def parse_bitable_config(config: dict[str, Any]) -> BitableConfig:
    bitable_cfg = dict(config.get('bitable') or {})
    feishu_cfg = dict(config.get('feishu') or {})
    return BitableConfig(
        enabled=bool(bitable_cfg.get('enabled')),
        app_token=str(bitable_cfg.get('appToken') or ''),
        table_id=str(bitable_cfg.get('tableId') or ''),
        account=str(bitable_cfg.get('account') or feishu_cfg.get('replyAccount') or ''),
        unique_field=str(bitable_cfg.get('uniqueField') or '邮件UID'),
        source_task=str(bitable_cfg.get('sourceTask') or 'recruiter-pipeline'),
        field_mapping={**DEFAULT_FIELD_MAPPING, **dict(bitable_cfg.get('fieldMapping') or {})},
    )


def _request_json(url: str, *, token: str, method: str = 'GET', payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _api_base(cfg: BitableConfig) -> str:
    return f'https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg.app_token}/tables/{cfg.table_id}/records'


def _get_access_token(account: str) -> str:
    app_id, app_secret = load_feishu_credentials(account)
    return get_feishu_tenant_token(app_id, app_secret)


def _search_record_by_uid(cfg: BitableConfig, token: str, uid: str) -> dict[str, Any] | None:
    url = _api_base(cfg) + '/search'
    payload = {
        'filter': {
            'conjunction': 'and',
            'conditions': [
                {
                    'field_name': cfg.unique_field,
                    'operator': 'is',
                    'value': [uid],
                }
            ],
        },
    }
    data = _request_json(url, token=token, method='POST', payload=payload)
    if data.get('code') not in (0, None):
        raise PipelineError(f'Failed to search Bitable record: {data}')
    items = data.get('data', {}).get('items') or []
    return items[0] if items else None


def _build_fields(result: CandidateResult, cfg: BitableConfig, existing_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    existing_fields = existing_fields or {}
    mapped = cfg.field_mapping
    first_processed_at = existing_fields.get(mapped['first_processed_at']) or result.processed_at
    return {
        mapped['mail_uid']: result.mail_uid,
        mapped['candidate_key']: result.candidate_key,
        mapped['candidate_name']: result.candidate_name,
        mapped['sender']: result.sender,
        mapped['subject']: result.subject,
        mapped['resume_filename']: result.resume_filename,
        mapped['phone']: result.phone,
        mapped['email']: result.email,
        mapped['years_of_experience']: result.years_of_experience,
        mapped['matched_jd_title']: result.matched_jd_title,
        mapped['score']: result.score,
        mapped['band']: result.band or '',
        mapped['passed']: '是' if result.passed else '否',
        mapped['fail_reason']: result.fail_reason,
        mapped['prefilter_passed']: '是' if result.prefilter_passed else '否',
        mapped['summary']: result.summary,
        mapped['recommendation']: result.recommendation,
        mapped['first_processed_at']: first_processed_at,
        mapped['updated_at']: result.updated_at,
        mapped['source_task']: result.source_task or cfg.source_task,
        mapped['status']: result.status,
        mapped['notified']: '是' if result.notified else '否',
        mapped['notes']: result.notes,
        mapped['archive_dir']: result.archive_dir,
        mapped['raw_attachment_paths']: '\n'.join(result.raw_attachment_paths),
        mapped['evaluation_json']: result.evaluation_json,
    }


def upsert_candidates_to_bitable(results: list[CandidateResult], config: dict[str, Any]) -> dict[str, Any]:
    cfg = parse_bitable_config(config)
    if not cfg.enabled:
        return {'enabled': False, 'created': 0, 'updated': 0, 'items': []}
    if not cfg.app_token or not cfg.table_id or not cfg.account:
        raise PipelineError('Bitable is enabled but appToken/tableId/account is missing')

    token = _get_access_token(cfg.account)
    base_url = _api_base(cfg)
    summary = {'enabled': True, 'created': 0, 'updated': 0, 'items': []}
    for result in results:
        existing = _search_record_by_uid(cfg, token, result.mail_uid)
        existing_fields = dict(existing.get('fields') or {}) if existing else {}
        fields = _build_fields(result, cfg, existing_fields=existing_fields)
        if existing:
            record_id = str(existing.get('record_id') or '')
            url = f'{base_url}/{urllib.parse.quote(record_id)}'
            data = _request_json(url, token=token, method='PUT', payload={'fields': fields})
            if data.get('code') not in (0, None):
                raise PipelineError(f'Failed to update Bitable record: {data}')
            summary['updated'] += 1
            summary['items'].append({'uid': result.mail_uid, 'action': 'updated', 'recordId': record_id})
        else:
            data = _request_json(base_url, token=token, method='POST', payload={'fields': fields})
            if data.get('code') not in (0, None):
                raise PipelineError(f'Failed to create Bitable record: {data}')
            record_id = str(data.get('data', {}).get('record', {}).get('record_id') or '')
            summary['created'] += 1
            summary['items'].append({'uid': result.mail_uid, 'action': 'created', 'recordId': record_id})
    return summary
