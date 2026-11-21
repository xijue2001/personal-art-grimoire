"""Validate generation metadata without altering received bytes or raw records."""
import json
import re


def object_value(value):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None


def generation_evidence(record):
    chunks = (record.get('raw_metadata') or {}).get('chunks') or {}
    prompt = object_value(chunks.get('prompt'))
    workflow = object_value(chunks.get('workflow'))
    api_nodes = isinstance(prompt, dict) and any(
        isinstance(n, dict) and isinstance(n.get('class_type'), str) and isinstance(n.get('inputs'), dict)
        for n in prompt.values())
    ui_nodes = isinstance(workflow, dict) and isinstance(workflow.get('nodes'), list) and any(
        isinstance(n, dict) and isinstance(n.get('type'), str) and ('inputs' in n or 'widgets_values' in n)
        for n in workflow['nodes'])
    if api_nodes or ui_nodes:
        return {'valid': True, 'reason': '已识别 ComfyUI 节点工作流'}
    settings = record.get('settings') or {}
    generation_keys = {'Steps', 'Seed', 'Sampler', 'CFG scale', 'Model', 'Model hash', 'Schedule type', 'Denoising strength'}
    if record.get('tool') in {'webforgeui', 'nai', 'comfyui'} and any(
            key in generation_keys and value is not None and str(value).strip() for key, value in settings.items()):
        return {'valid': True, 'reason': '已识别结构化生图参数'}
    comment = object_value(chunks.get('Comment'))
    if record.get('tool') == 'nai' and isinstance(comment, dict) and any(comment.get(k) for k in ('prompt', 'uc', 'v4_prompt', 'v4_negative_prompt')):
        return {'valid': True, 'reason': '已识别 NovelAI 提示词结构'}
    if record.get('tool') == 'nai' and (record.get('positive') or record.get('negative')) and re.search(
            'novelai', str(chunks.get('Software', ''))+' '+str(chunks.get('Source', '')), re.I):
        return {'valid': True, 'reason': 'NovelAI 标识与提示词内容相互印证'}
    return {'valid': False, 'reason': '只有普通备注或无法验证的文本，未找到结构化生图参数或工作流'}


def metadata_view(record, valid, reason):
    """Only the presentation is corrected; assets.metadata remains immutable."""
    record['parameter_evidence'] = {'valid': bool(valid), 'reason': reason}
    record['has_parameters'] = bool(valid)
    record['tool'] = display_tool(record, valid)
    if not valid:
        record['unverified_metadata'] = {key: record.get(key) for key in ('positive', 'negative', 'settings') if record.get(key)}
        comment = ((record.get('raw_metadata') or {}).get('exif') or {}).get('UserComment')
        if comment:
            record['unverified_metadata']['文件备注'] = comment
        record['positive'], record['negative'], record['settings'] = '', '', {}
    return record


def display_tool(record, valid):
    raw = record.get('raw_metadata') or {}
    chunks = raw.get('chunks') or {}
    if not valid and record.get('tool') == 'webforgeui' and (raw.get('exif') or {}).get('UserComment') and not (chunks.get('parameters') or chunks.get('Parameters')):
        return 'unknown'
    return record.get('tool', 'unknown')
