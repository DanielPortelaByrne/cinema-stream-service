#!/usr/bin/env python3
"""Stream films straight to the TV from the same indexers that feed the library.

POST /start   {"title": "...", "year": 1999}  -> {"path": "/stream/<hash>/<ticket>", "label": "...", "bytes": n}
POST /prepare {"title": "...", "year": 1999}  -> 202 at once; searches in the background so a later /start is quick
POST /voice/request {"query": "download dune part two"}  -> files a Seerr request (needs SEERR_API_KEY and VOICE_TOKEN)
GET  /stream/<hash>/<ticket>                           -> the film's bytes, with Range support while it downloads
GET  /health                                  -> {"ok": true}

Candidates come from Prowlarr (the indexers Radarr searches). Only releases with enough seeders, a
plausible film size and a matching title are kept. The chosen torrent is added to qBittorrent with
sequential download, and only its film file is selected. The server reads that file as it grows.
No API keys or session cookies leave this process, and logs never include them.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import datetime as dt
import base64
import hmac
import http.cookiejar
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from configuration import Settings
from playback import PlaybackTickets
from voice import Voice

LOG = logging.getLogger('stream-service')
HOST_MEDIA = Path('./media')
QBIT_MEDIA = '/media'
PROWLARR = 'http://127.0.0.1:9696'
QBIT = 'http://127.0.0.1:8080'
SEERR = 'http://127.0.0.1:5055'
CATEGORY = 'cinema-stream'
MOVIE_CATEGORY = 2000
TV_CATEGORY = 5000
MIN_SEEDERS = 5
MIN_BYTES = 300 * 1024 * 1024
MAX_BYTES = 12 * 1024 ** 3
MIN_EPISODE_BYTES = 80 * 1024 * 1024    # an episode is far smaller than a film
MAX_EPISODE_BYTES = 8 * 1024 ** 3
MAX_BODY = 65536           # a request may carry up to 60 add-on candidates
STALL_SECONDS = 60          # no new bytes for this long means the stream has stalled
QUICK_POLL_SECONDS = 0.25   # how often a waiting stream checks for newly written bytes
COPY_CHUNK_BYTES = 256 * 1024   # small enough to start sending sooner, large enough not to thrash the socket
READY_BYTES = 16 * 1024 * 1024  # how much must be ready before the loading screen hands over to playback
FFMPEG = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'
IDLE_SECONDS = 4 * 3600     # an unwatched stream is removed after this long
START_TIMEOUT = 90          # waiting for qBittorrent to fetch torrent metadata
SEARCH_TTL = 20 * 60        # a search answer is reused this long (a /prepare, then the /start that follows it)
INDEXER_SECONDS = 15        # one indexer that has not answered by now is skipped
ADDON_SECONDS = 25          # a public Stremio add-on that has not answered by now is skipped
SEARCH_SECONDS = 30         # the whole search gives up on the indexers still silent by then
# Public Stremio add-ons that answer for anyone (no account, no configuration). Their streams come by info hash.
# STREAM_ADDONS replaces this list (comma-separated base URLs); "none" turns the add-on lookup off.
ADDONS = ('https://torrentio.strem.fun', 'https://mediafusion.elfhosted.com')
CINEMETA = 'https://v3-cinemeta.strem.io'   # finds a title's IMDb id, which the add-ons key on
# Public trackers added to a magnet made from an info hash, so the peers Stremio's add-ons use can be found too.
TRACKERS = ('udp://tracker.opentrackr.org:1337/announce', 'udp://open.stealth.si:80/announce',
            'udp://tracker.torrent.eu.org:451/announce', 'udp://exodus.desync.com:6969/announce',
            'udp://open.demonii.com:1337/announce', 'udp://tracker.moeking.me:6969/announce',
            'http://tracker.openbittorrent.com:80/announce')
FILM_MINUTES = 110          # running times assumed when the request does not say
EPISODE_MINUTES = 45
MBPS_PER_SEEDER = 1.5       # roughly what one seeder sustains for a sequential download over a home line
STREAM_HEADROOM = 0.5       # a release should need at most this share of what its seeders can sustain
VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.m4v', '.webm', '.avi', '.mov')
BAD_RELEASE = re.compile(r'\b(cam|camrip|hdcam|ts|telesync|tc|telecine|screener|scr|r5|dvdscr|workprint)\b', re.I)
RESOLUTIONS = (('2160', 2.5), ('1080', 3.0), ('720', 2.0), ('480', 0.5))
SOURCES = (('remux', 2.0), ('bluray', 2.0), ('blu-ray', 2.0), ('web-dl', 2.0), ('webdl', 2.0), ('webrip', 1.0), ('web-rip', 1.0), ('hdtv', 0.5))


class ServiceError(Exception):
    pass


def clean_candidates(raw):
    """Torrents the owner's Stremio add-ons offered, as Cinema sends them. Anything not a well-formed torrent is dropped."""
    if not isinstance(raw, list):
        return []
    kept = []
    for item in raw[:60]:
        if not isinstance(item, dict):
            continue
        info = str(item.get('infoHash', '')).lower()
        seeders, size = item.get('seeders'), item.get('size')
        if re.fullmatch(r'[0-9a-f]{40}', info) and type(seeders) is int and type(size) is int \
                and 0 <= seeders < 10 ** 6 and 0 < size < 10 ** 12:
            kept.append({'title': str(item.get('title', ''))[:300], 'infoHash': info, 'seeders': seeders, 'size': size})
    return kept


def normalize(text):
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def parse_release(name):
    """Resolution, source and rejection flags from a release name. Unknown stays unknown."""
    lowered = (name or '').lower()
    resolution = next((r for r, _ in RESOLUTIONS if r in lowered), None)
    if resolution is None and re.search(r'\b4k\b|uhd', lowered):
        resolution = '2160'
    source = next((s for s, _ in SOURCES if s in lowered), None)
    return {
        'resolution': resolution,
        'source': source,
        'bad': bool(BAD_RELEASE.search(lowered)),
        'multi_season': bool(re.search(r's\d{1,2}\s*-\s*s?\d{1,2}|complete series|season \d+\s*-\s*\d+', lowered)),
    }


