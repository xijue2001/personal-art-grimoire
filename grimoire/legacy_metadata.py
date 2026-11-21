"""图像元数据读取，沿用绘图拾集内置分类器的离线规则。"""
import gzip
import json
import os
import re
import struct
import zlib

# Collector adaptation: cap decompressed metadata from untrusted group files.
def _bounded_metadata_inflate(data, gzip_format=False):
    decoder = zlib.decompressobj(31 if gzip_format else 15)
    result = decoder.decompress(data, 4 * 1024 * 1024 + 1)
    if len(result) > 4 * 1024 * 1024 or not decoder.eof:
        raise ValueError('图片元数据解压后超过上限或内容不完整')
    return result

# =========================================================================
# 分类规则 —— 与 HTML 版保持一致，加新来源在这里加一行即可
# =========================================================================
SOURCE_RULES = [
    ('nai',         re.compile(r'novelai', re.I)),
    ('anima',       re.compile(r'anima', re.I)),
    ('krea2',       re.compile(r'krea', re.I)),
    ('MOSS',        re.compile(r'moss[-_ .]*image|^moss(?:[-_ .]|$)', re.I)),
    ('Illustrious', re.compile(r'illustrious|动漫光影|anishadow', re.I)),
]
TOOL_LABEL = {'nai': 'nai', 'comfyui': 'comfyui', 'webforgeui': 'webui', 'unknown': '未识别'}
# 工具维度要建的文件夹（NovelAI 不建，它在来源里已经是 nai 了）
TOOL_FOLDER = {'comfyui': 'comfyui', 'webforgeui': 'webui'}
# 「其他」里的图按模型再分一层文件夹：命中别名就用别名，否则用模型文件名本身
MODEL_ALIASES = [
    ('NoobAI',     re.compile(r'noob', re.I)),
    ('Flux',       re.compile(r'flux', re.I)),
    ('Qwen-Image', re.compile(r'qwen[-_ ]?image', re.I)),
]
UNKNOWN = '未识别'
OTHER = '其他'
UPSCALE = '高清放大'
IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# =========================================================================
# 一、二进制读取：PNG 文本块 / EXIF UserComment
# =========================================================================
def read_png_text_chunks(path):
    """读取 PNG 的 tEXt / zTXt / iTXt 文本块。非 PNG 返回 None。"""
    out = {}
    try:
        with open(path, 'rb') as f:
            if f.read(8) != b'\x89PNG\r\n\x1a\n':
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack('>I4s', head)
                data = f.read(length)
                f.read(4)                       # CRC 校验位，跳过
                if ctype == b'IEND':
                    break
                if ctype == b'tEXt':
                    i = data.find(b'\x00')
                    if i > 0:
                        out[_txt(data[:i])] = _txt(data[i + 1:])
                elif ctype == b'zTXt':
                    i = data.find(b'\x00')
                    if i > 0 and len(data) > i + 1 and data[i + 1] == 0:
                        try:
                            out[_txt(data[:i])] = _txt(_bounded_metadata_inflate(data[i + 2:]))
                        except Exception:
                            pass
                elif ctype == b'iTXt':
                    i = data.find(b'\x00')
                    if i > 0:
                        key = _txt(data[:i])
                        comp = data[i + 1]
                        rest = data[i + 3:]
                        j = rest.find(b'\x00')
                        if j < 0:
                            continue
                        rest = rest[j + 1:]
                        k = rest.find(b'\x00')
                        if k < 0:
                            continue
                        body = rest[k + 1:]
                        try:
                            if comp == 1:
                                body = _bounded_metadata_inflate(body)
                            out[key] = _txt(body)
                        except Exception:
                            pass
    except Exception:
        return out
    return out


def _txt(b):
    """PNG tEXt 规范是 Latin-1，但实际工具多写 UTF-8，优先按 UTF-8 解。"""
    try:
        return b.decode('utf-8')
    except Exception:
        return b.decode('latin-1', 'replace')


