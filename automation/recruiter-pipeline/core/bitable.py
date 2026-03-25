from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import dump_json, load_json
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

# Feishu 多维表字段类型能力在不同接口版本上存在差异。
# 这里默认全部建成文本字段，优先保证应用身份自动初始化可落地、可回退。
TEXT_FIELD_TYPE = 1


@dataclass
class BitableConfig:
    enabled: bool
    account: str
    unique_field: str
    source_task: str
    field_mapping: dict[str, str]
    init_mode: str
    app_token: str
    table_id: str
    app_name: str
    table_name: str
    folder_token: str
    state_path: Path
    allow_create_app: bool
    allow_create_table: bool
    allow_create_fields: bool


def parse_bitable_config(config: dict[str, Any]) -> BitableConfig:
    bitable_cfg = dict(config.get('bitable') or {})
    feishu_cfg = dict(config.get('feishu') or {})
    init_cfg = dict(bitable_cfg.get('initialization') or {})
    runtime_dir = Path(config.get('pipeline', {}).get('runtimeDir') or '.')
    state_path = Path(init_cfg.get('statePath') or bitable_cfg.get('statePath') or (runtime_dir / 'state' / 'bitable-managed.json'))
    return BitableConfig(
        enabled=bool(bitable_cfg.get('enabled')),
        account=str(bitable_cfg.get('account') or feishu_cfg.get('replyAccount') or ''),
        unique_field=str(bitable_cfg.get('uniqueField') or DEFAULT_FIELD_MAPPING['mail_uid']),
        source_task=str(bitable_cfg.get('sourceTask') or 'recruiter-pipeline'),
        field_mapping={**DEFAULT_FIELD_MAPPING, **dict(bitable_cfg.get('fieldMapping') or {})},
        init_mode=str(init_cfg.get('mode') or ('manual' if bitable_cfg.get('appToken') and bitable_cfg.get('tableId') else 'automationManaged')),
        app_token=str(init_cfg.get('appToken') or bitable_cfg.get('appToken') or ''),
        table_id=str(init_cfg.get('tableId') or bitable_cfg.get('tableId') or ''),
        app_name=str(init_cfg.get('appName') or bitable_cfg.get('appName') or 'Recruiter Pipeline'),
        table_name=str(init_cfg.get('tableName') or bitable_cfg.get('tableName') or 'Candidates'),
        folder_token=str(init_cfg.get('folderToken') or bitable_cfg.get('folderToken') or ''),
        state_path=state_path,
        allow_create_app=bool(init_cfg.get('allowCreateApp', True)),
        allow_create_table=bool(init_cfg.get('allowCreateTable', True)),
        allow_create_fields=bool(init_cfg.get('allowCreateFields', True)),
    )


def _request_json(url: str, *, token: str, method: str = 'GET', payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='ignore')
        raise PipelineError(f'HTTP {exc.code} calling {url}: {body or exc.reason}') from exc


def _api_base(cfg: BitableConfig) -> str:
    return f'https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg.app_token}/tables/{cfg.table_id}/records'


def _app_base(app_token: str = '') -> str:
    suffix = f'/{app_token}' if app_token else ''
    return f'https://open.feishu.cn/open-apis/bitable/v1/apps{suffix}'


def _table_base(app_token: str) -> str:
    return f'{_app_base(app_token)}/tables'


def _field_base(app_token: str, table_id: str) -> str:
    return f'{_table_base(app_token)}/{table_id}/fields'


def _get_access_token(account: str) -> str:
    app_id, app_secret = load_feishu_credentials(account)
    return get_feishu_tenant_token(app_id, app_secret)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except Exception as exc:
        raise PipelineError(f'Failed to load Bitable state file {path}: {exc}') from exc


def _save_state(path: Path, data: dict[str, Any]) -> None:
    dump_json(path, data)


def _resolve_bitable_config(cfg: BitableConfig) -> BitableConfig:
    state = _load_state(cfg.state_path)
    resource = dict(state.get('resource') or {})
    if not cfg.app_token:
        cfg.app_token = str(resource.get('appToken') or '')
    if not cfg.table_id:
        cfg.table_id = str(resource.get('tableId') or '')
    return cfg


def _store_resource_state(cfg: BitableConfig, *, app_token: str, table_id: str, initialization: dict[str, Any]) -> None:
    state = {
        'resource': {
            'mode': cfg.init_mode,
            'account': cfg.account,
            'appName': cfg.app_name,
            'tableName': cfg.table_name,
            'appToken': app_token,
            'tableId': table_id,
        },
        'fieldMapping': cfg.field_mapping,
        'initialization': initialization,
    }
    _save_state(cfg.state_path, state)


def _assert_success(data: dict[str, Any], message: str) -> None:
    if data.get('code') not in (0, None):
        raise PipelineError(f'{message}: {data}')


def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data.get('data', {}).get('items') or [])


def _find_by_name(items: list[dict[str, Any]], key: str, name: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get(key) or '').strip() == name:
            return item
    return None


