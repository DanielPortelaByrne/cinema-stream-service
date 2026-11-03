import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import stream_service as svc
import voice
from configuration import Settings

TOKEN = 't' * 32


class ParseQuery(unittest.TestCase):
    def test_strips_the_verb_and_filler(self):
        self.assertEqual(voice.parse_query('download Dune Part Two'), ('Dune Part Two', None, None))
        self.assertEqual(voice.parse_query('Please could you get me The Matrix please'), ('The Matrix', None, None))
        self.assertEqual(voice.parse_query('hey google, request heat'), ('heat', None, None))

    def test_keeps_a_leading_the_that_is_part_of_the_title(self):
        self.assertEqual(voice.parse_query('download the matrix')[0], 'the matrix')

    def test_a_kind_word_narrows_the_search(self):
        self.assertEqual(voice.parse_query('download the show Love Is Blind'), ('Love Is Blind', 'tv', None))
        self.assertEqual(voice.parse_query('get the movie called Heat'), ('Heat', 'movie', None))
        self.assertEqual(voice.parse_query('queue series Severance'), ('Severance', 'tv', None))

    def test_a_trailing_year_is_taken_out(self):
        self.assertEqual(voice.parse_query('download Dune 2021'), ('Dune', None, 2021))
        self.assertEqual(voice.parse_query('download Dune (1984)'), ('Dune', None, 1984))

    def test_a_title_that_is_only_a_year_is_kept(self):
        self.assertEqual(voice.parse_query('download 1917'), ('1917', None, None))

    def test_nothing_left_is_nothing(self):
        self.assertEqual(voice.parse_query('download'), (None, None, None))
        self.assertEqual(voice.parse_query('   '), (None, None, None))


class Choose(unittest.TestCase):
    results = [
        {'id': 1, 'mediaType': 'person', 'name': 'Dune Someone'},
        {'id': 2, 'mediaType': 'movie', 'title': 'Dune', 'releaseDate': '2021-09-15'},
        {'id': 3, 'mediaType': 'movie', 'title': 'Dune', 'releaseDate': '1984-12-14'},
        {'id': 4, 'mediaType': 'tv', 'name': 'Dune: Prophecy', 'firstAirDate': '2024-11-17'},
    ]

    def test_takes_the_first_film_or_series_and_skips_people(self):
        self.assertEqual(voice.choose(self.results)['id'], 2)

    def test_prefers_the_stated_year_and_kind(self):
        self.assertEqual(voice.choose(self.results, year=1984)['id'], 3)
        self.assertEqual(voice.choose(self.results, kind='tv')['id'], 4)

    def test_falls_back_when_the_year_matches_nothing(self):
        self.assertEqual(voice.choose(self.results, year=1999)['id'], 2)

    def test_nothing_usable_is_none(self):
        self.assertIsNone(voice.choose([{'id': 1, 'mediaType': 'person'}]))