def read_exif_user_comment(path):
    """从 JPEG / WebP 的 EXIF 里取出 UserComment（A1111 系格式存这里）。"""
    try:
        with open(path, 'rb') as f:
            head = f.read()
    except Exception:
        return {}
    tiff = _find_exif_tiff(head)
    if not tiff:
        return {}
    return _parse_tiff(tiff)


def _find_exif_tiff(u8):
    if len(u8) > 4 and u8[0] == 0xFF and u8[1] == 0xD8:            # JPEG
        off = 2
        while off + 4 <= len(u8):
            if u8[off] != 0xFF:
                off += 1
                continue
            marker = u8[off + 1]
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                off += 2
                continue
            if marker in (0xDA, 0xD9):
                break
            size = (u8[off + 2] << 8) | u8[off + 3]
            if marker == 0xE1 and size > 8:
                seg = u8[off + 4:off + 2 + size]
                if seg[:4] == b'Exif':
                    return seg[6:]
            off += 2 + size
        return None
    if len(u8) > 16 and u8[:4] == b'RIFF' and u8[8:12] == b'WEBP':  # WebP
        off = 12
        while off + 8 <= len(u8):
            fourcc = u8[off:off + 4]
            size = int.from_bytes(u8[off + 4:off + 8], 'little')
            if fourcc == b'EXIF':
                s = off + 8
                if u8[s:s + 4] == b'Exif':
                    s += 6
                return u8[s:off + 8 + size]
            if size <= 0:
                break
            off += 8 + size + (size & 1)
        return None
    if len(u8) > 8 and u8[:8] == b'\x89PNG\r\n\x1a\n':              # PNG 的 eXIf 块
        off = 8
        while off + 8 <= len(u8):
            ln = int.from_bytes(u8[off:off + 4], 'big')
            ct = u8[off + 4:off + 8]
            if ct == b'eXIf':
                return u8[off + 8:off + 8 + ln]                     # 块体本身就是 TIFF 头
            if ct == b'IEND' or off + 8 + ln + 4 > len(u8):
                break
            off += 8 + ln + 4
    return None


def _parse_tiff(tiff):
    """遍历 TIFF IFD，取出 UserComment(0x9286) 等标签。"""
    out = {}
    if not tiff or len(tiff) < 8:
        return out
    le = tiff[0:2] == b'II'
    fmt = '<' if le else '>'
    try:
        u16 = lambda o: struct.unpack(fmt + 'H', tiff[o:o + 2])[0]
        u32 = lambda o: struct.unpack(fmt + 'I', tiff[o:o + 4])[0]
    except Exception:
        return out
    TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    seen = set()

    def read_ifd(off):
        if not off or off <= 0 or off + 2 > len(tiff) or off in seen:
            return
        seen.add(off)
        try:
            n = u16(off)
        except Exception:
            return
        for i in range(n):
            e = off + 2 + i * 12
            if e + 12 > len(tiff):
                break
            tag, typ, count = u16(e), u16(e + 2), u32(e + 4)
            size = TYPE_SIZE.get(typ, 1) * count
            val_off = e + 8
            if size > 4:
                val_off = u32(e + 8)
            if size <= 0 or val_off < 0 or val_off + size > len(tiff):
                continue
            if tag == 0x8769:
                read_ifd(u32(e + 8))
            elif tag == 0x9286:
                out['UserComment'] = tiff[val_off:val_off + size]
            elif tag == 0x010F:
                out['Make'] = _txt(tiff[val_off:val_off + size - 1])
            elif tag == 0x0110:
                out['Model'] = _txt(tiff[val_off:val_off + size - 1])
            elif tag == 0x0131:
                out['Software'] = _txt(tiff[val_off:val_off + size - 1])

    if len(tiff) >= 8:
        read_ifd(u32(4))
    return out