def episode_marker(season, episode):
    """The SxxExx tag a release names its episode with, as a regex that does not match a longer number."""
    return re.compile(r"(?<![a-z0-9])s0*%de0*%d(?![0-9])" % (int(season), int(episode)), re.I)


def title_matches(title, year, release):
    """Every word of the film's title must appear in the release name, and a year, if given, must too."""
    words = normalize(title).split()
    haystack = ' ' + normalize(release) + ' '
    if not words or any(' ' + w + ' ' not in haystack for w in words):
        return False
    if year and not re.search(r'(?<!\d)%d(?!\d)' % int(year), release):
        return False
    return True


EPISODE_TAG = re.compile(r'(?<![a-z0-9])s\d{1,2}(?:e\d|(?![a-z0-9]))|(?<![a-z0-9])\d{1,2}x\d{2}|season\s*\d|complete', re.I)
SHOW_QUALIFIERS = {'us'}   # "The Office US" is still The Office; "UK", "Netherlands" and the like are different shows


def show_title_is(title, release):
    """Whether the words before a release's season/episode tag are this show and no other ("... Netherlands" is another show)."""
    tag = EPISODE_TAG.search(release or '')
    head = normalize(release[:tag.start()] if tag else release).split()
    while head and (head[-1] in SHOW_QUALIFIERS or re.fullmatch(r'(?:19|20)\d\d', head[-1])):
        head.pop()
    return head == normalize(title).split()


PACK_EPISODES = 10   # a season pack's size is shared out over about this many episodes when judging its bitrate


def season_pack(name, season):
    """Whether a release name is a season's pack (S01 with no episode tag, or Season 1, or COMPLETE)."""
    return re.search(r'(?<![a-z0-9])s0*%d(?![0-9e])|season\s*0*%d(?![0-9])|complete' % (season, season), name or '', re.I) is not None


def acceptable(result, title, year, episode=None):
    """Whether a Prowlarr result is worth trying at all. Returns (keep, reason).

    With episode = (season, episode) the result is that episode's own release, or a pack of the season that holds it. The
    pack's episode file is chosen once its files are known. Packs of several seasons are refused.
    """
    name = result.get('title') or ''
    seeders = result.get('seeders')
    size = result.get('size') or 0
    info = parse_release(name)
    if not isinstance(seeders, int) or seeders < MIN_SEEDERS:
        return False, 'seeders'
    pack = episode is not None and not episode_marker(*episode).search(name)
    low, high = (MIN_BYTES, MAX_BYTES) if episode is None or pack else (MIN_EPISODE_BYTES, MAX_EPISODE_BYTES)
    if size < low or size > high:
        return False, 'size'
    if info['bad']:
        return False, 'poor copy'
    if info['multi_season']:
        return False, 'not a film' if episode is None else 'several seasons'
    if episode is not None:
        if pack and not season_pack(name, episode[0]):
            return False, 'other episode'
        if pack and re.search(r'extras?|bonus|featurette|sample|trailer', name, re.I):
            return False, 'extras'
        if not pack and not episode_marker(*episode).search(name):
            return False, 'other episode'
        if not title_matches(title, None, name) or not show_title_is(title, name):
            return False, 'title'
    elif not title_matches(title, year, name):
        return False, 'title'
    if magnet_for(result, title) is None:
        return False, 'no link'
    return True, ''


# Word starts only: release names run codecs into numbers ("DDP5 1"), so a trailing \b would miss them.
HARD_AUDIO = re.compile(r'(?<![a-z])(ddp|dd\+|e-?ac-?3|dts|truehd)', re.I)
PLAIN_AUDIO = re.compile(r'(?<![a-z])(ac-?3|aac|dd ?5[ .]1)', re.I)
AUDIO_PENALTY = 3.0   # the launcher's player (Chromecast HD class) cannot decode these, so the film would play silent


def audio_unplayable(name):
    """True when the release's audio is E-AC-3/DTS/TrueHD and carries no AC-3/AAC track to fall back on."""
    return bool(HARD_AUDIO.search(name or '')) and not PLAIN_AUDIO.search(name or '')


def score(result):
    """Higher is better: resolution first, then source quality, then how well it is seeded, and small files last."""
    info = parse_release(result.get('title'))
    resolution = dict(RESOLUTIONS).get(info['resolution'], 1.0)
    source = dict(SOURCES).get(info['source'], 0.0)
    seeds = math.log10(max(result.get('seeders') or 0, 1))
    size_gb = (result.get('size') or 0) / 1024 ** 3
    oversize = max(0.0, size_gb - 8.0) * 0.5
    silent = AUDIO_PENALTY if audio_unplayable(result.get('title')) else 0.0
    return resolution * 2 + source + seeds - oversize - silent


def stream_score(result, runtime_minutes=None, episode=None, any_audio=False):
    """Like score(), but for playing while it downloads: a release must arrive faster than it plays.

    The bitrate is estimated from size and running time and set against what its seeders can typically sustain. A
    heavy release with few seeders, which would win on resolution alone and then stop to buffer, is pushed down; seeders
    count for more than in score(). The download pipeline keeps score() unchanged. With any_audio (a player that decodes
    E-AC-3, DTS and TrueHD itself) those soundtracks are not penalised.
    """
    info = parse_release(result.get('title'))
    resolution = dict(RESOLUTIONS).get(info['resolution'], 1.0)
    source = dict(SOURCES).get(info['source'], 0.0)
    seeders = max(result.get('seeders') or 0, 1)
    minutes = runtime_minutes or (EPISODE_MINUTES if episode is not None else FILM_MINUTES)
    size = result.get('size') or 0
    if episode is not None and not episode_marker(*episode).search(result.get('title') or ''):
        size /= PACK_EPISODES   # a season pack: one episode's share of it plays
    mbps = size * 8 / (minutes * 60) / 1e6
    comfortable = seeders * MBPS_PER_SEEDER * STREAM_HEADROOM
    starved = 3.0 * math.log2(mbps / comfortable) if mbps > comfortable > 0 else 0.0
    silent = AUDIO_PENALTY if not any_audio and audio_unplayable(result.get('title')) else 0.0
    return resolution * 2 + source + 2 * math.log10(seeders) - starved - silent


