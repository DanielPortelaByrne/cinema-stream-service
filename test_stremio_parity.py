"""Regressions for add-on metadata, episode identity and selected-file progress."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import stream_service as svc

HASH = 'a' * 40


class StremioParity(unittest.TestCase):
    def streams(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        streams = svc.Streams(host_media=Path(directory.name), addons=())
        streams.searches.get = lambda *args: []
        streams.end_piece_retries = ()
        return streams

    def candidate(self, **extra):
        return {'title': 'Sharp.Objects.S01E05.1080p', 'infoHash': HASH,
                'size': 1024 ** 3, 'seeders': 50, **extra}

    def test_addon_file_index_and_tracker_survive_sanitizing_and_magnet_creation(self):
        candidate = svc.clean_candidates([self.candidate(fileIdx=4, sources=[
            'tracker:https://tracker.example/announce?passkey=example', 'dht:node', 42])])[0]
        self.assertEqual(candidate['fileIdx'], 4)
        trackers = parse_qs(urlsplit(svc.magnet_for(candidate, 'Sharp Objects')).query)['tr']
        self.assertEqual(trackers, ['https://tracker.example/announce?passkey=example'])

    def test_unknown_display_metadata_is_eligible_but_not_fabricated(self):
        candidate = svc.clean_candidates([self.candidate(seeders=None, size=None)])[0]
        self.assertIsNone(candidate['seeders'])
        self.assertIs(svc.pick([candidate], 'Sharp Objects', None, (1, 5)), candidate)

    def test_season_pack_is_not_rejected_by_a_single_movies_size_limit(self):
        candidate = self.candidate(title='Sharp.Objects.S01.Complete.1080p', size=30 * 1024 ** 3)
        self.assertTrue(svc.acceptable(candidate, 'Sharp Objects', None, (1, 5))[0])

    def test_invalid_file_index_is_not_silently_replaced(self):
        for idx in (-1, True, 1.5, 65536):
            self.assertEqual(svc.clean_candidates([self.candidate(fileIdx=idx)]), [])
        files = [{'index': 0, 'name': 'Sharp.Objects.S01E05.mkv', 'size': 100},
                 {'index': 1, 'name': 'Sharp.Objects.S01E06.mkv', 'size': 200}]
        self.assertEqual(svc.video_file(files, (1, 5), 0), (0, files[0]['name'], 100))
        self.assertIsNone(svc.video_file(files, (1, 5), 1))
        self.assertIsNone(svc.video_file(files, (1, 5), 7))

    def test_reusing_a_pack_switches_to_the_requested_episode(self):
        files = [{'index': 0, 'name': 'Sharp.Objects.S01E04.mkv', 'size': 100},
                 {'index': 1, 'name': 'Sharp.Objects.S01E05.mkv', 'size': 200}]
        for extra in ({'fileIdx': 1}, {}):
            streams = self.streams()
            streams.known[HASH] = {'file_index': 0, 'path': 'Sharp.Objects.S01E04.mkv', 'label': 'Episode 4'}
            calls = []

            def qbit(path, form=None):
                calls.append((path, form))
                return files if '/torrents/files?' in path else {}
            streams.qbit = qbit
            streams.torrent = lambda h: {'hash': HASH, 'save_path': streams.to_qbit(streams.cache)}
            _, _, phase = streams.start('Sharp Objects', None, (1, 5), candidates=[self.candidate(**extra)])
            self.assertEqual(phase, 'ready')
            self.assertEqual(streams.known[HASH]['file_index'], 1)
            self.assertIn(('/api/v2/torrents/filePrio', {'hash': HASH, 'id': '0', 'priority': 0}), calls)
            self.assertIn(('/api/v2/torrents/filePrio', {'hash': HASH, 'id': '1', 'priority': 1}), calls)
            # The new file's first and last pieces (Matroska's seek index) are put first again.
            toggles = [c for c in calls if c[0] == '/api/v2/torrents/toggleFirstLastPiecePrio']
            self.assertEqual(len(toggles), 2)
            self.assertLess(calls.index(('/api/v2/torrents/filePrio', {'hash': HASH, 'id': '1', 'priority': 1})), calls.index(toggles[0]))

    def test_reusing_a_pack_for_the_same_episode_returns_it_as_is(self):
        streams = self.streams()
        streams.known[HASH] = {'file_index': 4, 'path': 'Sharp.Objects.S01E05.mkv', 'label': 'Episode 5'}
        streams.qbit = lambda *args: self.fail('no qBittorrent call is needed')
        self.assertEqual(streams.start('Sharp Objects', None, (1, 5), candidates=[self.candidate(fileIdx=4)])[2], 'ready')

    def test_indexer_failure_does_not_discard_the_owners_candidates(self):
        streams = self.streams()
        streams.searches.get = lambda *args: (_ for _ in ()).throw(svc.ServiceError('indexer down'))
        calls = []
        streams.qbit = lambda path, form=None: calls.append((path, form)) or None
        with patch.object(svc.threading, 'Thread'):
            self.assertEqual(streams.start('Sharp Objects', None, (1, 5), candidates=[self.candidate()])[0], HASH)
        self.assertTrue(any(path.endswith('/torrents/add') for path, _ in calls))

    def test_retry_skips_a_source_whose_metadata_already_failed(self):
        streams = self.streams()
        streams.pending[HASH] = {'error': 'metadata timeout'}
        alternative = self.candidate(infoHash='b' * 40, seeders=30)
        streams.qbit = lambda *args: None
        with patch.object(svc.threading, 'Thread'):
            chosen, _, _ = streams.start('Sharp Objects', None, (1, 5), candidates=[self.candidate(), alternative])
        self.assertEqual(chosen, 'b' * 40)

    def test_metadata_fetch_is_force_started_before_files_are_available(self):
        streams = self.streams()
        streams.pending[HASH] = {'episode': (1, 5), 'fileIdx': 0}
        calls = []

        def qbit(path, form=None):
            calls.append((path, form))
            if '/torrents/info?' in path:
                if len([p for p, _ in calls if '/torrents/info?' in p]) == 1:
                    return [{'hash': HASH, 'state': 'metaDL'}]
                return [{'hash': HASH, 'state': 'downloading', 'save_path': '/media/Downloads/stream'}]
            if '/torrents/files?' in path:
                return [{'index': 0, 'name': 'Sharp.Objects.S01E05.mkv', 'size': 100}]
            return {}

        streams.qbit = qbit
        with patch.object(svc.time, 'sleep'):
            streams.await_film(HASH)
        self.assertEqual(calls[1], ('/api/v2/torrents/setForceStart', {'hashes': HASH, 'value': 'true'}))
        self.assertEqual(streams.known[HASH]['file_index'], 0)

    def test_progress_is_contiguous_selected_file_bytes_not_pack_progress(self):
        streams = self.streams()
        streams.known[HASH] = {'label': 'Episode 5', 'file_index': 4, 'size': 1000}
        streams.torrent = lambda _: {'progress': 0.95}
        streams.downloaded_until = lambda *args: 100
        status = streams.status(HASH)
        self.assertEqual(status['progress'], 0.1)
        self.assertEqual(status['torrentProgress'], 0.95)
        self.assertEqual(status['fileIdx'], 4)

    def test_restore_respects_selected_file_priority(self):
        streams = self.streams()
        files = [{'index': 0, 'name': 'Sharp.Objects.S01E01.mkv', 'size': 200, 'priority': 0},
                 {'index': 1, 'name': 'Sharp.Objects.S01E05.mkv', 'size': 100, 'priority': 1}]
        streams.qbit = lambda path, form=None: ([{'hash': HASH, 'save_path': '/media/Downloads/stream'}]
            if '/torrents/info?' in path else files if '/torrents/files?' in path else {})
        streams.restore()
        self.assertEqual(streams.known[HASH]['file_index'], 1)
