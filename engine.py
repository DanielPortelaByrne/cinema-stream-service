"""Client for Stremio's streaming server, the torrent engine Stremio itself plays through.

It downloads the pieces a reader is waiting for first (the start of a file, its seek index at the end, wherever the player
seeks), which a sequential qBittorrent download cannot do. Run it next to this service, for example the official
stremio/server container, and set STREMIO_SERVER to its address. Only info hashes, file numbers and tracker hints are
sent to it; nothing about the household or its accounts.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class EngineError(Exception):
    pass


class StremioEngine:
    def __init__(self, base, timeout=30):
        self.base = base.rstrip('/')
        self.timeout = timeout

    def _json(self, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data, {'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise EngineError(f'streaming engine unavailable ({error.__class__.__name__})') from None
        try:
            return json.loads(raw or b'{}')
        except ValueError:
            raise EngineError('unexpected answer from the streaming engine') from None

    def open(self, info_hash, sources=(), timeout=None):
        """Starts (or reuses) the torrent and returns its files as [{'index', 'name', 'size'}] once metadata is known."""
        body = {'torrent': {'infoHash': info_hash},
                'peerSearch': {'sources': list(sources) or [f'dht:{info_hash}'], 'min': 40, 'max': 150},
                'guessFileIdx': {}}
        answer = self._json(f'/{info_hash}/create', body, timeout)
        files = answer.get('files') or []
        if not files:
            raise EngineError('no torrent metadata')
        return [{'index': i, 'name': f.get('name') or '', 'path': f.get('path') or '', 'size': int(f.get('length') or 0)}
                for i, f in enumerate(files)]

    def stats(self, info_hash, file_idx):
        return self._json(f'/{info_hash}/{int(file_idx)}/stats.json', timeout=5)

    def remove(self, info_hash):
        try:
            self._json(f'/{info_hash}/remove', timeout=5)
        except EngineError:
            pass

    def url(self, info_hash, file_idx):
        return f'{self.base}/{info_hash}/{int(file_idx)}'

    def fetch(self, info_hash, file_idx, start, end, timeout):
        """Reads bytes [start, end] of a file and throws them away, so the engine has them (and stays busy) before the
        player asks. True when they all arrived."""
        request = urllib.request.Request(self.url(info_hash, file_idx), headers={'Range': f'bytes={start}-{end}'})
        want, got, began = end - start + 1, 0, time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                while got < want and time.time() - began < timeout:
                    chunk = response.read1(256 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
        except (urllib.error.URLError, OSError, ValueError):
            return False
        return got >= want

    def head_time(self, info_hash, file_idx, length, deadline):
        """Reads the first [length] bytes of a file; seconds it took, or None if it did not arrive by [deadline]."""
        started = time.time()
        request = urllib.request.Request(self.url(info_hash, file_idx), headers={'Range': f'bytes=0-{length - 1}'})
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, deadline - started)) as response:
                got = 0
                while got < length and time.time() < deadline:
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
            return time.time() - started if got >= length else None
        except (urllib.error.URLError, OSError, ValueError):
            return None


def sources_for(result, info_hash, fallback_trackers):
    """Peer discovery hints in Stremio's form: the add-on's own, a magnet's trackers, DHT, then public trackers."""
    sources = [s for s in result.get('sources') or [] if isinstance(s, str) and s.startswith(('tracker:', 'dht:'))]
    link = result.get('magnetUrl') or ''
    if link.startswith('magnet:'):
        sources += ['tracker:' + t for t in urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('tr', [])]
    if not any(s.startswith('tracker:') for s in sources):
        sources += ['tracker:' + t for t in fallback_trackers]
    sources.append('dht:' + info_hash)
    return list(dict.fromkeys(sources))