def _list_tables(app_token: str, token: str) -> list[dict[str, Any]]:
    data = _request_json(_table_base(app_token), token=token, method='GET')
    _assert_success(data, 'Failed to list Bitable tables')
    return _extract_items(data)


def _list_fields(app_token: str, table_id: str, token: str) -> list[dict[str, Any]]:
    data = _request_json(_field_base(app_token, table_id), token=token, method='GET')
    _assert_success(data, 'Failed to list Bitable fields')
    return _extract_items(data)


def _create_app(cfg: BitableConfig, token: str) -> tuple[str, str]:
    if not cfg.allow_create_app:
        raise PipelineError(
            'Bitable automation-managed init needs an appToken, but allowCreateApp=false and no reusable appToken was found.'
        )
    payload: dict[str, Any] = {'name': cfg.app_name}
    if cfg.folder_token:
        payload['folder_token'] = cfg.folder_token
    data = _request_json(_app_base(), token=token, method='POST', payload=payload)
    _assert_success(
        data,
        'Failed to create Bitable app via application identity. Check whether the current Feishu app has OpenAPI capability to create Bitable resources',
    )
    app_token = str(
        data.get('data', {}).get('app', {}).get('app_token')
        or data.get('data', {}).get('app_token')
        or ''
    )
    default_table_id = str(
        data.get('data', {}).get('app', {}).get('default_table_id')
        or data.get('data', {}).get('default_table_id')
        or ''
    )
    if not app_token:
        raise PipelineError(
            'Bitable app creation returned no app_token. The create-app capability may be unsupported for the current tenant/app.'
        )
    return app_token, default_table_id


def _ensure_app(cfg: BitableConfig, token: str, init_report: dict[str, Any]) -> str:
    if cfg.app_token:
        init_report['app'] = {'action': 'configured', 'appToken': cfg.app_token}
        return cfg.app_token
    app_token, default_table_id = _create_app(cfg, token)
    init_report['app'] = {
        'action': 'created',
        'appToken': app_token,
        'defaultTableId': default_table_id or None,
    }
    cfg.app_token = app_token
    if default_table_id and not cfg.table_id:
        cfg.table_id = default_table_id
    return app_token


def _create_table(cfg: BitableConfig, token: str, app_token: str) -> str:
    if not cfg.allow_create_table:
        raise PipelineError(
            'Bitable automation-managed init needs a tableId, but allowCreateTable=false and no reusable tableId was found.'
        )
    payload = {'table': {'name': cfg.table_name}}
    data = _request_json(_table_base(app_token), token=token, method='POST', payload=payload)
    _assert_success(
        data,
        'Failed to create Bitable table via application identity. Check table creation capability and app permissions',
    )
    table_id = str(
        data.get('data', {}).get('table_id')
        or data.get('data', {}).get('table', {}).get('table_id')
        or ''
    )
    if not table_id:
        raise PipelineError(
            'Bitable table creation returned no table_id. The current OpenAPI capability may not support application-managed table creation.'
        )
    return table_id


def _ensure_table(cfg: BitableConfig, token: str, app_token: str, init_report: dict[str, Any]) -> str:
    if cfg.table_id:
        init_report['table'] = {'action': 'configured', 'tableId': cfg.table_id, 'name': cfg.table_name}
        return cfg.table_id
    tables = _list_tables(app_token, token)
    existing = _find_by_name(tables, 'name', cfg.table_name)
    if existing:
        table_id = str(existing.get('table_id') or '')
        if table_id:
            cfg.table_id = table_id
            init_report['table'] = {'action': 'reused', 'tableId': table_id, 'name': cfg.table_name}
            return table_id
    table_id = _create_table(cfg, token, app_token)
    cfg.table_id = table_id
    init_report['table'] = {'action': 'created', 'tableId': table_id, 'name': cfg.table_name}
    return table_id


def _create_field(app_token: str, table_id: str, token: str, field_name: str) -> str:
    payload = {'field_name': field_name, 'type': TEXT_FIELD_TYPE}
    data = _request_json(_field_base(app_token, table_id), token=token, method='POST', payload=payload)
    _assert_success(
        data,
        f'Failed to create Bitable field "{field_name}". The current API permissions or field schema contract may be unsupported',
    )
    field_id = str(
        data.get('data', {}).get('field', {}).get('field_id')
        or data.get('data', {}).get('field_id')
        or ''
    )
    if not field_id:
        raise PipelineError(f'Bitable field "{field_name}" creation returned no field_id.')
    return field_id


def _ensure_fields(cfg: BitableConfig, token: str, app_token: str, table_id: str, init_report: dict[str, Any]) -> None:
    existing_fields = _list_fields(app_token, table_id, token)
    existing_names = {str(item.get('field_name') or '').strip() for item in existing_fields}
    created_fields: list[str] = []
    missing = [name for name in cfg.field_mapping.values() if name not in existing_names]
    if missing and not cfg.allow_create_fields:
        raise PipelineError(
            'Bitable fields are incomplete but allowCreateFields=false. Missing fields: ' + ', '.join(missing)
        )
    for field_name in missing:
        _create_field(app_token, table_id, token, field_name)
        created_fields.append(field_name)
    init_report['fields'] = {
        'existingCount': len(existing_fields),
        'created': created_fields,
        'missingAfterInit': [],
    }


