"""The search features the live Deck service had before they were merged here: add-on streams, per-indexer search,
Stremio candidates sent by the app, tracker magnets, and the most-seeded-first pick."""
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import stream_service as svc
from configuration import Settings

H1, H2, H3 = '1' * 40, '2' * 40, '3' * 40


def result(name, seeders=50, size=3 * 1024 ** 3, **extra):
    return {'title': name, 'seeders': seeders, 'size': size, 'infoHash': H1, **extra}


class Pick(unittest.TestCase):
    def test_a_far_less_seeded_release_loses_even_when_better_quality(self):
        crowded = result('Heat 1995 1080p WEB-DL', seeders=400)
        lonely = result('Heat 1995 2160p REMUX', seeders=20)
        self.assertEqual(svc.pick([lonely, crowded], 'Heat', 1995)['title'], crowded['title'])

    def test_among_well_seeded_releases_quality_wins(self):
        a = result('Heat 1995 720p WEB-DL', seeders=300)
        b = result('Heat 1995 1080p BluRay', seeders=200)
        self.assertEqual(svc.pick([a, b], 'Heat', 1995)['title'], b['title'])


class Candidates(unittest.TestCase):
    def test_only_well_formed_torrents_survive(self):
        good = {'title': 'Heat 1995 1080p', 'infoHash': H1.upper(), 'seeders': 30, 'size': 2 * 1024 ** 3}
        kept = svc.clean_candidates([
            good, {'infoHash': 'short', 'seeders': 1, 'size': 1}, {'infoHash': H2, 'seeders': True, 'size': 1},
            {'infoHash': H2, 'seeders': -1, 'size': 5}, {'infoHash': H2, 'seeders': 5, 'size': 0}, 'junk', None])
        self.assertEqual(kept, [{**good, 'infoHash': H1}])

    def test_not_a_list_is_nothing_and_the_list_is_capped(self):
        self.assertEqual(svc.clean_candidates({'infoHash': H1}), [])
        many = [{'infoHash': H1, 'seeders': 5, 'size': 10} for _ in range(200)]
        self.assertEqual(len(svc.clean_candidates(many)), 60)


class Magnets(unittest.TestCase):
    def test_a_bare_info_hash_gets_trackers_so_peers_can_be_found(self):
        link = svc.magnet_for({'infoHash': H1}, 'Heat 1995')
        self.assertIn('xt=urn:btih:' + H1, link)
        self.assertIn(urllib.parse.quote('udp://tracker.opentrackr.org:1337/announce', safe=''), link)

    def test_a_real_magnet_link_is_kept_as_it_is(self):
        real = 'magnet:?xt=urn:btih:%s&tr=https%%3A%%2F%%2Fprivate.example%%2Fannounce' % H1
        self.assertEqual(svc.magnet_for({'magnetUrl': real, 'infoHash': H1}, 'Heat'), real)


def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, 'http://127.0.0.1:%d' % server.server_address[1]


def respond(handler, body, code=200):
    data = json.dumps(body).encode()
    handler.send_response(code); handler.send_header('Content-Length', str(len(data))); handler.end_headers(); handler.wfile.write(data)


class FakeStremio:
    """Cinemeta (title to IMDb id) and one add-on (IMDb id to torrents), on one local port."""

    def __init__(self, test, streams):
        outer = self
        self.paths = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                outer.paths.append(self.path)
                if self.path.startswith('/catalog/'):
                    respond(self, {'metas': [{'id': 'tt0113277', 'name': 'Heat', 'releaseInfo': '1995'},
                                             {'id': 'tt9999999', 'name': 'Heat', 'releaseInfo': '2013'}]})
                elif self.path.startswith('/stream/'):
                    respond(self, {'streams': streams})
                else:
                    respond(self, {}, 404)

        self.server, self.url = serve(Handler)
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)


