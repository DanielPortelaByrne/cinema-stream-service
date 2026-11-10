import json
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import stream_service as svc

HASH = 'a' * 40
PIECE = 65536


def release(name, seeders=20, size=2 * 1024 ** 3, **extra):
    return {'title': name, 'seeders': seeders, 'size': size, 'magnetUrl': 'magnet:?xt=urn:btih:' + HASH, **extra}


class SelectionRules(unittest.TestCase):
    def test_rejects_low_seeders(self):
        self.assertEqual(svc.acceptable(release('Heat 1995 1080p BluRay x264', seeders=4), 'Heat', 1995), (False, 'seeders'))

    def test_rejects_implausible_size(self):
        self.assertFalse(svc.acceptable(release('Heat 1995 1080p', size=50 * 1024 ** 2), 'Heat', 1995)[0])
        self.assertFalse(svc.acceptable(release('Heat 1995 1080p', size=20 * 1024 ** 3), 'Heat', 1995)[0])

    def test_rejects_cam_and_telesync(self):
        self.assertEqual(svc.acceptable(release('Heat 1995 HDCAM x264'), 'Heat', 1995)[1], 'poor copy')
        self.assertEqual(svc.acceptable(release('Heat 1995 TELESYNC'), 'Heat', 1995)[1], 'poor copy')

    def test_a_release_may_leave_out_the_subtitle_when_the_year_is_there(self):
        title = "Lemora: A Child's Tale of the Supernatural"
        self.assertTrue(svc.title_matches(title, 1973, 'Lemora 1973 1080p BluRay x264'))
        self.assertTrue(svc.title_matches(title, 1973, 'Lemora.A.Childs.Tale.of.the.Supernatural.1973.720p'))
        self.assertFalse(svc.title_matches(title, 1973, 'Lemora 2019 1080p'))
        self.assertFalse(svc.title_matches(title, None, 'Lemora 1080p'))   # no year: the main title alone is too loose

    def test_rejects_other_films_and_series_packs(self):
        self.assertFalse(svc.acceptable(release('Heat 1995 1080p'), 'Heat', 2001)[0])  # year mismatch
        self.assertFalse(svc.acceptable(release('Heat 1995 1080p'), 'Ronin', 1998)[0])  # title mismatch
        self.assertEqual(svc.acceptable(release('Heat S01-S03 1080p 1995'), 'Heat', 1995)[1], 'not a film')

    def test_title_needs_whole_words(self):
        # "Heat" must not match inside "Heater".
        self.assertFalse(svc.title_matches('Heat', 1995, 'Heater 1995 1080p'))

    def test_prefers_higher_resolution_then_source_then_seeds(self):
        hd = release('Heat 1995 1080p BluRay', seeders=8)
        uhd = release('Heat 1995 2160p WEB-DL', seeders=8)
        well_seeded = release('Heat 1995 1080p BluRay', seeders=200)
        # 1080p outranks 4K on purpose: 4K files are heavy to stream reliably over a home connection.
        self.assertGreater(svc.score(hd), svc.score(uhd))
        self.assertGreater(svc.score(well_seeded), svc.score(hd))
        self.assertGreater(svc.score(release('Heat 1995 1080p BluRay')), svc.score(release('Heat 1995 1080p HDTV')))

    def test_prefers_audio_the_tv_can_decode(self):
        eac3 = release('Backrooms 2026 1080p AMZN WEB-DL DDP5 1 Atmos H 264', seeders=800)
        plain = release('Backrooms 2026 1080p WEB-DL HEVC x265 5.1 BONE', seeders=500)
        self.assertTrue(svc.audio_unplayable(eac3['title']))
        self.assertFalse(svc.audio_unplayable(plain['title']))
        self.assertEqual(svc.pick([eac3, plain], 'Backrooms', 2026)['title'], plain['title'])
        # With only E-AC-3 seeded, the film is still offered rather than failing.
        self.assertEqual(svc.pick([eac3], 'Backrooms', 2026)['title'], eac3['title'])

    def test_a_player_that_decodes_any_audio_is_not_steered_away_from_e_ac_3(self):
        eac3 = release('Backrooms 2026 1080p AMZN WEB-DL DDP5 1 Atmos H 264', seeders=800)
        plain = release('Backrooms 2026 1080p WEB-DL HEVC x265 5.1 BONE', seeders=120)
        self.assertEqual(svc.pick([eac3, plain], 'Backrooms', 2026, any_audio=True)['title'], eac3['title'])

    def test_pick_returns_none_when_nothing_survives(self):
        self.assertIsNone(svc.pick([release('Heat 1995 HDCAM', seeders=50)], 'Heat', 1995))
        self.assertEqual(svc.pick([release('Heat 1995 1080p', seeders=50)], 'Heat', 1995)['seeders'], 50)