class FakeSeerr:
    """Answers search and request like Seerr, and records what it was asked to request."""

    def __init__(self, results, request_code=201):
        self.results, self.request_code, self.requests, self.keys = results, request_code, [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                outer.keys.append(self.headers.get('X-Api-Key'))
                self.send(200, {'results': outer.results})

            def do_POST(self):
                outer.keys.append(self.headers.get('X-Api-Key'))
                outer.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send(outer.request_code, {})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def close(self):
        self.server.shutdown(); self.server.server_close()


MOVIE = {'id': 438631, 'mediaType': 'movie', 'title': 'Dune', 'releaseDate': '2021-09-15'}
SHOW = {'id': 98, 'mediaType': 'tv', 'name': 'Love Is Blind', 'firstAirDate': '2020-02-13'}


class VoiceRequests(unittest.TestCase):
    def seerr(self, results, code=201):
        fake = FakeSeerr(results, code)
        self.addCleanup(fake.close)
        return fake, voice.Voice(fake.url, 'seerr-key')

    def test_a_film_is_requested_with_the_api_key(self):
        fake, v = self.seerr([MOVIE])
        answer = v.request('download Dune')
        self.assertEqual((answer['status'], answer['say']), ('requested', 'Requested Dune (2021).'))
        self.assertEqual(fake.requests, [{'mediaType': 'movie', 'mediaId': 438631}])
        self.assertEqual(set(fake.keys), {'seerr-key'})

    def test_a_series_asks_for_every_season(self):
        fake, v = self.seerr([SHOW])
        v.request('download the show Love Is Blind')
        self.assertEqual(fake.requests, [{'mediaType': 'tv', 'mediaId': 98, 'seasons': 'all'}])

    def test_already_in_the_library_is_not_requested_again(self):
        fake, v = self.seerr([{**MOVIE, 'mediaInfo': {'status': 5}}])
        self.assertEqual(v.request('download Dune')['status'], 'available')
        self.assertEqual(fake.requests, [])

    def test_a_conflict_means_already_requested(self):
        _, v = self.seerr([MOVIE], code=409)
        self.assertEqual(v.request('download Dune')['status'], 'already')

    def test_nothing_found(self):
        fake, v = self.seerr([{'id': 1, 'mediaType': 'person'}])
        self.assertEqual(v.request('download Zzzzz')['status'], 'not_found')
        self.assertEqual(fake.requests, [])

    def test_an_unclear_phrase_never_reaches_seerr(self):
        fake, v = self.seerr([MOVIE])
        self.assertEqual(v.request('download')['status'], 'unclear')
        self.assertEqual(fake.keys, [])

    def test_seerr_refusing_the_key_is_unavailable(self):
        _, v = self.seerr([MOVIE], code=403)
        self.assertEqual(v.request('download Dune')['status'], 'unavailable')

    def test_seerr_down_is_unavailable(self):
        self.assertEqual(voice.Voice('http://127.0.0.1:1', 'k').request('download Dune')['status'], 'unavailable')

    def test_the_rate_limit_counts_a_minute(self):
        now = [0.0]
        v = voice.Voice('http://127.0.0.1:1', 'k', per_minute=2, clock=lambda: now[0])
        self.assertEqual([v.allow(), v.allow(), v.allow()], [True, True, False])
        now[0] = 61
        self.assertTrue(v.allow())


class VoiceEndpoint(unittest.TestCase):
    def serve(self, results=(MOVIE,), enabled=True, per_minute=12):
        fake = FakeSeerr(list(results))
        server = svc.LimitedServer(('127.0.0.1', 0), svc.Handler)
        if enabled:
            server.voice, server.voice_token = voice.Voice(fake.url, 'seerr-key', per_minute=per_minute), TOKEN
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for stop in (fake.close, server.server_close, server.shutdown):
            self.addCleanup(stop)
        return fake, 'http://127.0.0.1:%d/voice/request' % server.server_address[1]

    def post(self, url, body, token=TOKEN):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(url, data=json.dumps(body).encode() if not isinstance(body, bytes) else body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_request_with_the_token_is_filed(self):
        fake, url = self.serve()
        code, body = self.post(url, {'query': 'download Dune'})
        self.assertEqual((code, body['status']), (200, 'requested'))
        self.assertEqual(len(fake.requests), 1)

    def test_a_wrong_or_missing_token_is_refused_and_files_nothing(self):
        fake, url = self.serve()
        self.assertEqual(self.post(url, {'query': 'download Dune'}, token='wrong')[0], 401)
        self.assertEqual(self.post(url, {'query': 'download Dune'}, token=None)[0], 401)
        self.assertEqual(fake.requests, [])

    def test_it_is_off_unless_configured(self):
        _, url = self.serve(enabled=False)
        self.assertEqual(self.post(url, {'query': 'download Dune'})[0], 404)

    def test_a_bad_body_is_a_400(self):
        _, url = self.serve()
        self.assertEqual(self.post(url, {'nope': 1})[0], 400)
        self.assertEqual(self.post(url, b'not json')[0], 400)
        self.assertEqual(self.post(url, {'query': '   '})[0], 400)

    def test_the_limit_returns_429(self):
        _, url = self.serve(per_minute=1)
        self.assertEqual(self.post(url, {'query': 'download Dune'})[0], 200)
        self.assertEqual(self.post(url, {'query': 'download Dune'})[0], 429)


class VoiceSettings(unittest.TestCase):
    base = {'PROWLARR_API_KEY': 'p', 'QBITTORRENT_USERNAME': 'u', 'QBITTORRENT_PASSWORD': 'w'}

    def test_both_are_optional(self):
        s = Settings.from_env(self.base)
        self.assertEqual((s.seerr_api_key, s.voice_token), ('', ''))

    def test_they_are_read_and_hidden_from_repr(self):
        s = Settings.from_env({**self.base, 'SEERR_API_KEY': 'seerr-key', 'VOICE_TOKEN': TOKEN})
        self.assertEqual((s.seerr_api_key, s.voice_token), ('seerr-key', TOKEN))
        self.assertNotIn(TOKEN, repr(s))
        self.assertNotIn('seerr-key', repr(s))

    def test_a_short_token_is_refused(self):
        with self.assertRaises(ValueError):
            Settings.from_env({**self.base, 'VOICE_TOKEN': 'short'})


if __name__ == '__main__':
    unittest.main()