class AddonResults(unittest.TestCase):
    STREAMS = [
        {'infoHash': H1, 'title': 'Heat.1995.1080p.BluRay.x264\n\U0001F464 87 \U0001F4BE 2.5 GB'},
        {'infoHash': H2, 'title': 'Heat.1995.720p\n\U0001F464 12 \U0001F4BE 900 MB'},
        {'infoHash': 'bad', 'title': 'no hash\n\U0001F464 5 \U0001F4BE 1 GB'},
        {'infoHash': H3, 'title': 'no seeders line\n\U0001F4BE 1 GB'},
    ]

    def setUp(self):
        svc.imdb_ids.clear()

    def test_streams_become_results_the_pick_can_use(self):
        fake = FakeStremio(self, self.STREAMS)
        found = svc.addon_results(fake.url, 'Heat', 1995, None, fake.url)
        self.assertEqual([(r['infoHash'], r['seeders'], r['size']) for r in found],
                         [(H1, 87, int(2.5 * 1024 ** 3)), (H2, 12, 900 * 1024 ** 2)])
        self.assertTrue(any(p.startswith('/stream/movie/tt0113277') for p in fake.paths))   # the 1995 one, not the 2013 one

    def test_an_episode_is_asked_for_by_season_and_episode(self):
        fake = FakeStremio(self, [])
        svc.addon_results(fake.url, 'Heat', None, (2, 5), fake.url)
        self.assertTrue(any(p.startswith('/stream/series/tt9999999:2:5') or p.startswith('/stream/series/tt0113277:2:5') for p in fake.paths))

    def test_an_unknown_title_asks_no_add_on(self):
        class Empty(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self): respond(self, {'metas': []})
        server, url = serve(Empty)
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.assertEqual(svc.addon_results(url, 'Nothing Here', 2001, None, url), [])


class Search(unittest.TestCase):
    """Prowlarr is asked indexer by indexer, alongside the add-ons; a failing source is skipped, not fatal."""

    def setUp(self):
        svc.imdb_ids.clear()
        self.asked = []
        outer = self

        class Prowlarr(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                if self.path.startswith('/api/v1/indexer'):
                    respond(self, [{'id': 1, 'enable': True}, {'id': 2, 'enable': True}, {'id': 3, 'enable': False}])
                    return
                ids = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['indexerIds']
                outer.asked.append(ids[0])
                if ids == ['2']:
                    respond(self, {}, 500)
                else:
                    respond(self, [result('Heat 1995 1080p BluRay', seeders=40)])

        server, self.prowlarr = serve(Prowlarr)
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)

    def streams(self, **extra):
        return svc.Streams(prowlarr=self.prowlarr, prowlarr_api_key='k', host_media=Path(tempfile.mkdtemp()), **extra)

    def test_only_enabled_indexers_are_asked_and_a_failing_one_is_skipped(self):
        found = self.streams(addons=()).search('Heat', 1995)
        self.assertEqual(sorted(self.asked), ['1', '2'])
        self.assertEqual([r['title'] for r in found], ['Heat 1995 1080p BluRay'])

    def test_add_on_streams_join_the_indexer_results(self):
        fake = FakeStremio(self, [{'infoHash': H1, 'title': 'Heat.1995.2160p\n\U0001F464 99 \U0001F4BE 9 GB'}])
        found = self.streams(addons=(fake.url,), cinemeta=fake.url).search('Heat', 1995)
        self.assertEqual({r['title'] for r in found}, {'Heat 1995 1080p BluRay', 'Heat.1995.2160p'})

    def test_a_dead_add_on_does_not_stop_the_indexers_answering(self):
        found = self.streams(addons=('http://127.0.0.1:1',), cinemeta='http://127.0.0.1:1').search('Heat', 1995)
        self.assertEqual(len(found), 1)

    def test_nothing_answering_is_an_error(self):
        class Down(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self): respond(self, {}, 500)
        server, url = serve(Down)
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        streams = svc.Streams(prowlarr=url, prowlarr_api_key='k', host_media=Path(tempfile.mkdtemp()), addons=())
        with self.assertRaises(svc.ServiceError):
            streams.search('Heat', 1995)

    def test_candidates_from_the_app_are_considered_by_start(self):
        streams = self.streams(addons=())
        calls = []
        streams.searches.get = lambda *a: []
        streams.qbit = lambda path, form=None: calls.append((path, form)) or None
        streams.wait_for_entry = lambda *a, **k: None
        streams.start('Heat', '1995', candidates=[{'title': 'Heat 1995 1080p BluRay', 'infoHash': H1, 'seeders': 60, 'size': 3 * 1024 ** 3}])
        added = [form for path, form in calls if path.endswith('/torrents/add')]
        self.assertEqual(len(added), 1)
        self.assertIn(H1, added[0]['urls'])