class EpisodeRules(unittest.TestCase):
    """A TV episode: only a single-episode release for that exact SxxExx, and only that file is played."""

    EP = (1, 7)
    SHOW = 'Westworld'

    def test_accepts_the_right_episode_only(self):
        good = release('Westworld S01E07 1080p BluRay x264', size=1024 ** 3)
        self.assertTrue(svc.acceptable(good, self.SHOW, None, self.EP)[0])
        self.assertEqual(svc.acceptable(release('Westworld S01E08 1080p BluRay', size=1024 ** 3), self.SHOW, None, self.EP), (False, 'other episode'))
        self.assertEqual(svc.acceptable(release('Westworld S01E17 1080p BluRay', size=1024 ** 3), self.SHOW, None, self.EP), (False, 'other episode'))

    def test_the_pack_of_that_season_is_accepted_but_not_other_seasons_or_several(self):
        pack = release('Westworld S01 Complete 1080p BluRay', size=8 * 1024 ** 3)
        self.assertTrue(svc.acceptable(pack, self.SHOW, None, self.EP)[0])
        self.assertTrue(svc.acceptable(release('Westworld Season 1 1080p WEB-DL', size=8 * 1024 ** 3), self.SHOW, None, self.EP)[0])
        self.assertEqual(svc.acceptable(release('Westworld S02 1080p BluRay', size=8 * 1024 ** 3), self.SHOW, None, self.EP), (False, 'other episode'))
        ranged = release('Westworld S01-S02 1080p BluRay S01E07', size=8 * 1024 ** 3)
        self.assertEqual(svc.acceptable(ranged, self.SHOW, None, self.EP)[1], 'several seasons')
        self.assertEqual(svc.acceptable(release('Westworld S01 Extras 1080p', size=8 * 1024 ** 3), self.SHOW, None, self.EP)[1], 'extras')

    def test_a_pack_is_judged_by_one_episodes_share_of_it(self):
        pack = release('Westworld S01 Complete 1080p BluRay', size=40 * 1024 ** 3)
        single = release('Westworld S01E07 1080p BluRay', size=4 * 1024 ** 3)
        self.assertAlmostEqual(svc.stream_score(pack, episode=self.EP), svc.stream_score(single, episode=self.EP))

    def test_episode_size_bounds_differ_from_films(self):
        small = release('Westworld S01E07 720p WEB-DL', size=40 * 1024 ** 2)
        self.assertEqual(svc.acceptable(small, self.SHOW, None, self.EP)[1], 'size')
        tiny_film = release('Heat 1995 1080p', size=40 * 1024 ** 2)
        self.assertEqual(svc.acceptable(tiny_film, 'Heat', 1995)[1], 'size')

    def test_title_must_match_without_a_year(self):
        self.assertEqual(svc.acceptable(release('Ronin S01E07 1080p', size=1024 ** 3), self.SHOW, None, self.EP)[1], 'title')

    def test_video_file_picks_only_that_episode(self):
        files = [{'index': 0, 'name': 'Westworld.S01E07.mkv', 'size': 900}, {'index': 1, 'name': 'Westworld.S01E08.mkv', 'size': 950}]
        self.assertEqual(svc.video_file(files, self.EP), (0, 'Westworld.S01E07.mkv', 900))
        self.assertIsNone(svc.video_file([files[1]], self.EP))
        self.assertEqual(svc.video_file(files)[0], 1)   # with no episode, the largest video, as for films

    def test_marker_does_not_match_longer_numbers(self):
        self.assertIsNone(svc.episode_marker(1, 1).search('Show.S01E12.mkv'))
        self.assertIsNotNone(svc.episode_marker(1, 12).search('Show.S01E12.mkv'))