def ensure_bitable_ready(config: dict[str, Any]) -> dict[str, Any]:
    cfg = _resolve_bitable_config(parse_bitable_config(config))
    if not cfg.enabled:
        return {'enabled': False, 'mode': cfg.init_mode}
    if not cfg.account:
        raise PipelineError('Bitable is enabled but account is missing')

    token = _get_access_token(cfg.account)
    init_report: dict[str, Any] = {
        'enabled': True,
        'mode': cfg.init_mode,
        'statePath': str(cfg.state_path),
    }

    if cfg.init_mode == 'manual':
        if not cfg.app_token or not cfg.table_id:
            raise PipelineError('Bitable manual mode requires appToken and tableId')
        app_token = cfg.app_token
        table_id = cfg.table_id
        init_report['app'] = {'action': 'configured', 'appToken': app_token}
        init_report['table'] = {'action': 'configured', 'tableId': table_id}
        _ensure_fields(cfg, token, app_token, table_id, init_report)
        _store_resource_state(cfg, app_token=app_token, table_id=table_id, initialization=init_report)
        return {
            **init_report,
            'account': cfg.account,
            'appToken': app_token,
            'tableId': table_id,
            'fieldMapping': cfg.field_mapping,
            'sourceTask': cfg.source_task,
            'uniqueField': cfg.unique_field,
        }

    app_token = _ensure_app(cfg, token, init_report)
    table_id = _ensure_table(cfg, token, app_token, init_report)
    _ensure_fields(cfg, token, app_token, table_id, init_report)
    _store_resource_state(cfg, app_token=app_token, table_id=table_id, initialization=init_report)
    return {
        **init_report,
        'account': cfg.account,
        'appToken': app_token,
        'tableId': table_id,
        'fieldMapping': cfg.field_mapping,
        'sourceTask': cfg.source_task,
        'uniqueField': cfg.unique_field,
    }


def _search_record_by_uid(cfg: BitableConfig, token: str, uid: str) -> dict[str, Any] | None:
    url = _api_base(cfg)
    page_token = None
    while True:
        query = urllib.parse.urlencode({'page_size': 500, **({'page_token': page_token} if page_token else {})})
        data = _request_json(url + f'?{query}', token=token, method='GET')
        _assert_success(data, 'Failed to list Bitable records for UID lookup')
        items = _extract_items(data)
        for item in items:
            fields = dict(item.get('fields') or {})
            if str(fields.get(cfg.unique_field) or '') == str(uid):
                return item
        page_token = data.get('data', {}).get('page_token')
        has_more = bool(data.get('data', {}).get('has_more'))
        if not has_more or not page_token:
            return None


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
        mapped['score']: str(result.score),
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
    ready = ensure_bitable_ready(config)
    if not ready.get('enabled'):
        return {'enabled': False, 'created': 0, 'updated': 0, 'items': []}

    cfg = parse_bitable_config(config)
    cfg.app_token = str(ready.get('appToken') or '')
    cfg.table_id = str(ready.get('tableId') or '')
    cfg.account = str(ready.get('account') or cfg.account)
    cfg.unique_field = str(ready.get('uniqueField') or cfg.unique_field)
    cfg.field_mapping = dict(ready.get('fieldMapping') or cfg.field_mapping)
    cfg.source_task = str(ready.get('sourceTask') or cfg.source_task)
    if not cfg.app_token or not cfg.table_id or not cfg.account:
        raise PipelineError('Bitable initialization did not resolve appToken/tableId/account')

    token = _get_access_token(cfg.account)
    base_url = _api_base(cfg)
    summary = {
        'enabled': True,
        'mode': ready.get('mode'),
        'appToken': cfg.app_token,
        'tableId': cfg.table_id,
        'initialization': ready,
        'created': 0,
        'updated': 0,
        'items': [],
    }
    for result in results:
        existing = _search_record_by_uid(cfg, token, result.mail_uid)
        existing_fields = dict(existing.get('fields') or {}) if existing else {}
        fields = _build_fields(result, cfg, existing_fields=existing_fields)
        if existing:
            record_id = str(existing.get('record_id') or '')
            url = f'{base_url}/{urllib.parse.quote(record_id)}'
            data = _request_json(url, token=token, method='PUT', payload={'fields': fields})
            _assert_success(data, 'Failed to update Bitable record')
            summary['updated'] += 1
            summary['items'].append({'uid': result.mail_uid, 'action': 'updated', 'recordId': record_id})
        else:
            data = _request_json(base_url, token=token, method='POST', payload={'fields': fields})
            _assert_success(data, 'Failed to create Bitable record')
            record_id = str(data.get('data', {}).get('record', {}).get('record_id') or '')
            summary['created'] += 1
            summary['items'].append({'uid': result.mail_uid, 'action': 'created', 'recordId': record_id})
    return summary
