"""Serve the grimoire on this computer only."""
import argparse
from base64 import b64encode
from functools import lru_cache
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import mimetypes
from pathlib import Path
import re
import shutil
import threading
from urllib.parse import parse_qs, urlsplit
import warnings
import webbrowser

from PIL import Image, ImageOps
from .catalog import Catalog, KEY, PAGE_SIZE
from .metadata import MAX_BYTES


PROJECT = Path(__file__).resolve().parents[1]
STATIC = PROJECT / 'site'
PORT = 18770
ASSET_ROUTE = re.compile(r'/media/([a-f0-9]{64})/(cover|thumbnail|original)\Z')
STATIC_FILES = {'/': 'index.html', '/site/app.js': 'app.js', '/site/style.css': 'style.css'}
PREVIEW_LOCK = threading.Lock()
THUMBNAIL_SIZE = (832, 1216)
THUMBNAIL_BACKGROUND = '#e1e8ee'


def known_folder():
    collector = PROJECT.parent / 'QQ爬楼bot' / 'data'
    for candidate in (collector / 'collector' / 'originals', collector / 'originals'):
        if candidate.is_dir():
            return candidate.resolve()
    return None


def shared_image(folder, filename):
    if not folder:
        return None
    root = Path(folder)
    path = root / filename
    try:
        if path.resolve(strict=True) == path and path.is_file() and path.stat().st_size <= MAX_BYTES:
            return path
    except OSError:
        pass
    return None


def shared_preview_paths(digest, thumbnails, previews):
    for folder, name in ((thumbnails, digest + '-s640.jpg'), (thumbnails, digest + '.jpg'),
                         (previews, digest + '.jpg')):
        path = shared_image(folder, name)
        if path:
            yield path


