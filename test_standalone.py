"""Configuration and the exact HTTP contract consumed by the APK and Flatpak."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from configuration import Settings
from playback import PlaybackTickets
import stream_service as svc

HASH = 'a' * 40


class LocalServerTests(unittest.TestCase):
    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f'http://127.0.0.1:{server.server_port}'

    def temporary(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)


class ConfigurationTests(LocalServerTests):
    def env(self, **extra):
        return {'PROWLARR_API_KEY': 'fixture-key', 'QBITTORRENT_USERNAME': 'someone',
                'QBITTORRENT_PASSWORD': 'fixture-password', **extra}

    def test_configuration_does_not_require_a_media_stack_database(self):
        root = self.temporary()
        settings = Settings.from_env(self.env(MEDIA_ROOT=str(root), QBITTORRENT_MEDIA_ROOT='/downloads'))
        self.assertEqual(settings.media_root, root.resolve())
        self.assertEqual(settings.qbit_media_root, '/downloads')
        self.assertEqual(settings.listen_host, '127.0.0.1')
        self.assertNotIn('fixture-key', repr(settings))
        self.assertNotIn('fixture-password', repr(settings))

    def test_secrets_can_be_mounted_as_files(self):
        path = self.temporary() / 'key'
        path.write_text('mounted-key\n')
        env = self.env()
        del env['PROWLARR_API_KEY']
        env['PROWLARR_API_KEY_FILE'] = str(path)
        self.assertEqual(Settings.from_env(env).prowlarr_api_key, 'mounted-key')

    def test_ambiguous_missing_and_unreadable_secrets_fail_without_echoing_them(self):
        for env in ({}, self.env(PROWLARR_API_KEY_FILE='/not/a/file'),
                    {'PROWLARR_API_KEY_FILE': '/not/a/file'}):
            with self.assertRaises(ValueError) as caught:
                Settings.from_env(env)
            self.assertNotIn('fixture-password', str(caught.exception))
            self.assertNotIn('fixture-key', str(caught.exception))

    def test_bad_upstream_urls_and_ports_are_rejected(self):
        for extra in ({'PROWLARR_URL': 'file:///etc/passwd'}, {'SEERR_URL': 'http://user:password@host'},
                      {'QBITTORRENT_URL': 'http://host/?token=secret'}, {'STREAM_PORT': '0'},
                      {'STREAM_PORT': '65536'}, {'QBITTORRENT_MEDIA_ROOT': '/media/../private'}):
            with self.assertRaises(ValueError):
                Settings.from_env(self.env(**extra))


class TicketTests(LocalServerTests):
    def test_ticket_is_bound_to_one_hash_and_expires(self):
        now = [1000]
        tickets = PlaybackTickets(b'x' * 32, lifetime=60, clock=lambda: now[0])
        ticket = tickets.issue(HASH)
        self.assertTrue(tickets.valid(HASH, ticket))
        self.assertFalse(tickets.valid('b' * 40, ticket))
        self.assertFalse(tickets.valid(HASH, ticket + '0'))
        now[0] = 1060
        self.assertFalse(tickets.valid(HASH, ticket))

    def test_persisted_key_preserves_links_across_service_restarts(self):
        file = self.temporary() / 'state' / 'playback.key'
        first = PlaybackTickets.from_file(file)
        ticket = first.issue(HASH)
        self.assertTrue(PlaybackTickets.from_file(file).valid(HASH, ticket))
        self.assertEqual(file.stat().st_size, 32)


class UpstreamTests(LocalServerTests):
    def test_qbittorrent_signin_cookie_and_plain_text_success(self):
        logins = []
        class Qbit(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.headers.get('Cookie') != 'SID=fixture':
                    self.send_response(403); self.end_headers(); return
                body = b'[]'
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode())
                self.send_response(200)
                if self.path.endswith('/login'):
                    logins.append(form); self.send_header('Set-Cookie', 'SID=fixture; Path=/')
                self.end_headers(); self.wfile.write(b'Ok.')
        _, base = self.serve(Qbit)
        streams = svc.Streams(qbit=base, qbit_username='someone', qbit_password='fixture-password', host_media=self.temporary())
        self.assertEqual(streams.qbit('/api/v2/torrents/info'), [])
        self.assertEqual(logins, [{'username': ['someone'], 'password': ['fixture-password']}])
        self.assertIsNone(streams.qbit('/api/v2/torrents/add', {'urls': 'fixture'}))
        self.assertEqual(len(logins), 1)

    def test_qbittorrent_http_200_login_failure_is_rejected(self):
        class Qbit(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self): self.send_response(403); self.end_headers()
            def do_POST(self): self.send_response(200); self.end_headers(); self.wfile.write(b'Fails.')
        _, base = self.serve(Qbit)
        streams = svc.Streams(qbit=base, qbit_username='someone', qbit_password='fixture-password', host_media=self.temporary())
        with self.assertRaisesRegex(svc.ServiceError, 'sign-in failed'):
            streams.qbit('/api/v2/torrents/info')

    def test_prowlarr_receives_the_configured_key_in_a_header(self):
        observed = []
        class Prowlarr(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                observed.append((self.headers.get('X-Api-Key'), self.path))
                self.send_response(200); self.end_headers(); self.wfile.write(b'[]')
        _, base = self.serve(Prowlarr)
        streams = svc.Streams(prowlarr=base, prowlarr_api_key='fixture-key', host_media=self.temporary())
        self.assertEqual(streams.search('Example Film', 2026), [])
        self.assertEqual(observed[0][0], 'fixture-key')
        self.assertNotIn('fixture-key', observed[0][1])


class HttpContractTests(LocalServerTests):
    def setUp(self):
        self.authenticated = []
        outer = self
        class Seerr(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                outer.authenticated.append(self.headers.get('Cookie'))
                body = b'{"id":1}' if self.headers.get('Cookie') == 'connect.sid=fixture' else b'{}'
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        _, seerr = self.serve(Seerr)
        streams = svc.Streams(seerr=seerr, host_media=self.temporary())
        self.film = bytes(range(256)) * 4
        file = streams.cache / 'example.mkv'
        file.write_bytes(self.film)
        self.entry = {'hash': HASH, 'path': str(file), 'size': len(self.film), 'transcode': False, 'seen': time.time()}
        self.started = []
        def start(*args):
            self.started.append(args)
            return HASH, 'Example Film', 'connecting'
        streams.start = start
        streams.prepare = lambda *args: True
        streams.wait_for_entry = lambda _: self.entry
        streams.downloaded_until = lambda entry, position: entry['size']
        streams.status = lambda _: {'phase': 'ready', 'size': len(self.film)}
        server, self.base = self.serve(svc.Handler)
        server.streams = streams
        self.streams = streams

    def post(self, body, cookie='connect.sid=fixture', path='/start'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        return urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers), timeout=5)

    def test_apk_and_flatpak_can_play_and_seek_without_a_session_cookie(self):
        with self.post({'title': 'Example Film', 'year': '2026', 'audio': 'any'}) as response:
            result = json.load(response)
        self.assertEqual(self.authenticated, ['connect.sid=fixture'])
        # Both clients append this query; Android also replaces /stream/ with /status/.
        url = self.base + result['path'] + '?audio=original'
        request = urllib.request.Request(url, headers={'Range': 'bytes=100-199'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], 'bytes 100-199/1024')
            self.assertEqual(response.read(), self.film[100:200])
        with urllib.request.urlopen(url.replace('/stream/', '/status/')) as response:
            self.assertEqual(json.load(response)['phase'], 'ready')

    def test_missing_or_invalid_session_cannot_start_a_download(self):
        for cookie in ('', 'connect.sid=bad'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post({'title': 'Example Film'}, cookie=cookie)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
        self.assertEqual(self.started, [])

    def test_prepare_uses_the_same_session_and_accepts_episode_metadata(self):
        with self.post({'title': 'Example Series', 'season': 1, 'episode': 2}, path='/prepare') as response:
            self.assertEqual(response.status, 202)
            self.assertTrue(json.load(response)['searching'])

    def test_unsigned_or_tampered_links_cannot_read_media_or_status(self):
        ticket = self.streams.tickets.issue(HASH)
        for path in (f'/stream/{HASH}', f'/stream/{HASH}/{ticket}0', f'/status/{HASH}/{ticket}0'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + path)
            self.assertIn(error.exception.code, (403, 404))
            error.exception.close()

    def test_invalid_request_bodies_return_client_errors_without_starting(self):
        for body, expected in ((b'[]', 400), (b'invalid', 400), (b'x' * 4097, 413),
                               ({'title': 'Example', 'season': True, 'episode': 1}, 400)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post(body)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
        self.assertEqual(self.started, [])


class StorageOwnershipTests(LocalServerTests):
    def test_tracker_parameters_are_preserved_in_a_real_magnet(self):
        magnet = 'magnet:?xt=urn:btih:' + HASH + '&tr=https%3A%2F%2Ftracker.example.org%2Fannounce'
        self.assertEqual(svc.magnet_for({'infoHash': HASH, 'magnetUrl': magnet}, 'Example'), magnet)

    def test_category_is_created_once_for_a_new_instance(self):
        streams = svc.Streams(host_media=self.temporary())
        calls = []
        streams.qbit = lambda path, form=None: calls.append((path, form))
        streams.ensure_category()
        streams.ensure_category()
        self.assertEqual(sum('/createCategory' in path for path, _ in calls), 1)

    def test_path_traversal_cannot_escape_the_shared_media_directory(self):
        streams = svc.Streams(host_media=self.temporary())
        for path in ('/media/../private', '/media/sub/../../private', '/media/sub\\..\\private', '/other/file'):
            with self.assertRaises(svc.ServiceError):
                streams.to_host(path)

    def test_an_existing_library_download_is_never_adopted_or_deleted(self):
        streams = svc.Streams(host_media=self.temporary())
        streams.search = lambda *args: [{'title': 'Example Film 2026 1080p', 'seeders': 20,
                                         'size': 1024 ** 3, 'infoHash': HASH}]
        calls = []
        def qbit(path, form=None):
            calls.append(path)
            return [{'hash': HASH, 'category': 'radarr', 'save_path': '/media/Movies'}] if '/info?' in path else None
        streams.qbit = qbit
        with self.assertRaisesRegex(svc.ServiceError, 'another download'):
            streams.start('Example Film', 2026)
        self.assertFalse(any('/add' in path or '/delete' in path for path in calls))
        self.assertNotIn(HASH, streams.pending)

    def test_idle_cleanup_leaves_downloads_reassigned_to_other_categories_alone(self):
        streams = svc.Streams(host_media=self.temporary())
        streams.known[HASH] = {'seen': 0}
        streams.torrent = lambda _: {'category': 'radarr', 'save_path': '/media/Movies'}
        calls = []
        streams.qbit = lambda *args: calls.append(args)
        streams.sweep()
        self.assertEqual(calls, [])
        self.assertNotIn(HASH, streams.known)


if __name__ == '__main__':
    unittest.main()
