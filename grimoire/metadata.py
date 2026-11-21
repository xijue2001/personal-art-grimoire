"""Read a user's source image while keeping its bytes intact."""
import hashlib
import json
import re
import warnings

from PIL import Image
import pillow_avif  # Register AVIF decoding.
from . import legacy_metadata as legacy
from .generation_evidence import generation_evidence, object_value
from .prompts import extract_artists


Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_BYTES = 64 * 1024 * 1024
EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.avif', '.bmp', '.gif', '.tif', '.tiff'}
TOOLS = {'nai': 'NovelAI', 'comfyui': 'ComfyUI', 'webforgeui': 'WebUI / Forge'}
CONFOUNDERS = ('reference_image', 'reference_image_multiple', 'reference_image_extracted',
               'vibe_transfer', 'director_reference_images', 'image')


def nai_caption(comment, name):
    value = comment.get(name)
    caption = value.get('caption') if isinstance(value, dict) else None
    return caption if isinstance(caption, dict) else {}


def text(value):
    return value if isinstance(value, str) else ''


def slots(caption):
    values = caption.get('char_captions')
    return values[:30] if isinstance(values, list) else []


def nai_fields(chunks, positive, negative, settings):
    comment = object_value(chunks.get('Comment'))
    comment = comment if isinstance(comment, dict) else {}
    base = nai_caption(comment, 'v4_prompt')
    undesired = nai_caption(comment, 'v4_negative_prompt')
    positive = text(base.get('base_caption')) or positive
    negative = text(undesired.get('base_caption')) or negative
    characters = []
    for index in range(max(len(slots(base)), len(slots(undesired)))):
        entry = {'正向': '', '负面': ''}
        for caption, label in ((base, '正向'), (undesired, '负面')):
            values = slots(caption)
            if index < len(values) and isinstance(values[index], dict):
                entry[label] = text(values[index].get('char_caption'))
                if label == '正向' and isinstance(values[index].get('centers'), list):
                    entry['位置'] = values[index]['centers']
        characters.append(entry)
    known_settings = ('model', 'noise_schedule', 'sm', 'sm_dyn', 'cfg_rescale', 'qualityToggle')
    settings.update({k: comment[k] for k in known_settings if k in comment})
    reference = any(comment.get(k) for k in CONFOUNDERS)
    strength = comment.get('strength')
    return positive, negative, characters, bool(reference or (
        isinstance(strength, (int, float)) and 0 < strength < 1))


def inspect(path):
    with path.open('rb') as original:
        digest = hashlib.file_digest(original, 'sha256').hexdigest()
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as img:
            width, height = img.size
            img.verify()
    chunks = legacy.read_png_text_chunks(path) or {}
    stealth = legacy.read_stealth_metadata(path)
    if stealth:
        chunks = {**chunks, **stealth, legacy.STEALTH_KEY: True}
    exif = legacy.read_exif_user_comment(path) or {}
    if exif.get('UserComment'):
        exif['UserComment'] = legacy.decode_user_comment(exif['UserComment'])
    record = legacy.classify(path.name, chunks, exif)
    record['raw_metadata'] = {'chunks': chunks, 'exif': exif}
    evidence = generation_evidence(record)['valid']
    positive = text(record.get('positive')) if evidence else ''
    negative = text(record.get('negative')) if evidence else ''
    settings = dict(record.get('settings') or {}) if evidence else {}
    tool = record.get('tool')
    characters, reference = [], False
    if tool == 'nai':
        positive, negative, characters, reference = nai_fields(chunks, positive, negative, settings)
    style_key, artist_text = extract_artists(positive) if evidence and tool == 'nai' else ('', '')
    models = record.get('models') or []
    model = (
        text(chunks.get('Source')) or text(settings.get('model')) if tool == 'nai'
        else text(settings.get('Model')) or (str(models[0]) if models else '')
    ).strip()[:160]
    return {
        'digest': digest, 'model': model or (TOOLS.get(tool, '') if evidence else '') or '未识别',
        'tool': TOOLS.get(tool, '') if evidence else '', 'width': width, 'height': height,
        'positive': positive, 'negative': negative,
        'raw_positive': text(record.get('positive')) if evidence else '',
        'characters': json.dumps(characters, ensure_ascii=False),
        'parameters': json.dumps(settings, ensure_ascii=False, default=str),
        'style_key': style_key, 'artist_text': artist_text,
        'has_prompt': int(bool(positive or negative or any(c['正向'] or c['负面'] for c in characters))),
        'reference': int(reference),
    }


def search_term(value):
    return re.sub(r'[\s_]+', ' ', value.casefold()).strip()