class Settings_(unittest.TestCase):
    base = {'PROWLARR_API_KEY': 'p', 'QBITTORRENT_USERNAME': 'u', 'QBITTORRENT_PASSWORD': 'w'}

    def test_defaults_keep_the_repo_category_and_the_public_add_ons(self):
        s = Settings.from_env(self.base)
        self.assertEqual((s.category, s.addons), ('cinema-stream', None))

    def test_a_category_can_be_adopted_and_must_be_plain(self):
        self.assertEqual(Settings.from_env({**self.base, 'STREAM_CATEGORY': 'mcm-stream'}).category, 'mcm-stream')
        for bad in ('has space', '../x', '', 'a' * 41):
            with self.assertRaises(ValueError):
                Settings.from_env({**self.base, 'STREAM_CATEGORY': bad})

    def test_add_ons_can_be_replaced_or_turned_off(self):
        self.assertEqual(Settings.from_env({**self.base, 'STREAM_ADDONS': 'none'}).addons, ())
        self.assertEqual(Settings.from_env({**self.base, 'STREAM_ADDONS': ''}).addons, ())
        self.assertEqual(Settings.from_env({**self.base, 'STREAM_ADDONS': 'https://a.example, http://10.0.0.5:7000/'}).addons,
                         ('https://a.example', 'http://10.0.0.5:7000'))
        with self.assertRaises(ValueError):
            Settings.from_env({**self.base, 'STREAM_ADDONS': 'ftp://nope'})

    def test_the_category_is_used_everywhere_a_torrent_is_owned(self):
        streams = svc.Streams(host_media=Path(tempfile.mkdtemp()), category='mcm-stream')
        streams.to_host = lambda p: streams.cache
        self.assertTrue(streams.owns_torrent({'category': 'mcm-stream', 'save_path': '/media'}))
        self.assertFalse(streams.owns_torrent({'category': 'cinema-stream', 'save_path': '/media'}))


if __name__ == '__main__':
    unittest.main()


class ShowTitle(unittest.TestCase):
    """An episode's release must name this show, not another that merely contains its words."""

    def test_a_different_show_with_extra_words_is_refused(self):
        self.assertFalse(svc.show_title_is('Love Is Blind', 'Love is Blind Netherlands S01E01 1080p WEB H264'))
        self.assertFalse(svc.show_title_is('Love Is Blind', 'Love Is Blind Japan S02E03 720p'))
        self.assertFalse(svc.show_title_is('Love Is Blind', 'Love is Blind UK S01E01 1080p WEB h264'))
        self.assertFalse(svc.show_title_is('The Office', 'The.Office.UK.S01E01.720p'))
        self.assertFalse(svc.show_title_is('Heat', 'Heat Wave S01E01'))

    def test_the_show_itself_is_accepted_in_common_spellings(self):
        for name in ('Love.Is.Blind.S01E01.1080p.WEB.H264-NTb', 'Love Is Blind (2020) S01E01 1080p', 'Love Is Blind 2020 S01 Complete',
                     'Love.Is.Blind.US.S01E01.720p', 'love is blind season 1 1080p', 'Love Is Blind 1x01 HDTV'):
            self.assertTrue(svc.show_title_is('Love Is Blind', name), name)

    def test_a_us_tag_is_allowed_but_only_after_the_title(self):
        self.assertTrue(svc.show_title_is('The Office', 'The.Office.US.S01E01.1080p'))
        self.assertFalse(svc.show_title_is('The Office', 'Inside The Office S01E01'))

    def test_acceptable_uses_it_for_episodes_only(self):
        other = result('Love is Blind Netherlands S01E01 1080p WEB H264', seeders=46, size=2 * 1024 ** 3)
        own = result('Love Is Blind S01E01 1080p WEB H264', seeders=30, size=2 * 1024 ** 3)
        self.assertEqual(svc.acceptable(other, 'Love Is Blind', None, (1, 1)), (False, 'title'))
        self.assertTrue(svc.acceptable(own, 'Love Is Blind', None, (1, 1))[0])
        self.assertEqual(svc.pick([other, own], 'Love Is Blind', None, (1, 1))['title'], own['title'])
        self.assertTrue(svc.acceptable(result('Love Is Blind Special 2024 1080p WEB', size=2 * 1024 ** 3), 'Love Is Blind', 2024)[0])  # films unchanged