class HelpersTest(unittest.TestCase):
    def test_parse_range_forms(self):
        self.assertEqual(svc.parse_range(None, 100), (0, 99))
        self.assertEqual(svc.parse_range('bytes=10-19', 100), (10, 19))
        self.assertEqual(svc.parse_range('bytes=50-', 100), (50, 99))
        self.assertEqual(svc.parse_range('bytes=-10', 100), (90, 99))
        self.assertEqual(svc.parse_range('bytes=10-999', 100), (10, 99))
        self.assertIsNone(svc.parse_range('bytes=100-', 100))
        self.assertIsNone(svc.parse_range('bytes=5-2', 100))
        self.assertIsNone(svc.parse_range('items=1-2', 100))

    def test_video_file_picks_largest_video(self):
        files = [{'index': 0, 'name': 'Heat/sample.mkv', 'size': 10}, {'index': 1, 'name': 'Heat/Heat.mkv', 'size': 900},
                 {'index': 2, 'name': 'Heat/Heat.nfo', 'size': 999}]
        self.assertEqual(svc.video_file(files), (1, 'Heat/Heat.mkv', 900))
        self.assertIsNone(svc.video_file([{'index': 0, 'name': 'readme.txt', 'size': 3}]))

    def test_hash_from_magnet(self):
        self.assertEqual(svc.hash_from_magnet('magnet:?xt=urn:btih:' + HASH.upper() + '&dn=x'), HASH)
        self.assertIsNone(svc.hash_from_magnet('https://example.test/file.torrent'))


