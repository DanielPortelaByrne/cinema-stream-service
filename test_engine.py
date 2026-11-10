"""Streaming through Stremio's streaming server: source racing, range pass-through, status and cleanup."""
import json
import re
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine import StremioEngine, sources_for
from test_standalone import LocalServerTests
import stream_service as svc

LIVE, DEAD, SLOW, NOEND = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40
FILM = bytes(range(256)) * 16384   # 4 MiB


class FakeEngine(BaseHTTPRequestHandler):
    """Stremio's server, cut down: create, ranged file reads, stats, remove. DEAD never sends a byte; SLOW is late."""
    created, removed, ranges, gone = [], [], [], set()
    slow_open = {}

    def log_message(self, *_):
        pass

    def answer(self, body):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        hash_ = self.path.split('/')[1]
        FakeEngine.created.append((hash_, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
        if hash_ in FakeEngine.slow_open:
            time.sleep(FakeEngine.slow_open[hash_])
        self.answer({'files': [{'name': 'Extras.mkv', 'length': 1000},
                               {'name': 'Show.S01E05.1080p.mkv', 'length': len(FILM)}]})

    def do_GET(self):
        parts = self.path.strip('/').split('/')
        if parts[-1] == 'remove':
            FakeEngine.removed.append(parts[0]); return self.answer({})
        if parts[-1] == 'stats.json':
            if parts[0] in FakeEngine.gone:   # Stremio answers null for a torrent it has dropped
                return self.answer(None)
            return self.answer({'peers': 9, 'unchoked': 4, 'downloadSpeed': 2_000_000, 'streamProgress': 0.25})
        if parts[0] == DEAD:
            time.sleep(3); return
        if parts[0] == SLOW:
            time.sleep(0.6)
        if parts[0] == NOEND and not self.headers['Range'].startswith('bytes=0-'):
            time.sleep(3); return   # its start arrives, its end never does
        FakeEngine.ranges.append(self.headers.get('Range'))
        start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers['Range']).groups())
        end = min(end, len(FILM) - 1)
        self.send_response(206)
        self.send_header('Content-Range', f'bytes {start}-{end}/{len(FILM)}')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        self.wfile.write(FILM[start:end + 1])