SEEDED_SHARE = 0.5   # the releases kept for quality are those with at least this share of the best seed count


def pick(results, title, year, episode=None, runtime_minutes=None, any_audio=False):
    """Best acceptable result to stream, or None.

    It must load first, so only the most seeded releases are considered (at least SEEDED_SHARE of the best seed count).
    Among those, the highest quality wins.
    """
    kept = [r for r in results if acceptable(r, title, year, episode)[0]]
    if not kept:
        return None
    best_seeds = max(int(r.get('seeders') or 0) for r in kept)
    seeded = [r for r in kept if int(r.get('seeders') or 0) >= max(MIN_SEEDERS, best_seeds * SEEDED_SHARE)]
    return max(seeded, key=lambda r: stream_score(r, runtime_minutes, episode, any_audio))


imdb_ids = {}   # (cinemeta, title, year, kind) -> IMDb id, or None when Cinemeta has no such title


def fetch_json(url, seconds):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=seconds) as response:
        return json.loads(response.read() or b'{}')


def imdb_id(title, year, kind, cinemeta=CINEMETA):
    """The IMDb id of a film or series, found by its name on Cinemeta."""
    key = (cinemeta, normalize(title), str(year or ''), kind)
    if key not in imdb_ids:
        found = None
        for meta in fetch_json(f'{cinemeta}/catalog/{kind}/top/search={urllib.parse.quote(title)}.json', INDEXER_SECONDS).get('metas') or []:
            same_year = not year or str(year) in str(meta.get('releaseInfo') or '')
            if normalize(meta.get('name')) == normalize(title) and same_year:
                found = meta.get('id')
                break
        imdb_ids[key] = found
    return imdb_ids[key]


def addon_results(base, title, year, episode, cinemeta=CINEMETA):
    """One public Stremio add-on's streams for the title, in the same shape as a Prowlarr result."""
    kind = 'movie' if episode is None else 'series'
    imdb = imdb_id(title, year, kind, cinemeta)
    if imdb is None:
        return []
    path = imdb if episode is None else '%s:%d:%d' % (imdb, int(episode[0]), int(episode[1]))
    results = []
    for stream in fetch_json(f'{base}/stream/{kind}/{path}.json', ADDON_SECONDS).get('streams') or []:
        info = (stream.get('infoHash') or '').lower()
        text = stream.get('title') or ''
        name = text.split('\n')[0].strip()
        seeds = re.search('\U0001F464\\s*(\\d+)', text)            # the person icon before the seeders
        size = re.search('\U0001F4BE\\s*([\\d.,]+)\\s*(GB|MB)', text)   # the disc icon before the size
        if not (re.fullmatch(r'[0-9a-f]{40}', info) and name and seeds and size):
            continue
        unit = 1024 ** 3 if size.group(2) == 'GB' else 1024 ** 2
        results.append({'title': name, 'infoHash': info, 'seeders': int(seeds.group(1)),
                        'size': int(float(size.group(1).replace(',', '.')) * unit)})
    return results


class SearchCache:
    """Search answers per (title, year, episode) for SEARCH_TTL seconds, with one search in flight per key.

    A /prepare when a title page opens fills it; the /start that follows a moment later then skips the indexers.
    """

    def __init__(self, search, ttl=SEARCH_TTL, clock=time.time):
        self.search = search
        self.ttl = ttl
        self.clock = clock
        self.lock = threading.Lock()
        self.answers = {}    # key -> (when, results)
        self.running = {}    # key -> threading.Event

    @staticmethod
    def key(title, year, episode=None):
        return (normalize(title), str(year or ''), tuple(int(x) for x in episode) if episode else None)

    def fresh(self, k):
        hit = self.answers.get(k)
        return hit[1] if hit and self.clock() - hit[0] < self.ttl else None

    def get(self, title, year, episode=None):
        """Results for the key: from the cache when fresh, from the search already running for it, or searched now."""
        k = self.key(title, year, episode)
        while True:
            with self.lock:
                hit = self.fresh(k)
                if hit is not None:
                    return hit
                running = self.running.get(k)
                mine = running is None
                if mine:
                    running = self.running[k] = threading.Event()
            if not mine:
                running.wait(70)
                with self.lock:
                    hit = self.fresh(k)
                if hit is not None:
                    return hit
                continue   # that search failed: try again ourselves
            try:
                results = self.search(title, year, episode)
                with self.lock:
                    now = self.clock()
                    self.answers = {x: v for x, v in self.answers.items() if now - v[0] < self.ttl}
                    self.answers[k] = (now, results)
                return results
            finally:
                with self.lock:
                    self.running.pop(k, None)
                running.set()

    def warm(self, title, year, episode=None):
        """Starts the search in the background (unless fresh or already running). Returns at once: True if it started."""
        k = self.key(title, year, episode)
        with self.lock:
            if self.fresh(k) is not None or k in self.running:
                return False

        def run():
            try:
                self.get(title, year, episode)
            except Exception:   # noqa: BLE001 - a failed warm-up is simply retried by /start itself
                pass
        threading.Thread(target=run, daemon=True).start()
        return True


def hash_from_magnet(url):
    match = re.search(r'btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})', url or '')
    if not match:
        return None
    value = match.group(1)
    return value.lower() if len(value) == 40 else base64.b32decode(value.upper()).hex()