class FakeServers:
    """A fake Prowlarr and qBittorrent. The fake qBittorrent writes the film in pieces, as a real download does."""

    def __init__(self, stack, film_bytes, host_media):
        self.film = film_bytes
        self.stack = stack
        self.host_media = Path(host_media)
        self.save_path = None
        self.torrent = None
        self.file_prio = {}
        self.deleted = []
        self.servers = []
        self.pieces_done = 0
        outer = self

        class Prowlarr(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path.startswith('/api/v1/indexer'):
                    body = json.dumps([{'id': 1, 'enable': True}]).encode()
                    self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
                    return
                body = json.dumps([release('Heat 1995 1080p BluRay', seeders=40), release('Heat 1995 HDCAM', seeders=500)]).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        class QBit(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def reply(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

            def form(self):
                length = int(self.headers.get('Content-Length', '0') or 0)
                raw = self.rfile.read(length).decode() if length else ''
                return dict(urllib.parse.parse_qsl(raw))

            def do_GET(self):
                path, _, query = self.path.partition('?')
                params = dict(urllib.parse.parse_qsl(query))
                if path == '/api/v2/torrents/info':
                    rows = [outer.torrent] if outer.torrent else []
                    self.reply(rows)
                elif path == '/api/v2/torrents/files':
                    self.reply([{'index': 0, 'name': 'Heat.1995/Heat.1995.1080p.mkv', 'size': len(outer.film)}])
                elif path == '/api/v2/torrents/properties':
                    self.reply({'piece_size': PIECE})
                elif path == '/api/v2/torrents/pieceStates':
                    total = (len(outer.film) + PIECE - 1) // PIECE
                    self.reply([2] * outer.pieces_done + [0] * (total - outer.pieces_done))
                else:
                    self.reply(None)

            def do_POST(self):
                path = self.path.split('?')[0]
                form = self.form()
                if path == '/api/v2/torrents/add':
                    outer.save_path = form['savepath']
                    outer.torrent = {'hash': HASH, 'state': 'downloading', 'save_path': form['savepath'],
                                     'category': form['category'], 'added_on': 1, 'size': len(outer.film)}
                    threading.Thread(target=outer.download, daemon=True).start()
                    self.reply(None)
                elif path == '/api/v2/torrents/filePrio':
                    outer.file_prio[form['id']] = form['priority']; self.reply(None)
                elif path == '/api/v2/torrents/delete':
                    outer.deleted.append(form['hashes']); outer.torrent = None; self.reply(None)
                else:
                    self.reply(None)

        self.prowlarr = self.serve(Prowlarr)
        self.qbit = self.serve(QBit)

    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def download(self):
        """Writes the film piece by piece, in order. The file is sized up front, as a sparse qBittorrent file appears."""
        # qBittorrent reports its own container path; the test maps it to where the host would see the same folder.
        target = self.host_media / self.save_path[len('/media/'):] / 'Heat.1995' / 'Heat.1995.1080p.mkv'
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as handle:
            handle.truncate(len(self.film))
            for i in range((len(self.film) + PIECE - 1) // PIECE):
                time.sleep(0.03)
                handle.seek(i * PIECE)
                handle.write(self.film[i * PIECE:(i + 1) * PIECE])
                handle.flush()
                self.pieces_done = i + 1


class StatusFields(unittest.TestCase):
    """The player's loading screen and Stream card read speed and size from /status."""

    def setUp(self):
        self.streams = svc.Streams(host_media=Path(tempfile.mkdtemp()))
        self.streams.torrent = lambda h: {'num_seeds': 7, 'num_leechs': 3, 'progress': 0.25, 'dlspeed': 2_400_000}

    def test_connecting_reports_speed(self):
        self.streams.pending[HASH] = {'label': 'Heat 1995', 'error': None}
        status = self.streams.status(HASH)
        self.assertEqual((status['phase'], status['seeds'], status['peers'], status['speed']), ('connecting', 7, 3, 2_400_000))

    def test_buffering_reports_speed_and_film_size(self):
        self.streams.known[HASH] = {'label': 'Heat 1995', 'size': 900}
        self.streams.downloaded_until = lambda entry, start: 0
        status = self.streams.status(HASH)
        self.assertEqual(status['phase'], 'buffering')
        self.assertEqual((status['speed'], status['size'], status['progress']), (2_400_000, 900, 0.0))
        self.assertEqual(status['torrentProgress'], 0.25)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.film = bytes(range(256)) * 4096   # 1 MiB of known content
        self.host_media = self.tmp / 'Media'
        self.fake = FakeServers(self.tmp, self.film, self.host_media)
        for server in self.fake.servers:
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.streams = svc.Streams(prowlarr_api_key='testkey', prowlarr=self.fake.prowlarr, qbit=self.fake.qbit, seerr='http://127.0.0.1:1', addons=(),
                                   host_media=self.host_media, qbit_media='/media')

    def test_start_selects_only_the_film_and_serves_it_with_ranges(self):
        from http.server import ThreadingHTTPServer as Server
        server = Server(('127.0.0.1', 0), svc.Handler)
        server.streams = self.streams
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        hash_, label, phase = self.streams.start('Heat', '1995')
        self.assertEqual((hash_, phase), (HASH, 'connecting'))
        self.assertEqual(label, 'Heat 1995 1080p BluRay')
        entry = self.streams.wait_for_entry(HASH)
        self.assertEqual(entry['size'], len(self.film))
        self.assertFalse(entry['transcode'])
        self.assertEqual(self.fake.file_prio.get('0'), '1')
        # Full body, while the file is still growing; the server waits for each piece.
        base = 'http://127.0.0.1:%d/stream/%s/%s' % (server.server_address[1], HASH, self.streams.tickets.issue(HASH))
        with urllib.request.urlopen(base, timeout=30) as response:
            self.assertEqual(response.read(), self.film)
        request = urllib.request.Request(base, headers={'Range': 'bytes=100-199'})
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], 'bytes 100-199/%d' % len(self.film))
            self.assertEqual(response.read(), self.film[100:200])

    def test_audio_original_serves_a_converted_film_as_it_is_with_ranges(self):
        from http.server import ThreadingHTTPServer as Server
        server = Server(('127.0.0.1', 0), svc.Handler)
        server.streams = self.streams
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.streams.start('Heat', '1995')
        entry = self.streams.wait_for_entry(HASH)
        entry['transcode'] = True   # what an E-AC-3 release gets, for players that cannot decode it
        url = 'http://127.0.0.1:%d/stream/%s/%s?audio=original' % (server.server_address[1], HASH, self.streams.tickets.issue(HASH))
        request = urllib.request.Request(url, headers={'Range': 'bytes=100-199'})
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.film[100:200])

    def test_proxy_magnet_urls_are_replaced_with_a_magnet_from_the_info_hash(self):
        # The real Prowlarr shape: a localhost proxy URL plus an info hash. qBittorrent cannot reach the proxy.
        proxied = {'title': 'Heat 1995 1080p BluRay', 'seeders': 40, 'size': 2 * 1024 ** 3,
                   'magnetUrl': 'http://127.0.0.1:9696/2/download?apikey=x&link=y', 'infoHash': HASH.upper()}
        link = svc.magnet_for(proxied, 'Heat')
        self.assertTrue(link.startswith('magnet:?xt=urn:btih:' + HASH + '&dn=Heat'))
        self.assertEqual(svc.hash_from_magnet(link), HASH)
        self.assertIsNone(svc.magnet_for({'magnetUrl': 'http://127.0.0.1:9696/x', 'title': 'Heat 1995'}, 'Heat'))

    def test_only_finished_pieces_count_as_available(self):
        streams = svc.Streams(host_media=Path(tempfile.mkdtemp()))
        entry = {'hash': HASH, 'offset': 0, 'size': 40, 'piece_size': 10, 'pieces': [2, 2, 0, 2], 'pieces_at': time.time()}
        self.assertEqual(streams.downloaded_until(entry, 0), 20)     # pieces 0-1 done, piece 2 not
        self.assertEqual(streams.downloaded_until(entry, 25), 20)    # inside the missing piece: nothing available
        self.assertEqual(streams.downloaded_until(entry, 30), 40)    # the tail piece is done even though piece 2 is not
        entry['offset'] = 5                                          # a film that starts part-way into the torrent
        self.assertEqual(streams.downloaded_until(entry, 0), 15)

    def test_paths_map_between_host_and_qbittorrent_container(self):
        cache = self.host_media / 'Downloads' / 'stream'
        self.assertEqual(self.streams.to_qbit(cache), '/media/Downloads/stream')
        self.assertEqual(self.streams.to_host('/media/Downloads/stream'), cache)
        with self.assertRaises(svc.ServiceError):
            self.streams.to_host('/tmp/elsewhere')

    def test_restore_adopts_stream_torrents_after_restart(self):
        self.fake.torrent = {'hash': HASH, 'state': 'stalledDL', 'save_path': '/media/Downloads/stream', 'category': svc.CATEGORY}
        (self.host_media / 'Downloads' / 'stream' / 'Heat.1995').mkdir(parents=True, exist_ok=True)
        (self.host_media / 'Downloads' / 'stream' / 'Heat.1995' / 'Heat.1995.1080p.mkv').write_bytes(self.film)
        self.streams.restore()
        self.assertIn(HASH, self.streams.known)
        self.assertEqual(self.streams.known[HASH]['size'], len(self.film))

    def test_start_with_no_acceptable_source_raises(self):
        self.streams.search = lambda title, year, episode=None: [release('Heat 1995 HDCAM', seeders=500)]
        with self.assertRaises(LookupError):
            self.streams.start('Heat', '1995')


class SearchCacheTests(unittest.TestCase):
    """/prepare searches ahead; /start then reuses the answer instead of asking the indexers again."""

    def setUp(self):
        self.now = 1000.0
        self.calls = []

        def search(title, year, episode=None):
            self.calls.append((title, year, episode))
            return [release('%s %s 1080p' % (title, year))]
        self.cache = svc.SearchCache(search, ttl=600, clock=lambda: self.now)

    def test_a_second_ask_within_the_ttl_is_a_hit(self):
        first = self.cache.get('Heat', '1995')
        self.assertIs(self.cache.get('heat', '1995'), first)   # same title, different case: same key
        self.assertEqual(len(self.calls), 1)

    def test_answers_expire(self):
        self.cache.get('Heat', '1995')
        self.now += 601
        self.cache.get('Heat', '1995')
        self.assertEqual(len(self.calls), 2)

    def test_each_episode_and_year_is_its_own_entry(self):
        self.cache.get('The Pitt', None, (2, 4))
        self.cache.get('The Pitt', None, (2, 5))
        self.cache.get('The Pitt', None, (2, 4))
        self.cache.get('Heat', '1995'); self.cache.get('Heat', '2026')
        self.assertEqual(self.calls, [('The Pitt', None, (2, 4)), ('The Pitt', None, (2, 5)), ('Heat', '1995', None), ('Heat', '2026', None)])

    def test_warm_runs_once_in_the_background_and_start_joins_it(self):
        gate = threading.Event()

        def slow(title, year, episode=None):
            gate.wait(2)
            self.calls.append(title)
            return [release('Heat 1995 1080p')]
        cache = svc.SearchCache(slow, ttl=600, clock=lambda: self.now)
        self.assertTrue(cache.warm('Heat', '1995'))
        self.assertFalse(cache.warm('Heat', '1995'))        # already running: not started twice
        got = []
        joiner = threading.Thread(target=lambda: got.append(cache.get('Heat', '1995')))
        joiner.start()
        time.sleep(0.05)
        gate.set()
        joiner.join(2)
        self.assertEqual(self.calls, ['Heat'])              # the /start waited for the warm-up instead of searching again
        self.assertEqual(len(got[0]), 1)
        self.assertFalse(cache.warm('Heat', '1995'))        # fresh now: nothing to do

    def test_a_failed_search_is_not_cached(self):
        fails = [True]

        def flaky(title, year, episode=None):
            self.calls.append(title)
            if fails.pop(0) if fails else False:
                raise svc.ServiceError('indexers unavailable')
            return []
        cache = svc.SearchCache(flaky, ttl=600, clock=lambda: self.now)
        with self.assertRaises(svc.ServiceError):
            cache.get('Heat', '1995')
        self.assertEqual(cache.get('Heat', '1995'), [])
        self.assertEqual(len(self.calls), 2)

    def test_start_uses_what_prepare_found(self):
        streams = svc.Streams(host_media=Path(tempfile.mkdtemp()))
        searched = []
        streams.search = lambda title, year, episode=None: searched.append(title) or [release('Heat 1995 1080p BluRay')]
        streams.qbit = lambda path, form=None: None
        streams.resolve = lambda hash_: None
        streams.searches.get('Heat', '1995')                 # what a /prepare leaves behind
        streams.start('Heat', '1995')
        self.assertEqual(searched, ['Heat'])


class StreamingScore(unittest.TestCase):
    """For streaming, a release has to arrive faster than it plays: bitrate is weighed against seeders."""

    GB = 1024 ** 3

    def test_a_heavy_4k_with_few_seeders_loses_to_a_well_seeded_1080p(self):
        uhd = release('Heat 1995 2160p WEB-DL', seeders=9, size=11 * self.GB)
        hd = release('Heat 1995 1080p WEB-DL', seeders=150, size=4 * self.GB)
        self.assertEqual(svc.pick([uhd, hd], 'Heat', 1995)['title'], hd['title'])

    def test_a_well_seeded_large_file_is_not_penalised(self):
        big = release('Heat 1995 1080p BluRay', seeders=400, size=10 * self.GB)
        small = release('Heat 1995 1080p WEBRip', seeders=400, size=2 * self.GB)
        self.assertGreater(svc.stream_score(big), svc.stream_score(small))   # better source, and the seeders can carry it

    def test_running_time_changes_the_estimate(self):
        r = release('Heat 1995 1080p BluRay', seeders=6, size=8 * self.GB)
        self.assertLess(svc.stream_score(r, runtime_minutes=60), svc.stream_score(r, runtime_minutes=170))

    def test_an_episode_is_judged_against_an_episode_length(self):
        ep = release('The Pitt S02E04 1080p WEB-DL', seeders=10, size=3 * self.GB)
        self.assertLess(svc.stream_score(ep, episode=(2, 4)), svc.stream_score(ep))

    def test_seeders_count_for_more_than_in_the_download_score(self):
        few = release('Heat 1995 1080p BluRay', seeders=10, size=2 * self.GB)
        many = release('Heat 1995 1080p BluRay', seeders=1000, size=2 * self.GB)
        self.assertGreater(svc.stream_score(many) - svc.stream_score(few), svc.score(many) - svc.score(few))

    def test_the_download_score_is_unchanged(self):
        self.assertAlmostEqual(svc.score(release('Heat 1995 1080p BluRay', seeders=100)), 3.0 * 2 + 2.0 + 2.0)


if __name__ == '__main__':
    unittest.main()
