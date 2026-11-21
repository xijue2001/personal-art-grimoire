"""Extract a reusable, conservative artist expression from image metadata."""
import hashlib
import json
import re
from decimal import Decimal


TOKEN = re.compile(r'\\.|(?P<weight>[+-]?(?:\d+(?:\.\d*)?|\.\d+))::|(?P<closing>::)|(?P<mark>[{},\[\]|\n])')
ARTIST = re.compile(r'artist:\s*(\S(?:.*\S)?)', re.IGNORECASE)
CLOSE = {'{': '}', '[': ']', '::': '::'}


def extract_artists(prompt):
    """Keep the relative order and weight scopes of explicit positive artist tags."""
    scopes, members = [], []
    start = 0

    def collect(end):
        match = ARTIST.fullmatch(prompt[start:end].strip())
        if match:
            members.append(('artist:' + match[1], tuple(scopes)))

    for token in TOKEN.finditer(prompt):
        if token.group().startswith('\\'):
            continue
        collect(token.start())
        start = token.end()
        mark = token['mark']
        if mark == '|':
            return '', ''
        if token['weight']:
            value = token['weight']
            if len(value) > 16 or any(scope[0] == '::' for scope in scopes):
                return '', ''
            weight = format(Decimal(value).normalize(), 'f')
            scopes.append(('::', '0' if weight == '-0' else weight))
        elif token['closing']:
            scopes.clear()
        elif mark in ('{', '['):
            scopes.append((mark, ''))
        elif mark in ('}', ']'):
            if not scopes or CLOSE[scopes[-1][0]] != mark:
                return '', ''
            scopes.pop()

    collect(len(prompt))
    if not members:
        return '', ''

    segments = []
    for artist, scope in members:
        head = ''.join(weight + mark for mark, weight in scope)
        tail = ''.join(CLOSE[mark] for mark, _ in reversed(scope))
        segments.append(head + artist + tail)
    expression = ', '.join(segments)
    key_data = json.dumps(members, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(key_data).hexdigest(), expression
