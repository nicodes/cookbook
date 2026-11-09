"""Exercise accepted descriptors through the real renderer and native Caddy."""
from source_fixture import SDK_ROOT, PREPARED_ROOT, tool_fixture
import gzip
import hashlib
import http.server
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import artifact_packet
import dependencies
import deployment
import komizo_core
import workload_fixture

oci_fixture = tool_fixture('image_scan_fixture')


class WorkloadHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = komizo_core.binary(PREPARED_ROOT, dict(os.environ))
        cls.caddy = workload_fixture.caddy_binary(PREPARED_ROOT, dict(os.environ))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native workload ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'source.txt').write_text('native HTTP source fixture')
        for args in [['init', '-q'], ['remote', 'add', 'origin', 'https://github.com/example/native.git'], ['add', '.'],
                     ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'source fixture']]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        self.revision = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.environment = dict(os.environ, APP_VERSION=self.revision, GITHUB_REPOSITORY='example/native', GITHUB_REPOSITORY_ID='123')
        source = self.root / 'public'
        source.mkdir()
        self.files = {'index.html': b'index-shell', '+not-found.html': b'owned-not-found',
            'profiles/alice.html': b'profile-shell', 'release.json': b'{"revision":"' + self.revision.encode() + b'"}',
            'releases/' + 'a' * 64 + '/game.pck': b'opaque-native-payload'}
        for name, body in self.files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        pack = source / ('releases/' + 'a' * 64 + '/game.pck')
        pack.with_suffix('.pck.gz').write_bytes(gzip.compress(pack.read_bytes(), mtime=0))
        packet = self.root / '.artifacts/release'
        (packet / 'static').mkdir(parents=True)
        (packet / 'images').mkdir()
        static = artifact_packet.pack_static(source, packet / 'static/web.tar.gz', max_bytes=4096, max_file_bytes=1024)
        digest = 'sha256:' + static['sha256']
        oci_fixture.archive(packet / 'images/api.oci.tar', reference='ghcr.io/example/native-api:' + self.revision, source=self.revision)
        image = dependencies.helper('oci_archive').inspect_archive(packet / 'images/api.oci.tar')
        self.release = {'kind': 'komizo.release', 'version': 1, 'application': 'native',
            'providers': {'cookbook_revision': 'b' * 40, 'tools_revision': 'c' * 40, 'core_revision': 'd' * 40},
            'source': {'repository': 'example/native', 'repository_id': '123', 'revision': self.revision},
            'artifacts': {'web': {'kind': 'archive',
                'reference': 'ghcr.io/example/native-web@sha256:' + hashlib.sha256(deployment.static_manifest(digest, static['size'])).hexdigest(),
                'digest': digest, 'size_bytes': static['size'], 'media_type': deployment.STATIC_MEDIA},
                'api': {'kind': 'image', 'reference': 'ghcr.io/example/native-api@' + image['manifest_digest'],
                    'digest': image['manifest_digest'], 'config_digest': image['config_digest'], 'platform': image['platform']}},
            'workload': {'services': {'api': {'artifact': 'api', 'user': '10001:10001', 'networks': ['api'],
                'limits': {'memory_bytes': 64 << 20, 'memory_swap_bytes': 64 << 20, 'milli_cpus': 500, 'pids': 32}}},
                'jobs': {}, 'assets': {'web': {'artifact': 'web', 'serving': {
                    'fallbacks': ['{path}', '{path}.html', '{path}/index.html'], 'not_found': '+not-found.html',
                    'headers': {'X-Content-Type-Options': 'nosniff'},
                    'route_rules': [{'pattern': '^/profiles/([^/]+)/play/?$', 'replacement': '/profiles/$1', 'redirect_status': 308}],
                    'header_rules': [{'paths': ['/', '*.html'], 'headers': {'Cache-Control': 'no-store'}},
                        {'path_regexp': '^/releases/[a-f0-9]{64}/', 'headers': {'Cache-Control': 'public, max-age=31536000, immutable'}},
                        {'paths': ['*.pck'], 'headers': {'Content-Type': 'application/octet-stream'}}],
                    'precompressed': ['gzip'], 'encode': ['gzip'], 'encode_content_types': ['text/*', 'application/json*']}}},
                'networks': {'api': {'internal': False}}, 'volumes': {},
                'routes': [{'host': 'app.example.test', 'path': '/api/*', 'strip_prefix': True,
                    'target': {'kind': 'service', 'name': 'api', 'port': 8080}},
                    {'host': 'app.example.test', 'path': '/', 'target': {'kind': 'asset', 'name': 'web'}},
                    {'host': 'api.example.test', 'path': '/', 'target': {'kind': 'service', 'name': 'api', 'port': 8080},
                        'headers': {'Content-Security-Policy': "default-src 'none'"}, 'proxy': {'encode': ['gzip'], 'encode_content_types': ['text/*', 'application/json*'], 'error_body': 'upstream unavailable'}},
                    {'host': 'www.example.test', 'path': '/', 'redirect': {'to': 'https://app.example.test', 'status': 301, 'preserve_uri': True}}],
                'readiness': [{'url': 'https://app.example.test/', 'status': 200}]}}
        (packet / 'release.json').write_bytes(deployment.canonical(self.release))
        artifact_packet.write_inventory(packet, {'release.json': 'release', 'static/web.tar.gz': 'static', 'images/api.oci.tar': 'image'})
        self.config = {'components': {'assets': [{'name': 'web', 'source': 'public', 'max_bytes': 4096, 'max_file_bytes': 1024}]}}
        class API(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/unavailable':
                    self.connection.close()
                    return
                if self.path=='/forwarded':
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'forwarded':self.headers.get_all('X-Forwarded-For',[]),
                        'real':self.headers.get_all('X-Real-IP',[])}).encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(('upstream:' + self.path).encode())
            def log_message(self, *_arguments):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), API)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.core_patch = patch.object(workload_fixture.komizo_core, 'binary', return_value=self.core)
        self.caddy_patch = patch.object(workload_fixture, 'caddy_binary', return_value=self.caddy)
        self.core_patch.start(); self.caddy_patch.start()
        self.addCleanup(self.core_patch.stop); self.addCleanup(self.caddy_patch.stop)

    def fixture(self):
        return workload_fixture.context(self.root, self.config, self.environment,
            upstreams={'api': '127.0.0.1:' + str(self.server.server_port)})

    def get(self, fixture, host, path='/', headers=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_arguments):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(fixture.base_url + path, headers={'Host': host, **(headers or {})})
        try:
            response = opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_source_headers_unknown_hosts_redirect_and_owned404(self):
        with self.fixture() as fixture:
            self.assertEqual(self.get(fixture, 'app.example.test')[0::2], (200, b'index-shell'))
            self.assertNotIn('Content-Security-Policy', self.get(fixture, 'app.example.test')[1])
            status, headers, body = self.get(fixture, 'unknown.example.test')
            self.assertEqual(status, 404)
            self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000')
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
            self.assertEqual(self.get(fixture, 'app.example.test', '/unknown/path')[0::2], (404, b'owned-not-found'))
            status, headers, body = self.get(fixture, 'www.example.test', '/a?q=1')
            self.assertEqual((status, headers['Location']), (301, 'https://app.example.test/a?q=1'))

    def test_dynamic_routes_and_uri_preserving_redirect(self):
        with self.fixture() as fixture:
            self.assertEqual(self.get(fixture, 'app.example.test', '/profiles/alice')[0::2], (200, b'profile-shell'))
            status, headers, _ = self.get(fixture, 'app.example.test', '/profiles/alice/play')
            self.assertEqual((status, headers['Location']), (308, '/profiles/alice'))

    def test_compressed_bytes_cache_and_mime_match_exact_static_payload(self):
        with self.fixture() as fixture:
            path = '/releases/' + 'a' * 64 + '/game.pck'
            status, headers, body = self.get(fixture, 'app.example.test', path, {'Accept-Encoding': 'gzip'})
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Encoding'], 'gzip')
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertEqual(headers['Cache-Control'], 'public, max-age=31536000, immutable')
            self.assertEqual(gzip.decompress(body), self.files[path[1:]])
            self.assertEqual(self.get(fixture, 'app.example.test')[1]['Cache-Control'], 'no-store')

    def test_proxy_prefix_and_error_response_keep_native_status_and_security(self):
        with self.fixture() as fixture:
            self.assertEqual(self.get(fixture, 'app.example.test', '/api/probe')[0::2], (200, b'upstream:/probe'))
            self.assertEqual(self.get(fixture, 'api.example.test', '/probe')[0::2], (200, b'upstream:/probe'))
            status, headers, body = self.get(fixture, 'api.example.test', '/unavailable')
            self.assertEqual((status, body), (502, b'upstream unavailable'))
            self.assertIn('Content-Security-Policy', headers)

    def test_proxy_replaces_forged_forwarded_identity_with_one_actual_client(self):
        with self.fixture() as fixture:
            status,_headers,body=self.get(fixture,'api.example.test','/forwarded',
                {'X-Forwarded-For':'1.1.1.1, 203.0.113.7, 192.0.2.2'})
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)['forwarded'],['127.0.0.1'])
            connection=http.client.HTTPConnection('127.0.0.1',fixture.port,timeout=5)
            try:
                connection.putrequest('GET','/forwarded',skip_host=True)
                connection.putheader('Host','api.example.test')
                connection.putheader('X-Forwarded-For','203.0.113.7')
                connection.putheader('X-Forwarded-For','192.0.2.2')
                connection.endheaders()
                response=connection.getresponse()
                self.assertEqual(response.status,200)
                self.assertEqual(json.loads(response.read())['forwarded'],['127.0.0.1'])
            finally:
                connection.close()

    def test_assertion_failure_always_reaps_its_owned_http_process(self):
        pid = None
        with self.assertRaisesRegex(ValueError, 'owned assertion'):
            with self.fixture() as fixture:
                pid = fixture.pid
                raise ValueError('owned assertion')
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_start_observer_failure_cleans_before_fixture_readiness(self):
        observed=[]
        def observer(pid):
            observed.append(pid)
            raise ValueError('startup observer failed')
        with self.assertRaisesRegex(ValueError,'startup observer failed'):
            with workload_fixture.context(self.root,self.config,self.environment,
                    upstreams={'api':'127.0.0.1:'+str(self.server.server_port)},on_started=observer):
                self.fail('failed startup observation cannot yield a ready fixture')
        self.assertEqual(len(observed),1)
        with self.assertRaises(ProcessLookupError):
            os.kill(observed[0],0)


if __name__ == '__main__':
    unittest.main()
