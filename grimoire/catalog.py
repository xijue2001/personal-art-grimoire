"""Private SQLite index. The source images are only opened for reading."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from PIL import Image
from .metadata import EXTENSIONS, MAX_BYTES, inspect, search_term


KEY = re.compile(r'[a-f0-9]{64}\Z')
PAGE_SIZE = 24
GALLERY_PAGE_SIZES = (PAGE_SIZE, 48, 72, 96)
NAI_VERSIONS = ('NAI 4.5', 'NAI 5')
STYLE_PREFIX = {'NAI 4.5': 'NAI4.5画师串', 'NAI 5': 'NA5 画师串'}
NAI_MODEL = re.compile(
    r'^(?:novelai|nai)(?:[\s_-]*diffusion)?[\s_-]*v?[\s_-]*(4[._-]5|5)(?![\d.])',
    re.IGNORECASE,
)
ALIASES = {
    '逆光': 'backlighting', '轮廓光': 'rim lighting', '雨': 'rain',
    '夜景': 'night', '水彩': 'watercolor', '长发': 'long hair',
    '蓝眼睛': 'blue eyes', '风景': 'landscape', '樱花': 'cherry blossoms',
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS preferences (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    digest TEXT PRIMARY KEY, model TEXT, tool TEXT, width INTEGER, height INTEGER,
    positive TEXT, negative TEXT, raw_positive TEXT, characters TEXT, parameters TEXT,
    style_key TEXT, artist_text TEXT, has_prompt INTEGER, reference INTEGER
);
CREATE INDEX IF NOT EXISTS asset_style ON assets(style_key, model);
CREATE TABLE IF NOT EXISTS files (
    root TEXT NOT NULL, name TEXT NOT NULL, digest TEXT NOT NULL REFERENCES assets(digest),
    size INTEGER, mtime INTEGER, present INTEGER,
    PRIMARY KEY(root, name)
);
CREATE INDEX IF NOT EXISTS file_digest ON files(root, digest, present);
CREATE TABLE IF NOT EXISTS edits (
    kind TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
    cover TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS trash (
    kind TEXT NOT NULL CHECK (kind IN ('image', 'style')),
    key TEXT NOT NULL, removed INTEGER NOT NULL,
    PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS style_numbers (
    style TEXT NOT NULL,
    version TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY(style, version),
    UNIQUE(version, ordinal)
);
'''

NUMBER_UNNAMED_STYLES = '''
WITH observed AS (
    SELECT style_key, nai_version(model, tool) AS version, MIN(rowid) AS first_seen
    FROM assets WHERE style_key!='' GROUP BY style_key, version
), existing AS (
    SELECT version, MAX(ordinal) AS last FROM style_numbers GROUP BY version
)
INSERT INTO style_numbers (style, version, ordinal)
SELECT o.style_key, o.version,
       COALESCE(e.last, 0) + ROW_NUMBER() OVER (PARTITION BY o.version ORDER BY o.first_seen, o.style_key)
FROM observed o LEFT JOIN existing e USING(version)
WHERE NOT EXISTS (SELECT 1 FROM style_numbers n WHERE n.style=o.style_key AND n.version=o.version)
'''

LIBRARY = '''
WITH locations AS (
    SELECT digest, MAX(present) AS present, MAX(mtime) AS changed
    FROM files WHERE root=? GROUP BY digest
), library AS (
    SELECT assets.*, locations.present, locations.changed,
           nai_version(assets.model, assets.tool) AS model_version,
           ti.removed AS image_removed, ts.removed AS style_removed
    FROM locations JOIN assets USING(digest)
    LEFT JOIN trash ti ON ti.kind='image' AND ti.key=assets.digest
    LEFT JOIN trash ts ON ts.kind='style' AND ts.key=assets.style_key
)
'''

CARD = '''digest, model, model_version, tool, width, height, SUBSTR(positive,1,160) AS excerpt,
          has_prompt, present, changed, style_key'''
ORDER = 'present DESC, changed DESC, digest'
COVER_ORDER = '''present DESC, (digest=COALESCE(e.cover,'')) DESC, reference ASC,
                 (width*height) DESC, digest'''


def nai_version(model, tool):
    if tool != 'NovelAI' or not model:
        return ''
    match = NAI_MODEL.match(model)
    return 'NAI ' + match[1].replace('-', '.').replace('_', '.') if match else ''


def style_name(version, ordinal):
    return f"{STYLE_PREFIX.get(version, 'NAI 画师串')} {ordinal:02d}" if ordinal is not None else ''


def local_dir(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('请输入有效的文件夹路径')
    value = value.strip().strip('"')
    if not value:
        return ''
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('请填写完整的文件夹路径')
    result = path.resolve(strict=True)
    if not result.is_dir():
        raise ValueError('此路径不是文件夹')
    return str(result)


def default_source(known_originals):
    if not known_originals or not known_originals.is_dir():
        return dict(originals='', previews='', thumbnails='')
    siblings = known_originals.parent
    return {
        'originals': str(known_originals),
        **{name: str(siblings / name) if (siblings / name).is_dir() else ''
           for name in ('previews', 'thumbnails')},
    }


def conditions(query, fields):
    checks, values = [], []
    for term in filter(None, map(search_term, re.split('[,，]', query[:500]))):
        exclude = term.startswith('-')
        word = term[1:].strip() if exclude else term
        if not word:
            continue
        keyword = ALIASES.get(word, word)
        checks.append(('NOT ' if exclude else '') + f'(instr(fold({fields}), ?) > 0 OR instr(fold({fields}), ?) > 0)')
        values.extend((word, keyword))
    return checks, values


def where(clauses):
    return ' WHERE ' + ' AND '.join(clauses) if clauses else ''


class Catalog:
    def __init__(self, location, known_originals=None, *, fixed_source=None, source_marker=''):
        self.location = location
        self.fixed_source = fixed_source
        self.source_marker = source_marker
        self.location.parent.mkdir(parents=True, exist_ok=True)
        self.default = default_source(known_originals)
        self.lock = threading.Lock()
        self.progress = {'phase': 'idle'}
        with self.db() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript(SCHEMA)
            connection.execute(NUMBER_UNNAMED_STYLES)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.location, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.create_function('fold', 1, search_term, deterministic=True)
        connection.create_function('nai_version', 2, nai_version, deterministic=True)
        connection.create_function('style_name', 2, style_name, deterministic=True)
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get_source(self, connection):
        if self.fixed_source is not None:
            return self.fixed_source.copy()
        value = connection.execute("SELECT value FROM preferences WHERE name='source'").fetchone()
        return json.loads(value['value']) if value else self.default.copy()

    def source(self):
        with self.db() as connection:
            return self.get_source(connection)

    def source_available(self, root):
        return bool(root and Path(root).is_dir() and (
            not self.source_marker or (Path(root) / self.source_marker).is_file()
        ))

    def save_source(self, data):
        if self.fixed_source is not None:
            raise PermissionError('这个实例的图片目录由部署配置管理')
        with self.lock:
            if self.progress['phase'] == 'running':
                raise RuntimeError('请等这趟扫描结束后再修改图片目录')
            source = {name: local_dir(data.get(name, '')) for name in self.default}
            if not source['originals']:
                raise ValueError('请填写原图目录')
            with self.db() as connection:
                connection.execute("INSERT OR REPLACE INTO preferences VALUES ('source', ?)",
                                   (json.dumps(source, ensure_ascii=False),))
            self.progress = {'phase': 'idle'}
            return source

    def state(self):
        with self.lock:
            progress = self.progress.copy()
        with self.db() as connection:
            source = self.get_source(connection)
            root = source['originals']
            result = connection.execute(LIBRARY + '''
                SELECT COUNT(*) AS images,
                       COUNT(DISTINCT NULLIF(style_key,'')) FILTER (WHERE style_removed IS NULL) AS styles,
                       COALESCE(SUM(has_prompt),0) AS with_prompt,
                       COALESCE(SUM(1-present),0) AS unavailable FROM library WHERE image_removed IS NULL
                ''', (root,)).fetchone()
        return {'source': source, 'progress': progress, 'models': list(NAI_VERSIONS), 'counts': dict(result),
                'gallery_page_sizes': GALLERY_PAGE_SIZES, 'source_available': self.source_available(root),
                'source_locked': self.fixed_source is not None}

    def start_scan(self):
        with self.lock:
            if self.progress['phase'] == 'running':
                return self.progress.copy()
            root = self.source()['originals']
            if not self.source_available(root):
                raise ValueError('原图目录不可用，请检查图片目录或磁盘挂载')
            self.progress = dict(phase='running', seen=0, added=0, indexed=0, skipped=0, failed=0, missing=0)
            threading.Thread(target=self.scan, args=(root,), name='grimoire-scan', daemon=True).start()
            return self.progress.copy()

    def scan(self, root):
        base = Path(root)
        counters = dict(seen=0, added=0, indexed=0, skipped=0, failed=0, missing=0)
        seen = set()

        def report(phase, **extra):
            with self.lock:
                self.progress = {'phase': phase, **counters, **extra}

        def walk_error(exc):
            raise exc

        try:
            status = base.stat()
            with self.db() as connection:
                cached = {r['name']: r for r in connection.execute(
                    'SELECT * FROM files WHERE root=?', (root,))}
                known = {r['digest'] for r in connection.execute(
                    'SELECT DISTINCT digest FROM files WHERE root=?', (root,))}
                for current, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
                    folder = Path(current)
                    dirs[:] = [name for name in dirs if not name.startswith('.') and
                               not (folder / name).is_symlink() and not (folder / name).is_junction()]
                    for name in names:
                        path = folder / name
                        if name.startswith('.') or path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                            continue
                        relative = path.relative_to(base).as_posix()
                        seen.add(relative)
                        counters['seen'] += 1
                        old = cached.get(relative)
                        try:
                            before = path.stat()
                            if old and old['present'] and (old['mtime'], old['size']) == (before.st_mtime_ns, before.st_size):
                                counters['skipped'] += 1
                                continue
                            if not path.is_file() or before.st_size > MAX_BYTES:
                                raise ValueError('Unsupported file')
                            asset = inspect(path)
                            after = path.stat()
                            if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                                raise ValueError('Source changed while reading')
                        except (OSError, ValueError, OverflowError, TypeError, SyntaxError,
                                Image.DecompressionBombWarning, Image.DecompressionBombError):
                            counters['failed'] += 1
                            if old:
                                with connection:
                                    connection.execute('UPDATE files SET present=0 WHERE root=? AND name=?', (root, relative))
                        else:
                            digest = asset['digest']
                            columns = ','.join(asset)
                            placeholders = ','.join('?' for _ in asset)
                            with connection:
                                connection.execute(f'INSERT OR IGNORE INTO assets ({columns}) VALUES ({placeholders})', tuple(asset.values()))
                                if asset['style_key']:
                                    version = nai_version(asset['model'], asset['tool'])
                                    connection.execute('''
                                        INSERT OR IGNORE INTO style_numbers (style, version, ordinal)
                                        SELECT ?, ?, COALESCE(MAX(ordinal), 0) + 1 FROM style_numbers WHERE version=?
                                        ''', (asset['style_key'], version, version))
                                connection.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,1)',
                                                   (root, relative, digest, after.st_size, after.st_mtime_ns))
                            counters['added'] += digest not in known
                            counters['indexed'] += 1
                            known.add(digest)
                        report('running')

                current = base.stat()
                if (status.st_dev, status.st_ino) != (current.st_dev, current.st_ino) or not self.source_available(root):
                    raise OSError('Source changed during scan')
                missing = [(root, name) for name, item in cached.items() if name not in seen and item['present']]
                with connection:
                    connection.executemany('UPDATE files SET present=0 WHERE root=? AND name=?', missing)
                counters['missing'] = len(missing)
        except (OSError, sqlite3.Error):
            report('error', message='扫描中断。请检查图片目录或磁盘是否可用，再试一次。')
        except Exception:
            report('error', message='扫描中断。已完成的索引已保存，可再次扫描。')
            raise
        else:
            report('done')

    def images(self, query='', model='', mode='', page=1, style='', in_trash=False, page_size=PAGE_SIZE):
        if style or in_trash or page_size not in GALLERY_PAGE_SIZES:
            page_size = PAGE_SIZE
        with self.db() as connection:
            args = [self.get_source(connection)['originals']]
            clauses, terms = conditions(query, "positive||' '||negative||' '||characters||' '||model||' '||artist_text||' '||COALESCE(e.title,'')||' '||COALESCE(e.notes,'')")
            clauses.append('image_removed IS NOT NULL' if in_trash else 'image_removed IS NULL')
            args.extend(terms)
            if model:
                clauses.append('model_version=?' if model in NAI_VERSIONS else 'model=?')
                args.append(model)
            if mode == 'favorite':
                clauses.append('e.favorite=1')
            if mode == 'pending':
                clauses.append('has_prompt=0')
            if style:
                clauses.append('style_key=?')
                args.append(style)
            body = " FROM library LEFT JOIN edits e ON e.kind='image' AND e.key=digest" + where(clauses)
            count = connection.execute(LIBRARY + 'SELECT COUNT(*)' + body, args).fetchone()[0]
            page = min(page, max(1, (count + page_size - 1) // page_size))
            order = 'image_removed DESC, digest' if in_trash else ORDER
            rows = connection.execute(LIBRARY + f'''SELECT {CARD}, COALESCE(e.title,'') AS title,
                COALESCE(e.favorite,0) AS favorite''' + body + f' ORDER BY {order} LIMIT ? OFFSET ?',
                (*args, page_size, (page - 1) * page_size))
            return self.page(rows, count, page, page_size)

    def styles(self, query='', model='', mode='', page=1, style='', in_trash=False):
        with self.db() as connection:
            root = self.get_source(connection)['originals']
            args = [root]
            clauses, terms = conditions(
                query, "artist_text||' '||model||' '||COALESCE(NULLIF(e.title,''),style_name(model_version,n.ordinal))||' '||COALESCE(e.notes,'')"
            )
            clauses.append("style_key!=''")
            if in_trash:
                clauses.append('style_removed IS NOT NULL')
            else:
                clauses.extend(('image_removed IS NULL', 'style_removed IS NULL'))
            args.extend(terms)
            if model:
                clauses.append('model_version=?' if model in NAI_VERSIONS else 'model=?')
                args.append(model)
            if mode == 'favorite':
                clauses.append('e.favorite=1')
            if style:
                clauses.append('style_key=?')
                args.append(style)
            body = (" FROM library LEFT JOIN edits e ON e.kind='style' AND e.key=style_key"
                    " LEFT JOIN style_numbers n ON n.style=style_key AND n.version=model_version") + where(clauses)
            count = connection.execute(LIBRARY + 'SELECT COUNT(DISTINCT style_key)' + body, args).fetchone()[0]
            page = min(page, max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE))
            order = 'style_removed DESC, style_key' if in_trash else 'favorite DESC, last_seen DESC, style_key'
            rows = connection.execute(LIBRARY + f'''
                , ranked AS (
                    SELECT {CARD}, artist_text, style_removed, COALESCE(e.cover,'') AS pinned,
                    style_name(model_version,n.ordinal) AS default_title,
                    COALESCE(e.title,'') AS title, COALESCE(e.notes,'') AS notes, COALESCE(e.favorite,0) AS favorite,
                    COUNT(*) OVER (PARTITION BY style_key) AS matches,
                    COUNT(*) OVER (PARTITION BY style_key, model_version) AS model_matches,
                    MAX(changed) OVER (PARTITION BY style_key) AS last_seen,
                    ROW_NUMBER() OVER (PARTITION BY style_key ORDER BY {COVER_ORDER}) AS rank
                    {body}
                )
                SELECT * FROM ranked WHERE rank=1
                ORDER BY {order} LIMIT ? OFFSET ?
                ''', (*args, PAGE_SIZE, (page - 1) * PAGE_SIZE))
            result = self.page(rows, count, page)
            if style and result['items']:
                result['models'] = [dict(row) for row in connection.execute(LIBRARY + '''
                    SELECT model_version AS model, COUNT(*) AS matches FROM library
                    WHERE style_key=? AND image_removed IS NULL AND model_version!=''
                    GROUP BY model_version ORDER BY model_version
                    ''', (root, style))]
            return result

    @staticmethod
    def page(rows, count, page, page_size=PAGE_SIZE):
        return {'items': list(map(dict, rows)), 'total': count, 'page': page, 'page_size': page_size}

    def image(self, digest):
        with self.db() as connection:
            row = connection.execute(LIBRARY + '''
                SELECT library.*, COALESCE(e.title,'') AS title, COALESCE(e.notes,'') AS notes,
                       COALESCE(e.favorite,0) AS favorite
                FROM library LEFT JOIN edits e ON e.kind='image' AND e.key=digest
                WHERE digest=? AND image_removed IS NULL
                ''', (self.get_source(connection)['originals'], digest)).fetchone()
        if not row:
            raise LookupError('图片未入典')
        result = dict(row)
        for field in ('characters', 'parameters'):
            result[field] = json.loads(result[field])
        return result

    def set_trash(self, kind, key, trashed):
        if kind not in ('image', 'style') or not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError('词条标识无效')
        if type(trashed) is not bool:
            raise ValueError('请选择删除或恢复')
        field = 'digest' if kind == 'image' else 'style_key'
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            root = self.get_source(connection)['originals']
            if not connection.execute(LIBRARY + f'SELECT 1 FROM library WHERE {field}=? LIMIT 1', (root, key)).fetchone():
                raise LookupError('词条未入典')
            if trashed:
                connection.execute('INSERT OR IGNORE INTO trash VALUES (?,?,?)', (kind, key, time.time_ns()))
            else:
                connection.execute('DELETE FROM trash WHERE kind=? AND key=?', (kind, key))
        return {'trashed': trashed}

    def update_edit(self, kind, key, values):
        if kind not in ('image', 'style') or not KEY.fullmatch(key):
            raise ValueError('词条标识无效')
        with self.db() as connection:
            root = self.get_source(connection)['originals']
            field = 'digest' if kind == 'image' else 'style_key'
            if not connection.execute(LIBRARY + f'SELECT 1 FROM library WHERE {field}=? LIMIT 1', (root, key)).fetchone():
                raise LookupError('词条未入典')
            current = connection.execute('SELECT * FROM edits WHERE kind=? AND key=?', (kind, key)).fetchone()
            edit = dict(current) if current else dict(title='', notes='', favorite=0, cover='')
            for name, limit in (('title', 100), ('notes', 4000)):
                if name in values:
                    if not isinstance(values[name], str):
                        raise ValueError('请输入文字')
                    edit[name] = values[name].strip()[:limit]
            if 'favorite' in values:
                edit['favorite'] = int(bool(values['favorite']))
            if 'cover' in values and kind == 'style':
                cover = values['cover']
                if not isinstance(cover, str) or (cover and (not KEY.fullmatch(cover) or not connection.execute(
                    LIBRARY + 'SELECT 1 FROM library WHERE digest=? AND style_key=? AND present=1 AND image_removed IS NULL',
                    (root, cover, key)
                ).fetchone())):
                    raise ValueError('这张图片暂时无法设为封面')
                edit['cover'] = cover
            connection.execute('INSERT OR REPLACE INTO edits VALUES (?,?,?,?,?,?)',
                               (kind, key, edit['title'], edit['notes'], edit['favorite'], edit['cover']))
            return {name: edit[name] for name in ('title', 'notes', 'favorite', 'cover')}

    def paths(self, digest):
        with self.db() as connection:
            sources = self.get_source(connection)
            root = sources['originals']
            rows = connection.execute('''
                SELECT name, mtime, size FROM files WHERE root=? AND digest=? AND present=1 ORDER BY name
                ''', (root, digest)).fetchall()
        base = Path(root)
        for row in rows:
            candidate = base / row['name']
            try:
                actual = candidate.resolve(strict=True)
                stat = actual.stat()
                if actual == candidate and actual.is_relative_to(base) and actual.is_file() and (
                        row['mtime'], row['size']) == (stat.st_mtime_ns, stat.st_size):
                    return sources, actual
            except OSError:
                continue
        raise LookupError('原图暂不可用，请检查目录或重新扫描')