def magnet_for(result, title):
    """A magnet link qBittorrent can use on its own, or None.

    Prowlarr's magnetUrl is a proxy on this machine's localhost, which the qBittorrent container cannot reach,
    so the info hash is used instead. A real magnet: link is used only when there is no hash.
    """
    link = result.get('magnetUrl') or ''
    if link.startswith('magnet:') and hash_from_magnet(link):
        return link  # Preserve tracker parameters, including those required by private trackers.
    info = (result.get('infoHash') or '').lower()
    if re.fullmatch(r'[0-9a-f]{40}', info):
        return ('magnet:?xt=urn:btih:' + info + '&dn=' + urllib.parse.quote(title or '')
                + ''.join('&tr=' + urllib.parse.quote(tracker, safe='') for tracker in TRACKERS))
    return None


def video_file(files, episode=None):
    """The largest video file in a torrent, as (index, name, size), or None.

    With episode = (season, episode), only a video file carrying that episode's SxxExx tag counts.
    """
    marker = episode_marker(*episode) if episode is not None else None
    videos = [(f.get('index'), f.get('name', ''), f.get('size', 0)) for f in files
              if f.get('name', '').lower().endswith(VIDEO_EXTENSIONS) and (marker is None or marker.search(f.get('name', '')))]
    return max(videos, key=lambda v: v[2]) if videos else None


def parse_range(header, total):
    """(start, end) inclusive for a single 'bytes=' range, or None when unsatisfiable or absent."""
    if not header:
        return 0, total - 1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    if not match.group(1):
        length = int(match.group(2))
        start, end = max(total - length, 0), total - 1
    else:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else total - 1
        end = min(end, total - 1)
    if start > end or start >= total:
        return None
    return start, end