class EngineTests(LocalServerTests):
    def setUp(self):
        FakeEngine.created, FakeEngine.removed, FakeEngine.ranges, FakeEngine.gone = [], [], [], set()
        FakeEngine.slow_open = {}
        _, self.engine_base = self.serve(FakeEngine)
        self.streams = svc.Streams(host_media=self.temporary(), addons=(), engine=StremioEngine(self.engine_base))
        self.streams.searches.get = lambda *args: []
        self.streams.race_patience, self.streams.race_deadline = 1.5, 5
        self.streams.prime_tail = self.streams.prime_head = 0   # priming is tested on its own: off, it cannot race other reads

    def candidate(self, hash_, seeders, quality='1080p'):
        return {'title': f'Show.S01E05.{quality}.WEB-DL', 'infoHash': hash_, 'seeders': seeders, 'size': 1024 ** 3,
                'fileIdx': 1, 'sources': ['tracker:udp://tracker.example:1337/announce']}

    def test_a_source_that_sends_nothing_loses_to_one_that_delivers(self):
        hash_, label, phase = self.streams.start('Show', None, (1, 5), candidates=[
            self.candidate(DEAD, 500, '2160p'), self.candidate(LIVE, 40)])
        self.assertEqual((hash_, phase), (LIVE, 'ready'))
        self.assertEqual(self.streams.known[LIVE]['file_index'], 1)
        self.assertIn(DEAD, FakeEngine.removed)
        self.assertNotIn(LIVE, FakeEngine.removed)

    def test_the_best_ranked_source_wins_when_it_delivers_within_its_patience(self):
        hash_, _, _ = self.streams.start('Show', None, (1, 5), candidates=[
            self.candidate(SLOW, 500), self.candidate(LIVE, 400, '720p')])
        self.assertEqual(hash_, SLOW)

    def test_nothing_delivering_is_no_source(self):
        self.streams.race_deadline = 1
        with self.assertRaises(LookupError):
            self.streams.start('Show', None, (1, 5), candidates=[self.candidate(DEAD, 50)])

    def test_a_source_already_playing_is_reused_without_racing(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        FakeEngine.created.clear()
        self.assertEqual(self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])[0], LIVE)
        self.assertEqual(FakeEngine.created, [])

    def test_tracker_hints_and_dht_reach_the_engine(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        sources = FakeEngine.created[0][1]['peerSearch']['sources']
        self.assertIn('tracker:udp://tracker.example:1337/announce', sources)
        self.assertEqual(sources[-1], 'dht:' + LIVE)
        magnet = sources_for({'magnetUrl': 'magnet:?xt=urn:btih:' + LIVE + '&tr=udp%3A%2F%2Fprivate.example%3A1%2Fannounce'}, LIVE, ['udp://public'])
        self.assertEqual(magnet, ['tracker:udp://private.example:1/announce', 'dht:' + LIVE])

    def test_status_reports_engine_stats_and_allows_seeking_anywhere(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        status = self.streams.status(LIVE)
        self.assertEqual((status['phase'], status['seeds'], status['peers'], status['speed']), ('ready', 4, 9, 2_000_000))
        self.assertTrue(status['seekAnywhere'])
        self.assertEqual(status['progress'], 0.25)

    def test_reads_pass_through_with_their_range(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        server, base = self.serve(svc.Handler)
        server.streams = self.streams
        path = '/stream/' + LIVE + '/' + self.streams.tickets.issue(LIVE)
        request = urllib.request.Request(base + path, headers={'Range': 'bytes=4000000-4000099'})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], f'bytes 4000000-4000099/{len(FILM)}')
            self.assertEqual(response.read(), FILM[4000000:4000100])
        self.assertEqual(FakeEngine.ranges[-1], 'bytes=4000000-4000099')

    def test_a_source_whose_end_never_comes_loses_to_one_that_sends_both(self):
        hash_, _, _ = self.streams.start('Show', None, (1, 5), candidates=[
            self.candidate(NOEND, 900, '2160p'), self.candidate(LIVE, 20, '720p')])
        self.assertEqual(hash_, LIVE)

    def test_with_no_end_anywhere_the_best_start_still_plays_without_waiting_out_the_race(self):
        self.streams.race_tail_patience, self.streams.race_deadline = 0.5, 20
        began = time.time()
        hash_, _, _ = self.streams.start('Show', None, (1, 5), candidates=[self.candidate(NOEND, 900)])
        self.assertEqual(hash_, NOEND)
        self.assertLess(time.time() - began, 5)

    def test_a_source_still_opening_after_the_race_is_removed_not_kept(self):
        slow = 'e' * 40
        FakeEngine.slow_open = {slow: 1.5}
        hash_, _, _ = self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40), self.candidate(slow, 400, '2160p')])
        self.assertEqual(hash_, LIVE)
        time.sleep(2.2)
        self.assertIn(slow, FakeEngine.removed)
        FakeEngine.slow_open = {}

    def test_status_survives_a_torrent_the_engine_dropped(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        FakeEngine.gone.add(LIVE)
        status = self.streams.status(LIVE)
        self.assertEqual((status['phase'], status['seeds'], status['speed']), ('ready', 0, 0))

    def test_a_stream_the_engine_dropped_is_started_again_not_reported_ready(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        FakeEngine.gone.add(LIVE)
        FakeEngine.created.clear()
        self.assertEqual(self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])[0], LIVE)
        self.assertEqual([h for h, _ in FakeEngine.created], [LIVE])

    def test_a_chosen_stream_fetches_its_end_and_opening_before_the_player_asks(self):
        self.streams.prime_tail, self.streams.prime_head = 1024 * 1024, 2 * 1024 * 1024
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        tail = f'bytes={len(FILM) - 1024 * 1024}-{len(FILM) - 1}'
        head = f'bytes=0-{2 * 1024 * 1024 - 1}'
        for _ in range(50):
            if tail in FakeEngine.ranges and FakeEngine.ranges.count(head) >= 2:
                break
            time.sleep(0.05)
        self.assertIn(tail, FakeEngine.ranges)
        self.assertLess(FakeEngine.ranges.index(tail), len(FakeEngine.ranges) - 1 - FakeEngine.ranges[::-1].index(head))

    def test_only_the_newest_unplayed_streams_are_kept_warm_and_only_for_a_while(self):
        for hash_ in (LIVE, SLOW):
            self.streams.start('Show', None, (1, 5), candidates=[self.candidate(hash_, 40)])
        self.streams.known[SLOW]['file_index'] = 1
        FakeEngine.ranges.clear()
        self.streams.hold_tick()
        self.assertEqual(FakeEngine.ranges.count('bytes=0-0'), 2)
        for entry in self.streams.known.values():
            entry['hold_until'] = time.time() - 1
        FakeEngine.ranges.clear()
        self.streams.hold_tick()
        self.assertEqual(FakeEngine.ranges, [])

    def test_idle_streams_are_removed_from_the_engine(self):
        self.streams.start('Show', None, (1, 5), candidates=[self.candidate(LIVE, 40)])
        self.streams.known[LIVE]['seen'] = 0
        self.streams.sweep()
        self.assertNotIn(LIVE, self.streams.known)
        self.assertIn(LIVE, FakeEngine.removed)


if __name__ == '__main__':
    unittest.main()
