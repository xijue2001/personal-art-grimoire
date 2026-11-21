from base64 import b64encode
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
from unittest import TestCase

from grimoire.app import Handler
from grimoire.catalog import Catalog


class NasAccess(TestCase):
    host = 'nas.example.test:18770'
    origin = 'http://' + host

    def setUp(self):
        working = self.enterContext(TemporaryDirectory())
        self.root = Path(working).resolve()
        self.marker = self.root / '.catalog-disk'
        self.marker.touch()
        source = dict(originals=str(self.root), previews='', thumbnails='')
        self.catalog = Catalog(self.root / 'index.sqlite3', fixed_source=source, source_marker=self.marker.name)
        self.server = self.enterContext(ThreadingHTTPServer(('127.0.0.1', 0), Handler))
        self.server.catalog = self.catalog
        self.server.public = urlsplit(self.origin)
        self.server.authorization = b'Basic ' + b64encode(b'grimoire:fixture-only-not-a-live-password')
        worker = Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, *, host=None, origin=None, login=False):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            headers = {'Host': host or self.host}
            if login:
                headers['Authorization'] = self.server.authorization.decode()
            if origin:
                headers['Origin'] = origin
            body = None
            if data is not None:
                headers['Content-Type'] = 'application/json'
                body = json.dumps(data)
            connection.request('POST' if body else 'GET', path, body, headers)
            response = connection.getresponse()
            return response.status, response.getheader('WWW-Authenticate'), response.read()
        finally:
            connection.close()

    def test_private_routes_require_sign_in(self):
        for path in ('/', '/site/app.js', '/api/state', '/media/' + 'a' * 64 + '/original'):
            with self.subTest(path=path):
                status, challenge, _ = self.request(path)
                self.assertEqual(status, 401)
                self.assertTrue(challenge.startswith('Basic '))
        self.assertEqual(self.request('/health')[0], 200)
        self.assertEqual(self.request('/api/state', host='elsewhere.test', login=True)[0], 403)
        status, _, raw = self.request('/api/state', login=True)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['source_locked'])

    def test_mutations_validate_origin_and_lock_directories(self):
        self.assertEqual(self.request('/api/scan', {}, login=True, origin='http://other.example.test')[0], 403)
        self.assertEqual(self.request('/api/source', {'originals': '/'}, login=True, origin=self.origin)[0], 403)
        self.marker.unlink()
        self.assertEqual(self.request('/api/scan', {}, login=True, origin=self.origin)[0], 400)