def decode_user_comment(raw):
    """UserComment 有字符集前缀和大小端两种坑，这里做兜底解码。"""
    if not raw:
        return ''
    body = raw
    cs = ''
    if len(raw) >= 8:
        cs = raw[:8].replace(b'\x00', b'').decode('latin-1', 'replace').strip().lower()
    if cs in ('unicode', 'ascii', 'jis'):
        body = raw[8:]
    if body[:2] == b'\xff\xfe':
        text = body[2:].decode('utf-16-le', 'replace')
    elif body[:2] == b'\xfe\xff':
        text = body[2:].decode('utf-16-be', 'replace')
    elif cs == 'unicode':
        be = body.decode('utf-16-be', 'replace')
        le = body.decode('utf-16-le', 'replace')
        text = be if _printable_ratio(be) >= _printable_ratio(le) else le
    else:
        text = _txt(body)
    return text.replace('\x00', '').strip()


def _printable_ratio(s):
    if not s:
        return 0
    # Fixed ASCII setting names help distinguish UTF-16 byte orders.
    good = sum(2 if c in '\n\r\t' or 32 <= ord(c) < 127 else
               1 if 0x2000 < ord(c) < 0xFFFD else 0 for c in s)
    return good / len(s)


# =========================================================================
# 一之二、NovelAI 的 alpha 通道隐写（stealth_pngcomp）
#
# NovelAI 从 V4 起可以不把元数据写进 PNG 文本块，而是把「魔数 + 长度 + gzip(JSON)」
# 以比特流形式藏在 alpha 通道的最低有效位里（Comment 里的 straight_alpha 就是它）。
# 布局：'stealth_pngcomp'(15 字节) + 大端 int32（gzip 数据的比特长度）+ gzip 数据。
# 比特取法：扫描顺序是外层 x、内层 y；每 8 个比特按 MSB 优先拼成一个字节。
# =========================================================================
STEALTH_MAGIC = b'stealth_pngcomp'
STEALTH_KEY = '__stealth__'