def portrait_bytes(path, *, shared=False):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if shared:
                width, height = image.size
                if image.getexif().get(274, 1) in (5, 6, 7, 8):
                    width, height = height, width
                if width < THUMBNAIL_SIZE[0] and height < THUMBNAIL_SIZE[1]:
                    return None
            ImageOps.exif_transpose(image, in_place=True)
            scale = min(canvas / picture for canvas, picture in zip(THUMBNAIL_SIZE, image.size))
            fitted = tuple(max(1, round(picture * scale)) for picture in image.size)
            with image.convert('RGBA') as rgba, rgba.resize(fitted, Image.Resampling.LANCZOS) as resized:
                with Image.new('RGB', THUMBNAIL_SIZE, THUMBNAIL_BACKGROUND) as frame, BytesIO() as output:
                    offset = tuple((canvas - picture) // 2 for canvas, picture in zip(frame.size, resized.size))
                    frame.paste(resized, offset, resized)
                    frame.save(output, format='JPEG', quality=86)
                    return output.getvalue()


@lru_cache(maxsize=32)
def portrait_thumbnail(digest, original, thumbnails, previews):
    for path in shared_preview_paths(digest, thumbnails, previews):
        try:
            preview = portrait_bytes(path, shared=True)
        except (OSError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            continue
        if preview is not None:
            return preview
    return portrait_bytes(original)


class AuthenticationRequired(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def authorize(self, post=False):
        port = self.server.server_port
        host = self.headers.get('Host', '').lower()
        public = getattr(self.server, 'public', None)
        allowed = {public.netloc.lower()} if public else {f'127.0.0.1:{port}', f'localhost:{port}'}
        if host not in allowed:
            raise PermissionError('请通过法典配置的访问地址打开')
        if not post and self.path == '/health':
            return
        expected = getattr(self.server, 'authorization', None)
        supplied = self.headers.get('Authorization', '').encode('latin-1')
        if expected and not compare_digest(supplied, expected):
            raise AuthenticationRequired
        origin = (public.scheme if public else 'http') + '://' + host
        if post and (self.headers.get('Origin') != origin or
                     self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
            raise PermissionError('请在本站页面操作')

    def send_data(self, payload, kind, status=200, filename='', *, challenge=False):
        length = len(payload) if isinstance(payload, bytes) else payload[1]
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        if challenge:
            self.send_header('WWW-Authenticate', 'Basic realm="Art Grimoire", charset="UTF-8"')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        if isinstance(payload, bytes):
            self.wfile.write(payload)
        else:
            shutil.copyfileobj(payload[0], self.wfile, length=256 * 1024)

    def send_json(self, value, status=200):
        self.send_data(json.dumps(value, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', status)

    def send_file(self, path, filename=''):
        mime = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        if mime.startswith('text/') or mime == 'application/javascript':
            mime += '; charset=utf-8'
        with path.open('rb') as stream:
            size = path.stat().st_size
            self.send_data((stream, size), mime, filename=filename)

    def media(self, digest, kind, download):
        sources, original = self.server.catalog.paths(digest)
        if kind == 'original':
            self.send_file(original, digest + original.suffix.lower() if download else '')
            return
        if kind == 'thumbnail':
            with PREVIEW_LOCK:
                payload = portrait_thumbnail(digest, original, sources['thumbnails'], sources['previews'])
            self.send_data(payload, 'image/jpeg')
            return
        for path in shared_preview_paths(digest, sources['thumbnails'], sources['previews']):
            # Existing collector preview files may just have been refreshed.
            try:
                with Image.open(path) as preview:
                    preview.verify()
                self.send_file(path)
            except OSError:
                continue
            return
        with PREVIEW_LOCK, warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(original) as image, BytesIO() as output:
                image.thumbnail((640, 640), Image.Resampling.LANCZOS)
                with ImageOps.exif_transpose(image).convert('RGB') as preview:
                    preview.save(output, format='JPEG', quality=84)
                payload = output.getvalue()
        self.send_data(payload, 'image/jpeg')

    def api_get(self, path, params):
        catalog = self.server.catalog
        query = params.get('q', [''])[0]
        model = params.get('model', [''])[0][:160]
        mode = params.get('mode', [''])[0]
        try:
            page = max(1, int(params.get('page', ['1'])[0]))
        except ValueError:
            page = 1
        if path == '/health':
            return {'ok': True}
        if path == '/api/state':
            return catalog.state()
        if path == '/api/images':
            style = params.get('style', [''])[0]
            if style and not KEY.fullmatch(style):
                raise ValueError('画师串标识无效')
            try:
                page_size = int(params.get('page_size', [str(PAGE_SIZE)])[0])
            except ValueError:
                page_size = PAGE_SIZE
            return catalog.images(query, model, mode, page, style=style, page_size=page_size)
        if path == '/api/styles':
            return catalog.styles(query, model, mode, page)
        if path == '/api/trash':
            kind = params.get('kind', ['image'])[0]
            if kind == 'image':
                return catalog.images(page=page, in_trash=True)
            if kind == 'style':
                return catalog.styles(page=page, in_trash=True)
            raise ValueError('回收站类型无效')
        if path.startswith('/api/images/'):
            digest = path.removeprefix('/api/images/')
            if KEY.fullmatch(digest):
                return catalog.image(digest)
        if path.startswith('/api/styles/'):
            style = path.removeprefix('/api/styles/')
            if KEY.fullmatch(style):
                result = catalog.styles(model=model, style=style)
                if not result['items']:
                    raise LookupError('画师串未入典')
                entry = result['items'][0]
                cover = catalog.image(entry['digest'])
                recipe = {field: cover[field] for field in ('positive', 'negative', 'raw_positive', 'characters', 'parameters')}
                return {'entry': entry, 'models': result['models'], 'recipe': recipe,
                        'candidates': catalog.images(model=model, page=page, style=style) if 'candidates' in params else None}
        raise LookupError('页面不存在')

    def get(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        params = parse_qs(parsed.query, max_num_fields=20)
        if path in STATIC_FILES:
            self.send_file(STATIC / STATIC_FILES[path])
            return
        match = ASSET_ROUTE.fullmatch(path)
        if match:
            self.media(*match.groups(), download=params.get('download') == ['1'])
            return
        self.send_json(self.api_get(path, params))

    def post(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 32_768:
            raise ValueError('请求内容过长或为空')
        values = json.loads(self.rfile.read(length))
        if not isinstance(values, dict):
            raise ValueError('请求内容格式不正确')
        path = urlsplit(self.path).path
        catalog = self.server.catalog
        if path == '/api/source':
            self.send_json(catalog.save_source(values))
        elif path == '/api/scan':
            self.send_json(catalog.start_scan(), HTTPStatus.ACCEPTED)
        elif path == '/api/edit':
            self.send_json(catalog.update_edit(values.get('kind', ''), values.get('key', ''), values))
        elif path == '/api/trash':
            self.send_json(catalog.set_trash(values.get('kind'), values.get('key'), values.get('trashed')))
        else:
            raise LookupError('页面不存在')

    def handle_request(self, post=False):
        try:
            self.authorize(post)
            self.post() if post else self.get()
        except ConnectionError:
            return
        except AuthenticationRequired:
            self.send_data('请登录个人绘图法典'.encode(), 'text/plain; charset=utf-8',
                           HTTPStatus.UNAUTHORIZED, challenge=True)
        except PermissionError as error:
            self.send_json({'error': str(error)}, HTTPStatus.FORBIDDEN)
        except LookupError as error:
            self.send_json({'error': str(error)}, HTTPStatus.NOT_FOUND)
        except (json.JSONDecodeError, UnicodeError):
            self.send_json({'error': '请求内容格式不正确'}, HTTPStatus.BAD_REQUEST)
        except (ValueError, RuntimeError) as error:
            self.send_json({'error': str(error)}, HTTPStatus.BAD_REQUEST)
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            self.send_json({'error': '文件暂时无法读取，请检查目录或重新扫描'}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self.send_json({'error': '法典遇到读取错误，请重试'}, HTTPStatus.INTERNAL_SERVER_ERROR)
            raise

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(post=True)


def main():
    parser = argparse.ArgumentParser(description='个人绘图法典')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--database', type=Path, default=PROJECT / '.runtime' / 'catalog.sqlite3')
    parser.add_argument('--originals', type=Path)
    parser.add_argument('--previews', type=Path)
    parser.add_argument('--thumbnails', type=Path)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--public-url', help='内网或反向代理上的访问地址')
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--source-marker', default='', help='原图目录中的挂载校验文件名')
    parser.add_argument('--open', action='store_true', help='启动后打开浏览器')
    args = parser.parse_args()

    public = None
    authorization = None
    fixed = None
    if args.listen != '127.0.0.1' or args.public_url or args.password_file:
        if not (args.public_url and args.password_file and args.originals):
            parser.error('服务部署需要 --public-url、--password-file 和 --originals')
        try:
            public = urlsplit(args.public_url)
            port = public.port
            if (public.scheme not in ('http', 'https') or not public.hostname or public.username is not None
                    or public.query or public.fragment or public.path not in ('', '/')
                    or (port is not None and port < 1)):
                raise ValueError
            password = args.password_file.read_text(encoding='utf-8').strip()
            if not 16 <= len(password) <= 256 or not password.isascii():
                raise ValueError
            authorization = b'Basic ' + b64encode(('grimoire:' + password).encode())
        except (OSError, ValueError):
            parser.error('请配置有效的访问地址和 16 至 256 位 ASCII 密码文件')
        fixed = {}
        for role in ('originals', 'previews', 'thumbnails'):
            source = getattr(args, role)
            if source is not None and not source.is_dir():
                parser.error('图片挂载目录不可用')
            fixed[role] = str(source.resolve()) if source else ''
        if args.source_marker and (Path(args.source_marker).name != args.source_marker
                or args.source_marker in ('.', '..')
                or not (Path(fixed['originals']) / args.source_marker).is_file()):
            parser.error('原图挂载标记不可用')
    elif args.previews or args.thumbnails or args.source_marker:
        parser.error('固定图片目录选项适用于带登录的服务部署')

    known = args.originals.resolve() if args.originals else known_folder()
    catalog = Catalog(args.database, known, fixed_source=fixed, source_marker=args.source_marker)
    try:
        server = ThreadingHTTPServer((args.listen, args.port), Handler)
    except OSError:
        if args.open and args.port == PORT and not public:
            from urllib.request import urlopen
            try:
                with urlopen(f'http://127.0.0.1:{PORT}/api/state', timeout=2) as result:
                    state = json.load(result)
                if 'source' in state and 'counts' in state:
                    webbrowser.open(f'http://127.0.0.1:{PORT}/')
                    return
            except (OSError, ValueError):
                pass
        raise
    with server:
        server.catalog = catalog
        server.public = public
        server.authorization = authorization
        url = (public.geturl().rstrip('/') + '/') if public else f'http://127.0.0.1:{server.server_port}/'
        print(f'个人绘图法典 · {url}', flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