class Streams:
    """Turns a title into a torrent, keeps its film file known, and removes idle ones."""

    def __init__(self, *, prowlarr=PROWLARR, qbit=QBIT, seerr=SEERR, host_media=HOST_MEDIA, qbit_media=QBIT_MEDIA,
                 prowlarr_api_key='', qbit_username='', qbit_password='', tickets=None,
                 category=CATEGORY, addons=ADDONS, cinemeta=CINEMETA):
        self.category = category
        self.addons, self.cinemeta = tuple(addons), cinemeta
        self.prowlarr_api_key = prowlarr_api_key
        self.qbit_username = qbit_username
        self.qbit_password = qbit_password
        self.tickets = tickets or PlaybackTickets()
        self.prowlarr = prowlarr
        self.qbit_base = qbit
        self.seerr = seerr
        self.host_media = Path(host_media)
        self.qbit_media = qbit_media
        # qBittorrent runs in a container that sees the host's media root as /media, so it gets the container path.
        self.cache = self.host_media / 'Downloads' / 'stream'
        self.cache.mkdir(mode=0o770, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.known = {}     # hash -> {"path": str, "size": int, "label": str, "seen": float}
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.qbit_ready = False
        self.pending = {}   # hash -> {"label", "error"} for films whose metadata has not arrived yet
        self.searches = SearchCache(lambda title, year, episode: self.search(title, year, episode))

    # --- Prowlarr ---------------------------------------------------------------------------------

    def search(self, title, year, episode=None):
        key = self.prowlarr_api_key
        if not key:
            raise ServiceError('Prowlarr API key is not configured')
        if episode is None:
            text, category = (f'{title} {year}' if year else title), MOVIE_CATEGORY
        else:
            text, category = f'{title} S{int(episode[0]):02d}E{int(episode[1]):02d}', TV_CATEGORY
        headers = {'X-Api-Key': key, 'Accept': 'application/json'}
        try:
            with urllib.request.urlopen(urllib.request.Request(f'{self.prowlarr}/api/v1/indexer', headers=headers), timeout=10) as response:
                indexers = [i['id'] for i in json.loads(response.read() or b'[]') if i.get('enable')]
        except (urllib.error.URLError, ValueError, OSError, KeyError, TypeError):
            LOG.info('Prowlarr indexer list unavailable; asking the add-ons only')
            indexers = []

        # Each indexer is asked on its own and all at once. One that is slow or failing is skipped, so it cannot hold up
        # the answers from the rest (Prowlarr otherwise waits for every indexer before it replies).
        def ask(indexer):
            query = urllib.parse.urlencode({'query': text, 'type': 'search', 'categories': category, 'limit': 100, 'indexerIds': indexer})
            req = urllib.request.Request(f'{self.prowlarr}/api/v1/search?{query}', headers=headers)
            with urllib.request.urlopen(req, timeout=INDEXER_SECONDS) as response:
                return json.loads(response.read() or b'[]')

        def ask_addon(base):
            return addon_results(base, title, year, episode, self.cinemeta)

        workers = len(indexers) + len(self.addons)
        if not workers:
            raise ServiceError('indexers unavailable')
        pool = ThreadPoolExecutor(max_workers=workers)
        asked = {pool.submit(ask, i): f'indexer {i}' for i in indexers}
        asked.update({pool.submit(ask_addon, base): base for base in self.addons})
        done, late = wait(asked, timeout=SEARCH_SECONDS)
        pool.shutdown(wait=False, cancel_futures=True)
        results, answered = [], 0
        for future in done:
            try:
                results.extend(future.result())
                answered += 1
            except (urllib.error.URLError, ValueError, OSError, TypeError, AttributeError):
                LOG.info('%s failed, skipped', asked[future])
        for future in late:
            LOG.info('%s too slow, skipped', asked[future])
        if not answered:
            raise ServiceError('indexers unavailable')
        return results

    # --- qBittorrent ------------------------------------------------------------------------------

    def qbit(self, path, form=None):
        """Authenticate through qBittorrent's Web API; no configuration databases are opened."""
        def send(endpoint, data=None):
            request = urllib.request.Request(self.qbit_base + endpoint,
                data=urllib.parse.urlencode(data).encode() if data is not None else None,
                headers={'Referer': self.qbit_base + '/'})
            return self.client.open(request, timeout=20)

        def result(response):
            body = response.read().strip()
            if body in (b'', b'Ok.'):
                return None
            if body == b'Fails.':
                raise ServiceError('qBittorrent rejected the operation')
            try:
                return json.loads(body)
            except ValueError:
                raise ServiceError('Unexpected qBittorrent response') from None

        with self.lock:
            try:
                with send(path, form) as response:
                    return result(response)
            except urllib.error.HTTPError as error:
                if error.code not in (401, 403):
                    raise ServiceError('qBittorrent HTTP %d' % error.code) from None
            except (urllib.error.URLError, OSError):
                raise ServiceError('qBittorrent unavailable') from None
            if not self.qbit_username or not self.qbit_password:
                raise ServiceError('qBittorrent credentials are not configured')
            try:
                with send('/api/v2/auth/login', {'username': self.qbit_username, 'password': self.qbit_password}) as response:
                    # Success is "Ok." on 4.x and an empty 204 on newer releases; anything else ("Fails.") is a refusal.
                    if response.read().strip() not in (b'', b'Ok.'):
                        raise ServiceError('qBittorrent sign-in failed')
                with send(path, form) as response:
                    return result(response)
            except ServiceError:
                raise
            except (urllib.error.URLError, OSError):
                raise ServiceError('qBittorrent authentication unavailable') from None

    def to_qbit(self, host_path):
        """The container path qBittorrent knows for a host path under the media root."""
        return self.qbit_media.rstrip('/') + '/' + Path(host_path).relative_to(self.host_media).as_posix()

    def to_host(self, qbit_path):
        """Map the configured qBittorrent directory without permitting traversal outside it."""
        if not isinstance(qbit_path, str) or '\\' in qbit_path or '..' in PurePosixPath(qbit_path).parts:
            raise ServiceError('unexpected download path')
        try:
            relative = PurePosixPath(qbit_path).relative_to(PurePosixPath(self.qbit_media))
            mapped = (self.host_media / str(relative)).resolve()
            mapped.relative_to(self.host_media.resolve())
            return mapped
        except (ValueError, OSError):
            raise ServiceError('unexpected download path') from None

    def restore(self):
        """After a restart, adopts stream torrents qBittorrent still holds, so their data is still served and swept."""
        for row in self.qbit('/api/v2/torrents/info?category=' + self.category) or []:
            files = self.qbit('/api/v2/torrents/files?hash=' + row['hash']) or []
            chosen = video_file(files)
            if chosen is None:
                continue
            self.known[row['hash']] = self.entry_for(row['hash'], row['save_path'], files, chosen)

    def entry_for(self, hash_, save_path, files, chosen):
        """What the server needs to serve one film: where it is, how big, and where it sits among the torrent's pieces."""
        index, name, size = chosen
        props = self.qbit('/api/v2/torrents/properties?hash=' + hash_) or {}
        label = re.sub(r'[\._]+', ' ', Path(name).stem)[:120]
        if '\\' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ServiceError('unexpected video path')
        path = (self.to_host(save_path) / name).resolve()
        try:
            path.relative_to(self.cache.resolve())
        except ValueError:
            raise ServiceError('video is outside the stream cache') from None
        return {
            'hash': hash_, 'path': str(path), 'size': size, 'label': label,
            'offset': sum(f.get('size', 0) for f in files if f.get('index', 0) < index),
            'piece_size': props.get('piece_size') or 262144, 'pieces': [], 'pieces_at': 0.0, 'seen': time.time(),
            # Audio this TV cannot decode is converted to AAC while it is served; video is copied untouched.
            'transcode': audio_unplayable(name),
        }

    def downloaded_until(self, entry, position):
        """Film-relative byte just past the run of finished pieces that starts at [position].

        A sparse file reports its full size before it is written, so file size says nothing about what has arrived.
        qBittorrent's piece states do: 2 means downloaded. The list is refreshed at most once a second.
        """
        if time.time() - entry['pieces_at'] > 1.0:
            entry['pieces'] = self.qbit('/api/v2/torrents/pieceStates?hash=' + entry['hash']) or []
            entry['pieces_at'] = time.time()
        pieces, size = entry['pieces'], entry['piece_size']
        absolute = entry['offset'] + position
        piece = absolute // size
        while piece < len(pieces) and pieces[piece] == 2:
            piece += 1
        return min(piece * size - entry['offset'], entry['size'])

    def torrent(self, hash_):
        rows = self.qbit('/api/v2/torrents/info?hashes=' + hash_) or []
        return rows[0] if rows else None

    def owns_torrent(self, row):
        if not row or row.get('category') != self.category:
            return False
        try:
            self.to_host(row.get('save_path', '')).relative_to(self.cache.resolve())
            return True
        except (ServiceError, ValueError):
            return False

    # --- Starting a film --------------------------------------------------------------------------

    def prepare(self, title, year, episode=None):
        """Searches for the title in the background, so a /start that follows skips the indexers. Returns at once."""
        return self.searches.warm(title, year, episode)

    def ensure_category(self):
        with self.lock:
            if not self.qbit_ready:
                categories = self.qbit('/api/v2/torrents/categories') or {}
                if self.category not in categories:
                    self.qbit('/api/v2/torrents/createCategory', {'category': self.category})
                self.qbit_ready = True

    def start(self, title, year, episode=None, runtime_minutes=None, any_audio=False, candidates=()):
        """Finds the best copy and adds it to qBittorrent. Returns at once; the film's details arrive in the background.

        episode is (season, episode) for a TV episode, or None for a film.
        Returns (hash, label, phase) where phase is 'ready' or 'connecting'.
        """
        results = list(self.searches.get(title, year, episode)) + list(candidates)
        choice = pick(results, title, year, episode, runtime_minutes, any_audio)
        if choice is None:
            raise LookupError('no source')
        link = magnet_for(choice, title)
        if link is None:
            raise LookupError('no link')
        hash_ = hash_from_magnet(link)
        label = re.sub(r'[\._]+', ' ', choice.get('title') or title)[:120]
        with self.lock:
            if hash_ in self.known:
                self.known[hash_]['seen'] = time.time()
                return hash_, self.known[hash_]['label'], 'ready'
            if hash_ in self.pending and not self.pending[hash_]['error']:
                return hash_, self.pending[hash_]['label'], 'connecting'
            self.pending[hash_] = {'label': label, 'error': None, 'episode': episode}
        try:
            existing = self.torrent(hash_)
            if existing and not self.owns_torrent(existing):
                raise ServiceError('This source already belongs to another download')
            self.ensure_category()
            self.qbit('/api/v2/torrents/add', {
                'urls': link, 'savepath': self.to_qbit(self.cache), 'category': self.category,
                'sequentialDownload': 'true', 'firstLastPiecePrio': 'true', 'paused': 'false', 'stopped': 'false'})
        except ServiceError:
            with self.lock:
                self.pending.pop(hash_, None)
            raise
        threading.Thread(target=self.resolve, args=(hash_,), daemon=True).start()
        return hash_, label, 'connecting'

    def resolve(self, hash_):
        """Background: waits for the torrent's metadata, so the film can be served. A failure is kept for /status."""
        try:
            self.await_film(hash_)
            with self.lock:
                self.pending.pop(hash_, None)
        except (LookupError, TimeoutError, ServiceError) as error:
            LOG.warning('a film did not start: %s', error or 'timed out')
            with self.lock:
                if hash_ in self.pending:
                    self.pending[hash_]['error'] = str(error) or 'timed out'
            try:
                if self.owns_torrent(self.torrent(hash_)):
                    self.qbit('/api/v2/torrents/delete', {'hashes': hash_, 'deleteFiles': 'true'})
            except ServiceError:
                pass

    def wait_for_entry(self, hash_):
        """The film's entry once its metadata has arrived; None if it never does or the start failed."""
        deadline = time.time() + START_TIMEOUT + 10
        while True:
            with self.lock:
                entry = self.known.get(hash_)
                pending = self.pending.get(hash_)
            if entry is not None:
                entry['seen'] = time.time()
                return entry
            if pending is None or pending['error'] or time.time() > deadline:
                return None
            time.sleep(0.25)

    def status(self, hash_):
        """What the player's loading screen shows: where the film is in starting up, and how much is ready."""
        with self.lock:
            entry = self.known.get(hash_)
            pending = self.pending.get(hash_)
        if entry is None:
            if pending is None:
                return None
            if pending['error']:
                return {'phase': 'failed', 'label': pending['label']}
            row = self.torrent(hash_) or {}
            return {'phase': 'connecting', 'label': pending['label'],
                    'seeds': row.get('num_seeds', 0), 'peers': row.get('num_leechs', 0),
                    'speed': row.get('dlspeed', 0)}
        row = self.torrent(hash_) or {}
        ready = self.downloaded_until(entry, 0) >= min(READY_BYTES, entry['size'])
        # speed (bytes/s) and size (bytes of the film file) feed the player's loading screen and Stream card.
        return {'phase': 'ready' if ready else 'buffering', 'label': entry['label'],
                'seeds': row.get('num_seeds', 0), 'peers': row.get('num_leechs', 0),
                'progress': round(row.get('progress', 0.0), 4),
                'speed': row.get('dlspeed', 0), 'size': entry['size']}

    def await_film(self, hash_):
        """Waits for metadata, then selects only the film file, so the download follows its pieces in order."""
        deadline = time.time() + START_TIMEOUT
        with self.lock:
            episode = (self.pending.get(hash_) or {}).get('episode')
        while time.time() < deadline:
            rows = self.qbit('/api/v2/torrents/info?category=' + self.category) or []
            row = next((r for r in rows if hash_ and r.get('hash') == hash_), None)
            if row is None and not hash_ and rows:
                row = max(rows, key=lambda r: r.get('added_on', 0))
            if row and row.get('state') not in ('metaDL', 'checkingResumeData'):
                hash_ = row['hash']
                # Start it now: qBittorrent's active-download limit would otherwise queue the film behind the library's downloads.
                self.qbit('/api/v2/torrents/setForceStart', {'hashes': hash_, 'value': 'true'})
                files = self.qbit('/api/v2/torrents/files?hash=' + hash_) or []
                if not files:
                    # Right after the add, qBittorrent can report a state before it has the file list. Wait for it.
                    time.sleep(2)
                    continue
                chosen = video_file(files, episode)
                if chosen is None:
                    raise LookupError('no video file')
                index, name, size = chosen
                others = '|'.join(str(f['index']) for f in files if f['index'] != index)
                if others:
                    self.qbit('/api/v2/torrents/filePrio', {'hash': hash_, 'id': others, 'priority': 0})
                self.qbit('/api/v2/torrents/filePrio', {'hash': hash_, 'id': str(index), 'priority': 1})
                entry = self.entry_for(hash_, row['save_path'], files, chosen)
                self.known[hash_] = entry
                return hash_, entry
            time.sleep(2)
        raise TimeoutError('metadata')

    def sweep(self):
        """Removes torrents nobody has watched for a while, with their data."""
        now = time.time()
        with self.lock:
            stale = [h for h, e in self.known.items() if now - e['seen'] > IDLE_SECONDS]
            for hash_ in stale:
                try:
                    if self.owns_torrent(self.torrent(hash_)):
                        self.qbit('/api/v2/torrents/delete', {'hashes': hash_, 'deleteFiles': 'true'})
                except ServiceError:
                    continue
                self.known.pop(hash_, None)
                LOG.info('removed an idle stream')

    def file_for(self, hash_):
        with self.lock:
            entry = self.known.get(hash_)
            if entry is None:
                return None
            entry['seen'] = time.time()
            return entry


class Handler(BaseHTTPRequestHandler):
    server_version = 'Cinema-Streams'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(STALL_SECONDS)

    def log_message(self, *_):
        pass  # No paths, cookies or indexer responses in logs.

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def discard_body(self):
        """Reads and drops an unwanted request body, so replying early does not reset the client's connection."""
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)

    def authenticate(self):
        """Same Seerr sign-in the request pipeline uses: only household members with a Seerr account can start a stream."""
        cookie = self.headers.get('Cookie', '')
        if len(cookie) > 8192 or not re.search(r'(?:^|;\s*)connect\.sid=', cookie):
            LOG.info('start rejected: no Seerr session cookie sent')
            self.discard_body()
            self.reply(401, {'error': 'Sign in to Seerr'})
            return False
        try:
            request = urllib.request.Request(self.server.streams.seerr + '/api/v1/auth/me', headers={'Cookie': cookie, 'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                user = json.loads(response.read() or b'{}')
        except (urllib.error.URLError, ValueError, OSError):
            user = {}
        if not user.get('id'):
            self.discard_body()
            self.reply(401, {'error': 'Sign in to Seerr'})
            return False
        return True

    def do_GET(self):
        path, _, query = self.path.partition('?')
        if path == '/health':
            self.reply(200, {'ok': True})
            return
        parts = path.strip('/').split('/')
        if len(parts) != 3 or parts[0] not in ('stream', 'status'):
            self.reply(404, {'error': 'Not found'})
            return
        kind, hash_, ticket = parts
        if not re.fullmatch(r'[0-9a-f]{40}', hash_) or not self.server.streams.tickets.valid(hash_, ticket):
            self.reply(403, {'error': 'Stream link is invalid or expired. Start the stream again.'})
            return
        if kind == 'stream':
            self.serve(hash_, original=urllib.parse.parse_qs(query).get('audio') == ['original'])
        else:
            self.report(hash_)

    def read_request(self):
        """(title, year or None, (season, episode) or None, runtime minutes or None); None once it has replied 400."""
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.reply(400, {'error': 'Invalid Content-Length'})
            self.close_connection = True
            return None
        if not 0 < length <= MAX_BODY:
            self.reply(413 if length > MAX_BODY else 400, {'error': 'Request body must be between 1 and %d bytes' % MAX_BODY})
            self.close_connection = True
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self.reply(400, {'error': 'Invalid JSON'})
            return None
        if not isinstance(body, dict):
            self.reply(400, {'error': 'Expected a JSON object'})
            return None
        title = str(body.get('title', '')).strip()[:200]
        year = str(body.get('year', '')).strip()[:4]
        if not title:
            self.reply(400, {'error': 'Title missing'})
            return None
        season, episode = body.get('season'), body.get('episode')
        episode_key = None
        if season is not None or episode is not None:
            if not (type(season) is int and type(episode) is int and 0 < season < 100 and 0 < episode < 1000):
                self.reply(400, {'error': 'Season and episode must be numbers'})
                return None
            episode_key = (season, episode)
        runtime = body.get('runtime')
        runtime = runtime if isinstance(runtime, int) and 0 < runtime < 1000 else None
        self.any_audio = body.get('audio') == 'any'
        self.candidates = clean_candidates(body.get('candidates'))
        return title, (year if year.isdigit() else None), episode_key, runtime

    def voice_request(self):
        """A spoken "download X" from a routine. Off unless SEERR_API_KEY and VOICE_TOKEN are set; the token is its only guard."""
        voice = getattr(self.server, 'voice', None)
        if voice is None:
            self.reply(404, {'error': 'Not found'})
            return
        given = self.headers.get('Authorization', '')
        if not hmac.compare_digest(given.encode(), ('Bearer ' + self.server.voice_token).encode()):
            LOG.info('voice rejected: wrong or missing token')
            self.reply(401, {'error': 'Unauthorized', 'say': 'That was not allowed.'})
            return
        if not voice.allow():
            self.reply(429, {'error': 'Too many requests', 'say': 'Too many requests, try again in a minute.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else None
        except (ValueError, UnicodeDecodeError):
            body = None
        query = body.get('query') if isinstance(body, dict) else None
        if not isinstance(query, str) or not query.strip():
            self.reply(400, {'error': 'Send {"query": "what to download"}', 'say': "I didn't catch what to download."})
            return
        answer = voice.request(query)
        LOG.info('voice request: %s (%s)', answer.get('title') or '-', answer['status'])
        self.reply(200 if answer['status'] != 'unavailable' else 502, answer)

    def do_POST(self):
        if self.path == '/voice/request':
            self.voice_request()
            return
        if self.path not in ('/start', '/prepare'):
            self.reply(404, {'error': 'Not found'})
            return
        if not self.authenticate():
            return
        title = year = ''
        episode_key = None
        try:
            asked = self.read_request()
            if asked is None:
                return
            title, year, episode_key, runtime = asked
            if self.path == '/prepare':
                searching = self.server.streams.prepare(title, year, episode_key)
                self.reply(202, {'ok': True, 'searching': searching})
                return
            started = time.time()
            hash_, label, phase = self.server.streams.start(title, year, episode_key, runtime, getattr(self, 'any_audio', False),
                                                            getattr(self, 'candidates', []))
            LOG.info('start %s: %r %s %s in %.1fs as %s', phase, title, year, episode_key or '', time.time() - started, label)
            self.reply(200, {'path': '/stream/' + hash_ + '/' + self.server.streams.tickets.issue(hash_), 'label': label, 'phase': phase})
        except LookupError as error:
            LOG.info('start: no acceptable source for %r %s %s (%s)', title, year, episode_key or '', error)
            code = 404 if str(error) in ('no source', 'no video file') else 422
            self.reply(code, {'error': 'No playable stream found'})
        except (ServiceError, ValueError) as error:
            LOG.warning('start failed for %r: %s', title, error)
            self.reply(503, {'error': str(error) if isinstance(error, ServiceError) else 'Bad request'})

    def report(self, hash_):
        """Progress for the player's loading screen. Holds no secrets: the same hash the stream itself needs."""
        if not re.fullmatch(r'[0-9a-f]{40}', hash_):
            self.reply(404, {'error': 'Not found'})
            return
        try:
            info = self.server.streams.status(hash_)
        except ServiceError:
            self.reply(503, {'phase': 'unavailable'})
            return
        if info is None:
            self.reply(404, {'error': 'Not found'})
        else:
            self.reply(200, info)

    def serve(self, hash_, original=False):
        if not re.fullmatch(r'[0-9a-f]{40}', hash_):
            self.reply(404, {'error': 'Not found'})
            return
        entry = self.server.streams.wait_for_entry(hash_)
        if entry is None:
            self.reply(404, {'error': 'Not found'})
            return
        if entry['transcode'] and not original:
            self.serve_transcoded(entry)
            return
        total = entry['size']
        window = parse_range(self.headers.get('Range'), total)
        if window is None:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{total}')
            self.end_headers()
            return
        start, end = window
        partial = bool(self.headers.get('Range'))
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'video/x-matroska' if entry['path'].lower().endswith('.mkv') else 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.copy(entry, start, end)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # The viewer left, or the stream stalled; nothing to tell them.

    def serve_transcoded(self, entry):
        """Serves the film as Matroska with AAC audio, for a TV that cannot decode the original soundtrack.

        ffmpeg reads from a pipe that this method fills with finished pieces only, and writes to the response.
        The result cannot be seeked, so it is served whole; the player can still play straight through.
        """
        command = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
                   '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ac', '2',
                   '-f', 'matroska', 'pipe:1']
        try:
            ffmpeg = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.reply(503, {'error': 'Audio conversion is unavailable'})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'video/x-matroska')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        feeder = threading.Thread(target=self.feed, args=(ffmpeg, entry), daemon=True)
        feeder.start()
        try:
            while True:
                chunk = ffmpeg.stdout.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # The viewer left; stop converting.
        finally:
            ffmpeg.kill()
            ffmpeg.wait()

    def feed(self, ffmpeg, entry):
        """Writes the film to ffmpeg in order, but only the parts whose pieces have arrived."""
        streams = self.server.streams
        position, last_growth = 0, time.time()
        try:
            with Path(entry['path']).open('rb') as handle:
                while position < entry['size']:
                    try:
                        available = streams.downloaded_until(entry, position) - position
                    except ServiceError:
                        available = 0
                    if available <= 0:
                        if time.time() - last_growth > STALL_SECONDS:
                            break
                        time.sleep(QUICK_POLL_SECONDS)
                        continue
                    handle.seek(position)
                    chunk = handle.read(min(available, COPY_CHUNK_BYTES))
                    ffmpeg.stdin.write(chunk)
                    position += len(chunk)
                    entry['seen'] = last_growth = time.time()
        except (BrokenPipeError, OSError, ValueError):
            pass  # ffmpeg stopped reading; the viewer has gone.
        finally:
            try:
                ffmpeg.stdin.close()
            except OSError:
                pass

    def copy(self, entry, start, end):
        """Copies bytes as qBittorrent writes them, waiting for each piece instead of ending early.

        Waits poll every QUICK_POLL_SECONDS, so a piece that lands is sent almost at once rather than up to a second later.
        """
        path = Path(entry['path'])
        position, last_growth = start, time.time()
        handle = None
        streams = self.server.streams
        try:
            while position <= end:
                if handle is None:
                    if not path.exists():
                        if time.time() - last_growth > STALL_SECONDS:
                            return
                        time.sleep(QUICK_POLL_SECONDS)
                        continue
                    handle = path.open('rb')
                handle.seek(position)
                try:
                    # Only finished pieces may be sent. Anything past them is still zeros on disk.
                    available = min(streams.downloaded_until(entry, position), end + 1) - position
                except ServiceError:
                    available = 0
                if available <= 0:
                    if time.time() - last_growth > STALL_SECONDS:
                        return
                    time.sleep(QUICK_POLL_SECONDS)
                    continue
                chunk = handle.read(min(available, end - position + 1, COPY_CHUNK_BYTES))
                if not chunk:
                    time.sleep(QUICK_POLL_SECONDS)
                    continue
                self.wfile.write(chunk)
                position += len(chunk)
                entry['seen'] = last_growth = time.time()
        finally:
            if handle is not None:
                handle.close()


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    voice = None
    voice_token = ''


def main():
    parser = argparse.ArgumentParser(description='Streaming backend for Cinema APK and Flatpak clients')
    parser.add_argument('--check-config', action='store_true', help='Validate configuration without contacting upstream services')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o007)
    try:
        config = Settings.from_env()
        if args.check_config:
            print('Configuration OK')
            return
        tickets = PlaybackTickets.from_file(config.state_root / 'playback.key')
        streams = Streams(prowlarr=config.prowlarr_url, prowlarr_api_key=config.prowlarr_api_key,
                          qbit=config.qbit_url, qbit_username=config.qbit_username, qbit_password=config.qbit_password,
                          seerr=config.seerr_url, host_media=config.media_root, qbit_media=config.qbit_media_root,
                          tickets=tickets, category=config.category,
                          addons=ADDONS if config.addons is None else config.addons)
    except (ValueError, OSError) as error:
        parser.exit(2, f'Configuration error: {error}\n')

    def adopt_existing():
        # Off the main thread, so a slow qBittorrent never delays requests from the TV.
        try:
            streams.restore()
        except ServiceError:
            LOG.warning('qBittorrent unavailable at startup; earlier stream torrents will not be swept')

    threading.Thread(target=adopt_existing, daemon=True).start()

    def housekeeping():
        while True:
            time.sleep(600)
            streams.sweep()

    threading.Thread(target=housekeeping, daemon=True).start()
    server = LimitedServer((config.listen_host, config.port), Handler)
    server.streams = streams
    if config.seerr_api_key and config.voice_token:
        server.voice, server.voice_token = Voice(config.seerr_url, config.seerr_api_key), config.voice_token
        LOG.info('voice requests are on')
    server.serve_forever()


if __name__ == '__main__':
    main()