def read_stealth_metadata(path):
    """从 alpha 通道 LSB 里取出 NovelAI 隐写的元数据；没有就返回 None。"""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError('缺少 Pillow，无法检查 NovelAI 隐写元数据') from exc
    try:
        with Image.open(path) as im:
            if 'A' in im.getbands():
                alpha = im.getchannel('A')
            elif 'transparency' in im.info:
                alpha = im.convert('RGBA').getchannel('A')
            else:
                return None
            # 外层 x、内层 y；也支持调色板透明度保存的 alpha 位。
            seq = alpha.transpose(Image.Transpose.TRANSPOSE).tobytes()
    except Exception:
        return None

    def pack(start_byte, nbytes):
        """从第 start_byte 个 alpha 字节开始，取 nbytes 个字节的比特流。"""
        if start_byte + nbytes * 8 > len(seq):
            return None
        out = bytearray(nbytes)
        for j in range(nbytes):
            base = start_byte + j * 8
            v = 0
            for k in range(8):
                v = (v << 1) | (seq[base + k] & 1)
            out[j] = v
        return bytes(out)

    if pack(0, len(STEALTH_MAGIC)) != STEALTH_MAGIC:
        return None
    hdr = pack(len(STEALTH_MAGIC) * 8, 4)
    if not hdr:
        return None
    nbits = int.from_bytes(hdr, 'big')
    if nbits <= 0 or nbits % 8:
        return None
    blob = pack((len(STEALTH_MAGIC) + 4) * 8, nbits // 8)
    if not blob:
        return None
    try:
        obj = json.loads(_bounded_metadata_inflate(blob, gzip_format=True).decode('utf-8'))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


# =========================================================================
# 二、各写入方的格式解析
# =========================================================================
def parse_a1111(raw):
    """A1111 / Forge / Forge Neo 的 parameters 纯文本格式。"""
    text = str(raw).replace('\r\n', '\n')
    positive, negative, settings_raw = text, '', ''
    neg_idx = text.find('Negative prompt:')
    tail = text[neg_idx + len('Negative prompt:'):] if neg_idx >= 0 else text

    m = re.search(r'(?:^|\n)[ \t]*(Steps:[ \t]*\d[\s\S]*)$', tail)
    if m:
        if neg_idx >= 0:
            positive = text[:neg_idx]
            negative = tail[:m.start()]
        else:
            positive = tail[:m.start()]
        settings_raw = m.group(1)
    elif neg_idx >= 0:
        positive = text[:neg_idx]
        negative = tail
    return {
        'positive': positive.strip(),
        'negative': negative.strip(),
        'settings': parse_settings_line(settings_raw),
    }


def parse_settings_line(s):
    out = {}
    if not s:
        return out
    for part in re.split(r',\s*(?=[A-Za-z][A-Za-z0-9 _/-]{0,40}:)', s):
        i = part.find(':')
        if i <= 0:
            continue
        k = part[:i].strip()
        v = part[i + 1:].strip().strip('"')
        if k and v:
            out[k] = v
    return out


def parse_nai(comment_text, description_text):
    """NovelAI 的 Comment JSON / Description 文本。"""
    j = {}
    t = (comment_text or '').strip()
    if t.startswith('{'):
        try:
            j = json.loads(t)
        except Exception:
            j = {}
    if not isinstance(j, dict):
        j = {}
    if isinstance(j.get('prompt'), dict):                 # 少数版本会多套一层
        j = dict(j['prompt'], **j)
    positive = (j.get('prompt') if isinstance(j.get('prompt'), str) else '') or description_text or ''
    negative = j.get('uc') if isinstance(j.get('uc'), str) else ''
    mapping = {'steps': 'Steps', 'scale': 'CFG scale', 'seed': 'Seed', 'sampler': 'Sampler',
               'width': '宽度', 'height': '高度', 'strength': 'Strength', 'noise': 'Noise'}
    settings = {v: j[k] for k, v in mapping.items() if j.get(k) is not None}
    return {'positive': positive, 'negative': negative, 'settings': settings}


def comfy_analyze(nodes):
    """ComfyUI 的 API prompt JSON：{节点id: {class_type, inputs}}"""
    ckpts, unets, loras, vaes, clips, controlnets = [], [], [], [], [], []
    sampler, latent, save_prefix = None, None, ''

    for n in nodes.values():
        if not isinstance(n, dict):
            continue
        ct = str(n.get('class_type') or '')
        ins = n.get('inputs') or {}
        # Official MOSS-Image HF loader uses model_path, not ckpt_name.
        if ct == 'MOSSImageHFLoader':
            ckpts.append(str(ins.get('model_path') or 'MOSS-Image'))
        for k, v in ins.items():
            if not isinstance(v, str) or not v:
                continue
            if k == 'ckpt_name':
                ckpts.append(v)
            elif k == 'unet_name':
                unets.append(v)
            elif k == 'lora_name':
                loras.append(v)
            elif k == 'vae_name':
                vaes.append(v)
            elif re.fullmatch(r'clip_name\d?', k):
                clips.append(v)
            elif k == 'control_net_name':
                controlnets.append(v)
            elif k == 'filename_prefix':
                save_prefix = v
        if re.search(r'sampler', ct, re.I) and sampler is None and (
                ins.get('positive') is not None or ins.get('negative') is not None):
            sampler = ins
        if re.search(r'Empty.*Latent', ct) and latent is None:
            latent = ins

    settings = {}
    if sampler:
        for k, label in (('steps', 'Steps'), ('cfg', 'CFG scale'), ('sampler_name', 'Sampler'),
                         ('scheduler', 'Schedule type'), ('denoise', 'Denoising strength'), ('seed', 'Seed')):
            v = sampler.get(k)
            if v is not None and not isinstance(v, list):
                settings[label] = v
    if latent and latent.get('width') is not None and latent.get('height') is not None:
        w = '?' if isinstance(latent['width'], list) else latent['width']
        h = '?' if isinstance(latent['height'], list) else latent['height']
        settings['尺寸'] = f'{w}x{h}'

    return {
        'positive': comfy_trace(nodes, (sampler or {}).get('positive'), True),
        'negative': comfy_trace(nodes, (sampler or {}).get('negative'), False),
        'settings': settings,
        'ckpts': ckpts, 'unets': unets, 'loras': loras,
        'other': [f'VAE:{v}' for v in vaes] + [f'CLIP:{v}' for v in clips] + [f'CN:{v}' for v in controlnets],
        'save_prefix': save_prefix,
    }


def comfy_trace(nodes, ref, is_pos, depth=0, seen=None):
    """按极性顺链路找提示词文本；找正向只跟 positive 分支，找负向只跟 negative 分支。"""
    seen = seen if seen is not None else set()
    if depth > 18 or ref is None:
        return ''
    if isinstance(ref, str):
        return ref.strip()
    if not isinstance(ref, list) or not ref:
        return ''
    nid = str(ref[0])
    mark = nid + ('+' if is_pos else '-')
    if mark in seen:                                     # 防环
        return ''
    node = nodes.get(nid)
    if not isinstance(node, dict):
        return ''
    ins = node.get('inputs') or {}
    title = str((node.get('_meta') or {}).get('title') or '')
    looks_neg = re.search(r'negative|负向|负面|反向|uc\b', title, re.I)
    looks_pos = re.search(r'positive|正向|正面', title, re.I)
    if is_pos and looks_neg:
        return ''
    if not is_pos and looks_pos:
        return ''

    for k in ('result', 'text', 'text_0', 'text_g', 'text_l', 'prompt'):
        v = ins.get(k)
        if isinstance(v, str) and v.strip():
            return v
    pol = 'positive' if is_pos else 'negative'
    v = ins.get(pol)
    if isinstance(v, str) and v.strip():
        return v

    nxt = set(seen)
    nxt.add(mark)
    down = []
    if ins.get(pol) is not None:
        down.append(ins[pol])
    for k in ('text', 'conditioning', 'conditioning_1', 'conditioning_2', 'conditioning_from', 'conditioning_to'):
        if ins.get(k) is not None:
            down.append(ins[k])
    for r in down:
        got = comfy_trace(nodes, r, is_pos, depth + 1, nxt)
        if got:
            return got
    for k in ('text_a', 'text_b', 'text_c', 'value'):
        v = ins.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ''


def comfy_fallback(nodes):
    """图追踪失败时的兜底：先按节点标题认正负，再按长度和节点类型猜。"""
    cands = []
    for n in nodes.values():
        if not isinstance(n, dict):
            continue
        ins = n.get('inputs') or {}
        for k in ('result', 'text', 'text_0', 'text_g', 'text_l', 'prompt', 'positive', 'negative'):
            v = ins.get(k)
            if isinstance(v, str) and v.strip():
                cands.append({'t': v, 'title': str((n.get('_meta') or {}).get('title') or ''),
                              'ct': str(n.get('class_type') or '')})
                break
    if not cands:
        return {'positive': '', 'negative': ''}
    neg_re = re.compile(r'negative|负向|负面|反向|uc\b', re.I)
    pos_re = re.compile(r'positive|正向|正面', re.I)
    neg = next((c for c in cands if neg_re.search(c['title']) and not pos_re.search(c['title'])), None)
    pos = next((c for c in cands if pos_re.search(c['title']) and not neg_re.search(c['title']) and c is not neg), None)
    rest = [c for c in cands if c is not neg and c is not pos]
    if pos is None and rest:
        rest.sort(key=lambda c: -len(c['t']))
        pos = rest.pop(0)
    if neg is None and rest:
        clip = [c for c in rest if 'CLIPTextEncode' in c['ct']]
        neg = (clip or rest)[0]
    return {'positive': pos['t'] if pos else '', 'negative': neg['t'] if neg else ''}


def _looks_like_comfy(obj):
    if not isinstance(obj, dict) or not obj:
        return False
    return any(isinstance(v, dict) and ('class_type' in v or 'inputs' in v) for v in obj.values())


EMPTY_LATENT_RE = re.compile(r'Empty.*Latent|Latent.*Empty', re.I)


def is_postprocessing(nodes):
    """判断 ComfyUI 工作流是不是「不生成新图、只处理已有图片」（放大 / 修复 / 局部重绘）。

    依据是找不到任何「空 latent」来源：既没有 EmptyLatent 这类节点，
    也不能有把 latent 尺寸当输入参数的加载器 —— Efficient Loader 这类
    会把 empty_latent_width/height 藏在 inputs 里，只查节点类型会漏判。
    """
    objs = [n for n in nodes.values() if isinstance(n, dict)]
    if not objs:
        return False
    for n in objs:
        ins = n.get('inputs')
        if isinstance(ins, dict) and ('empty_latent_width' in ins or 'empty_latent_height' in ins):
            return False
    return not any(EMPTY_LATENT_RE.search(str(n.get('class_type') or '')) for n in objs)


def _try_json(s):
    if not s:
        return None
    t = str(s).strip()
    if not t.startswith('{'):
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def extract_lora_tags(text):
    return [m.group(1) + (' @' + m.group(2) if m.group(2) else '')
            for m in re.finditer(r'<(?:lora|lyco):([^:>]+)(?::([\d.]+))?>', text or '', re.I)]


def guess_webui_flavor(version, where):
    flavor = 'WebUI'
    if version:
        if re.match(r'neo', version, re.I):
            flavor = 'Forge Neo'
        elif re.match(r'f[\d.]', version, re.I):
            flavor = 'Forge'
        elif re.match(r'v?\d', version):
            flavor = 'A1111'
    return f'{flavor}（{where}{" · " + version if version else ""}）'


def clean_text(s):
    return re.sub(r'^[\s,;]+|[\s,;]+$', '', str(s or '').replace('\r\n', '\n')).strip()


def model_group_name(ckpts):
    """给「其他」里的图取模型分组名。

    只用底模名（先查别名，查不到就用文件名本身）。只有 LoRA 时不建分组 ——
    LoRA 名不代表模型身份，拿它当文件夹名没有意义，这类图直接留在 其他\\ 根下。
    """
    for model in ckpts:
        for name, pat in MODEL_ALIASES:
            if pat.search(model):
                return name
    if ckpts:
        base = os.path.basename(str(ckpts[0]).replace('\\', '/'))
        return re.sub(r'\.(safetensors|ckpt|pt|pth|gguf|bin)$', '', base, flags=re.I).strip()
    return ''


# =========================================================================
# 三、汇总分类
# =========================================================================
def classify(name, chunks, exif):
    chunks = chunks or {}
    exif = exif or {}
    notes = []
    if chunks.get(STEALTH_KEY):
        notes.append('元数据藏在 alpha 通道隐写块里（stealth_pngcomp），不是常规 PNG 文本块')
    ckpts, loras, other_models = [], [], []
    tool = 'unknown'
    tool_detail = positive = negative = ''
    settings = {}
    kind = ''                       # 类型维度：目前只有「高清放大」

    def _finish():
        """收尾：按模型串判定来源。"""
        src = ''
        pool = ckpts + loras
        for key, pat in SOURCE_RULES:
            if any(pat.search(m) for m in (ckpts or loras)):
                src = key
                break
        if tool == 'unknown':
            src = UNKNOWN
        elif not src:
            src = OTHER
        hits = list(dict.fromkeys(pool))
        return {
            'name': name, 'tool': tool, 'tool_detail': tool_detail, 'source': src, 'kind': kind,
            'model_group': model_group_name(ckpts) if src == OTHER else '',
            'models': hits[:6], 'loras': loras, 'other_models': other_models,
            'positive': clean_text(positive), 'negative': clean_text(negative),
            'settings': settings, 'notes': notes,
        }

    comfy_json = _try_json(chunks.get('prompt'))
    software = chunks.get('Software') or exif.get('Software') or ''
    nai_source = str(chunks.get('Source') or '')

    # ---- NovelAI ----
    if re.search(r'novelai', software, re.I) or re.search(r'novelai', nai_source, re.I):
        tool = 'nai'
        r = parse_nai(chunks.get('Comment'), chunks.get('Description'))
        positive, negative, settings = r['positive'], r['negative'], r['settings']
        tool_detail = re.sub(r'^NovelAI Diffusion\s*', 'NAI ', nai_source, flags=re.I) if nai_source else 'NovelAI'
        ckpts.append(nai_source or 'NovelAI Diffusion')
        return _finish()

    # ---- ComfyUI ----
    if _looks_like_comfy(comfy_json):
        tool = 'comfyui'
        a = comfy_analyze(comfy_json)
        p, ng = a['positive'], a['negative']
        if not p or not ng:
            f = comfy_fallback(comfy_json)
            p = p or f['positive']
            ng = ng or f['negative']
        positive, negative, settings = p, ng, a['settings']
        ckpts.extend(a['ckpts'] + a['unets'])
        loras.extend(a['loras'])
        other_models.extend(a['other'])
        tool_detail = f'ComfyUI {len(comfy_json)} 节点'
        if a['save_prefix']:
            notes.append('输出前缀: ' + a['save_prefix'])
        if 'workflow' not in chunks:
            notes.append('无 workflow 块')
        if chunks.get('parameters') or exif.get('UserComment'):
            notes.append('同时含 parameters 文本（可能经 WebUI/Forge 二次放大另存）')
        if is_postprocessing(comfy_json):
            kind = UPSCALE
            notes.append('工作流里没有随机生成节点，判定为对已有图片的后处理（放大/修复/重绘）')
        return _finish()

    # ---- WebUI / Forge / Forge Neo ----
    raw_params = chunks.get('parameters') or chunks.get('Parameters') or exif.get('UserComment')
    if raw_params and not (chunks.get('parameters') or chunks.get('Parameters')) and not re.search(r'(?:^|\n)[ \t]*Steps:[ \t]*\d', str(raw_params)):
        notes.append('EXIF UserComment 为普通文件备注，未识别到生图参数结构')
        raw_params = None
    if raw_params:
        tool = 'webforgeui'
        r = parse_a1111(raw_params)
        positive, negative, settings = r['positive'], r['negative'], r['settings']
        where = 'PNG parameters' if chunks.get('parameters') or chunks.get('Parameters') else 'EXIF UserComment'
        tool_detail = guess_webui_flavor(settings.get('Version'), where)
        if settings.get('Model'):
            ckpts.append(settings['Model'])
        else:
            notes.append('parameters 里没有 Model 字段，取不到底模名')
        if settings.get('Model hash'):
            notes.append('Model hash: ' + settings['Model hash'])
        if settings.get('Lora hashes'):
            notes.append('Lora hashes: ' + settings['Lora hashes'])
        loras.extend(extract_lora_tags(positive))
        return _finish()

    # ---- 兜底 ----
    aigc = chunks.get('AIGC')
    if aigc:
        try:
            a = json.loads(aigc)
            prod = a.get('ContentProducer') if isinstance(a, dict) else None
            notes.append(f'平台标记: {prod}（未找到可识别的生成参数）' if prod else '存在 AIGC 平台标记')
        except Exception:
            notes.append('存在 AIGC 平台标记')
    if chunks.get('Description'):
        positive = chunks['Description']
        notes.append('仅有 Description 文本，无法判定写入方')
    keys = [k for k in chunks if not k.startswith('__')]
    if keys:
        notes.append('存在的元数据键: ' + ', '.join(keys))
    elif exif.get('UserComment'):
        notes.append('保留原始 EXIF 备注，不作为提示词')
    else:
        notes.append('文本块、EXIF 与 alpha 隐写通道里都没有内容（多为截图 / 社交平台再压缩 / 平台导出）')
    return _finish()


# =========================================================================
# 四、扫描 + 归档
# =========================================================================
