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
from engine import EngineError, StremioEngine, sources_for
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
MAX_BODY = 512 * 1024      # up to 60 candidates, now including file indexes and tracker hints
STALL_SECONDS = 60          # no new bytes for this long means the stream has stalled
QUICK_POLL_SECONDS = 0.25   # how often a waiting stream checks for newly written bytes
COPY_CHUNK_BYTES = 256 * 1024   # small enough to start sending sooner, large enough not to thrash the socket
READY_BYTES = 16 * 1024 * 1024  # how much must be ready before the loading screen hands over to playback
FFMPEG = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'
IDLE_SECONDS = 4 * 3600     # an unwatched stream is removed after this long
# Seeder counts say little about how fast a source sends its first bytes: on the Deck the best-ranked source was often
# the slowest, and a lower-ranked one delivered in under two seconds. So more sources race, and the best-ranked one
# only gets a short head start.
RACE_CANDIDATES = 6          # the best sources tried at once in the streaming engine; the first to deliver plays
RACE_HEAD_BYTES = 2 * 1024 * 1024   # what each must deliver to count as alive (the player needs it first anyway)
# A Matroska file's seek index sits at its end, and the player reads it before the first frame. On a thinly seeded
# torrent the end can take far longer than the start (or never come), so for those files a source must deliver both.
RACE_TAIL_BYTES = 1024 * 1024
RACE_TAIL_PATIENCE_SECONDS = 6   # once a source has its start, how long to wait for one with its end as well
RACE_PATIENCE_SECONDS = 1.5  # how long the best-ranked source gets before a faster lower-ranked one is taken
RACE_DEADLINE_SECONDS = 30   # no source delivered by then: none of them is playable now
# What a player reads before its first frame, fetched as soon as a stream is chosen so it is already here on Play: the
# file's end (Matroska's seek index, an MP4's index when it is not at the front), then its opening stretch.
PRIME_TAIL_BYTES = 4 * 1024 * 1024
PRIME_HEAD_BYTES = 24 * 1024 * 1024
PRIME_SECONDS = 60
# The engine pauses a torrent nobody reads after 20 s and drops it after 2 minutes. A stream started ahead of Play (the
# TV does this once the viewer rests on a page) is kept busy with a one-byte read this often, for this long, so it is
# still warm, and still downloading ahead, when Play is pressed. Only the newest few are kept warm at once.
HOLD_EVERY_SECONDS = 10
HOLD_SECONDS = 5 * 60
HOLD_MAX = 2
END_PIECE_RETRY_SECONDS = (2, 4, 8)  # re-apply first/last-piece priority after qBittorrent applies a file selection
START_TIMEOUT = 90         # waiting for qBittorrent to fetch torrent metadata
SEARCH_TTL = 20 * 60        # a search answer is reused this long (a /prepare, then the /start that follows it)
SOURCE_FAILURE_TTL = 5 * 60  # avoid immediately selecting the same failed metadata source on retry
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
        if not re.fullmatch(r'[0-9a-f]{40}', info):
            continue
        if seeders is not None and (type(seeders) is not int or not 0 <= seeders < 10 ** 6):
            continue
        if size is not None and (type(size) is not int or not 0 < size < 10 ** 12):
            continue
        idx = item.get('fileIdx')
        if idx is not None and (type(idx) is not int or not 0 <= idx <= 65535):
            continue
        candidate = {'title': str(item.get('title', ''))[:300], 'infoHash': info, 'seeders': seeders, 'size': size}
        if seeders is None or size is None:
            candidate['_addon'] = True  # missing display metadata is not evidence of a dead torrent
        if idx is not None:
            candidate['fileIdx'] = idx
        sources = item.get('sources')
        if isinstance(sources, list):
            sources = [s for s in sources[:32] if isinstance(s, str) and len(s) <= 2048
                       and s.startswith(('tracker:http://', 'tracker:https://', 'tracker:udp://', 'dht:'))]
            if sources:
                candidate['sources'] = sources
        kept.append(candidate)
    return kept


def normalize(text):
    # An apostrophe joins rather than splits: "Child's" matches a release's "Childs".
    return re.sub(r'[^a-z0-9]+', ' ', re.sub(r"['’]", '', (text or '').lower())).strip()


def main_title(title):
    """A title without its subtitle ("Lemora" for "Lemora: A Child's Tale of the Supernatural"), or None."""
    short = re.split(r'\s*[:–—]\s+|\s+-\s+', title or '')[0].strip()
    return short if short and short != title and len(short) >= 4 else None


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
    """Every word of the film's title must appear in the release name, and a year, if given, must too. Releases often
    leave a subtitle out, so the main title alone is enough when the year is there to tell it from other films."""
    haystack = ' ' + normalize(release) + ' '

    def has(name):
        words = normalize(name).split()
        return bool(words) and all(' ' + w + ' ' in haystack for w in words)
    if not has(title) and not (year and main_title(title) and has(main_title(title))):
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


def search_texts(title, year, episode=None):
    """What the indexers are asked. An episode is also looked for in its season's pack, which is how older series are
    mostly shared (a search for "S01E01" alone found nothing for many of them). A long title is also asked by its main
    name ("Lemora" for "Lemora: A Child's Tale of the Supernatural"), as releases often leave the subtitle out."""
    names = [title] + ([main_title(title)] if main_title(title) else [])
    if episode is None:
        return [f'{n} {year}' if year else n for n in names]
    season, number = int(episode[0]), int(episode[1])
    return [f'{n} S{season:02d}E{number:02d}' for n in names] + [f'{n} S{season:02d}' for n in names]


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
    if not (seeders is None and result.get('_addon')) and (not isinstance(seeders, int) or seeders < MIN_SEEDERS):
        return False, 'seeders'
    pack = episode is not None and not episode_marker(*episode).search(name)
    low, high = ((MIN_BYTES, MAX_BYTES) if episode is None else
                 (MIN_EPISODE_BYTES, MAX_EPISODE_BYTES * PACK_EPISODES) if pack else
                 (MIN_EPISODE_BYTES, MAX_EPISODE_BYTES))
    if not (result.get('size') is None and result.get('_addon')) and (size < low or size > high):
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
    options = ranked(results, title, year, episode, runtime_minutes, any_audio)
    return options[0] if options else None


def ranked(results, title, year, episode=None, runtime_minutes=None, any_audio=False):
    """Acceptable results, best first: the well seeded ones by quality (as pick() chooses), then the rest by quality."""
    kept = [r for r in results if acceptable(r, title, year, episode)[0]]
    if not kept:
        return []
    best_seeds = max(int(r.get('seeders') or 0) for r in kept)
    seeded = [r for r in kept if int(r.get('seeders') or 0) >= max(MIN_SEEDERS, best_seeds * SEEDED_SHARE)]
    if not seeded:
        seeded = kept  # add-ons may omit seeder counts entirely
    score = lambda r: stream_score(r, runtime_minutes, episode, any_audio)
    rest = [r for r in kept if not any(r is s for s in seeded)]
    return sorted(seeded, key=score, reverse=True) + sorted(rest, key=score, reverse=True)


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
        text = stream.get('description') or stream.get('title') or ''
        hints = stream.get('behaviorHints') or {}
        name = hints.get('filename') or text.split('\n')[0].strip()
        seeds = re.search(r'(?:👤|👥|[Ss]eeders?\s*:)\s*([\d,]+)', text)
        size = re.search(r'💾\s*([\d.,]+)\s*(GiB|MiB|GB|MB)', text, re.I)
        if not name:
            continue
        unit = 1024 ** 3 if size and size.group(2).lower().startswith('g') else 1024 ** 2
        results.extend(clean_candidates([{
            'title': name, 'infoHash': info,
            'seeders': stream.get('seeders', int(seeds.group(1).replace(',', '')) if seeds else None),
            'size': hints.get('videoSize') or (int(float(size.group(1).replace(',', '.')) * unit) if size else None),
            'fileIdx': stream.get('fileIdx'), 'sources': stream.get('sources'),
        }]))
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
        trackers = [s[len('tracker:'):] for s in result.get('sources', []) if s.startswith('tracker:')]
        return ('magnet:?xt=urn:btih:' + info + '&dn=' + urllib.parse.quote(title or '')
                + ''.join('&tr=' + urllib.parse.quote(tracker, safe='') for tracker in dict.fromkeys(trackers or TRACKERS)))
    return None


def video_file(files, episode=None, file_idx=None):
    """The largest video file in a torrent, as (index, name, size), or None.

    With episode = (season, episode), only a video file carrying that episode's SxxExx tag counts.
    """
    marker = episode_marker(*episode) if episode is not None else None
    if file_idx is not None:
        # The add-on selected this file for this exact video ID. A missing index must never become a different file.
        selected = next((f for f in files if f.get('index') == file_idx), None)
        if not selected or not selected.get('name', '').lower().endswith(VIDEO_EXTENSIONS):
            return None
        name = selected['name']
        if marker and re.search(r's\d+e\d+', name, re.I) and not marker.search(name):
            return None
        return file_idx, name, selected.get('size', 0)
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
                 category=CATEGORY, addons=ADDONS, cinemeta=CINEMETA, engine=None):
        # Stremio's streaming server, when set up: streams play through it; qBittorrent is then only the fallback.
        self.engine = engine
        self.race_patience, self.race_deadline = RACE_PATIENCE_SECONDS, RACE_DEADLINE_SECONDS
        self.prime_tail, self.prime_head = PRIME_TAIL_BYTES, PRIME_HEAD_BYTES
        self.race_tail, self.race_tail_patience = RACE_TAIL_BYTES, RACE_TAIL_PATIENCE_SECONDS
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
        self.end_piece_retries = END_PIECE_RETRY_SECONDS
        self.pending = {}   # hash -> {"label", "error"} for films whose metadata has not arrived yet
        self.searches = SearchCache(lambda title, year, episode: self.search(title, year, episode))

    # --- Prowlarr ---------------------------------------------------------------------------------

    def search(self, title, year, episode=None):
        key = self.prowlarr_api_key
        if not key:
            raise ServiceError('Prowlarr API key is not configured')
        texts, category = search_texts(title, year, episode), (MOVIE_CATEGORY if episode is None else TV_CATEGORY)
        headers = {'X-Api-Key': key, 'Accept': 'application/json'}
        try:
            with urllib.request.urlopen(urllib.request.Request(f'{self.prowlarr}/api/v1/indexer', headers=headers), timeout=10) as response:
                indexers = [i['id'] for i in json.loads(response.read() or b'[]') if i.get('enable')]
        except (urllib.error.URLError, ValueError, OSError, KeyError, TypeError):
            LOG.info('Prowlarr indexer list unavailable; asking the add-ons only')
            indexers = []

        # Each indexer is asked on its own and all at once. One that is slow or failing is skipped, so it cannot hold up
        # the answers from the rest (Prowlarr otherwise waits for every indexer before it replies).
        def ask(indexer, text):
            query = urllib.parse.urlencode({'query': text, 'type': 'search', 'categories': category, 'limit': 100, 'indexerIds': indexer})
            req = urllib.request.Request(f'{self.prowlarr}/api/v1/search?{query}', headers=headers)
            with urllib.request.urlopen(req, timeout=INDEXER_SECONDS) as response:
                return json.loads(response.read() or b'[]')

        def ask_addon(base):
            return addon_results(base, title, year, episode, self.cinemeta)

        workers = len(indexers) * len(texts) + len(self.addons)
        if not workers:
            raise ServiceError('indexers unavailable')
        pool = ThreadPoolExecutor(max_workers=workers)
        asked = {pool.submit(ask, i, text): f'indexer {i} ({text})' for i in indexers for text in texts}
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
            selected = [f for f in files if f.get('priority', 1) > 0]
            chosen = video_file(selected)
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
            'hash': hash_, 'path': str(path), 'size': size, 'label': label, 'file_index': index,
            'advertised_seeders': (self.pending.get(hash_) or {}).get('advertised_seeders'),
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
        try:
            results = list(self.searches.get(title, year, episode))
        except ServiceError:
            if not candidates:
                raise
            results = []  # the owner's add-ons remain usable when the backend's indexers are down
        results += list(candidates)
        with self.lock:
            failed = {h for h, pending in self.pending.items() if pending['error'] and
                      time.monotonic() - pending.get('failed_at', time.monotonic()) < SOURCE_FAILURE_TTL}
        results = [r for r in results if hash_from_magnet(magnet_for(r, title)) not in failed]
        if self.engine is not None:
            return self.start_engine(results, title, year, episode, runtime_minutes, any_audio)
        choice = pick(results, title, year, episode, runtime_minutes, any_audio)
        if choice is None:
            raise LookupError('no source')
        link = magnet_for(choice, title)
        if link is None:
            raise LookupError('no link')
        hash_ = hash_from_magnet(link)
        label = re.sub(r'[\._]+', ' ', choice.get('title') or title)[:120]
        with self.lock:
            entry = self.known.get(hash_)
            other_file = entry is not None and (
                (choice.get('fileIdx') is not None and choice['fileIdx'] != entry.get('file_index')) or
                (episode is not None and choice.get('fileIdx') is None and not episode_marker(*episode).search(entry['path'])))
            if entry is not None and not other_file:
                entry['seen'] = time.time()
                return hash_, entry['label'], 'ready'
        if other_file:
            # Another episode of a season pack that is already here: point the torrent at that file.
            entry = self.switch_file(hash_, episode, choice.get('fileIdx'))
            return hash_, entry['label'], 'ready'
        with self.lock:
            if hash_ in self.pending and not self.pending[hash_]['error']:
                pending = self.pending[hash_]
                if pending.get('episode') != episode or pending.get('fileIdx') != choice.get('fileIdx'):
                    raise ServiceError('Another file in this torrent is already connecting')
                return hash_, self.pending[hash_]['label'], 'connecting'
            self.pending[hash_] = {'label': label, 'error': None, 'episode': episode, 'fileIdx': choice.get('fileIdx'),
                                   'advertised_seeders': choice.get('seeders')}
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

    def start_engine(self, results, title, year, episode, runtime_minutes, any_audio):
        """Plays through Stremio's streaming engine. Returns (hash, label, 'ready') once the film's first bytes are here.

        The best few sources are opened at once and each is asked for the start of its file, which the player needs
        first anyway. The best-ranked one plays if it delivers within RACE_PATIENCE_SECONDS; otherwise the best-ranked
        of those that already have. A source that advertises seeders but sends nothing never wins.
        """
        tried = []
        for result in ranked(results, title, year, episode, runtime_minutes, any_audio):
            link = magnet_for(result, title)
            hash_ = hash_from_magnet(link) if link else None
            if hash_ and all(hash_ != h for _, h in tried):
                tried.append((result, hash_))
            if len(tried) == RACE_CANDIDATES:
                break
        if not tried:
            raise LookupError('no source')
        with self.lock:
            reuse = None
            for result, hash_ in tried:   # already playing here: no need to race
                entry = self.known.get(hash_)
                if entry and entry.get('engine') and self.same_file(entry, episode, result.get('fileIdx')):
                    reuse = hash_, entry
                    break
        if reuse is not None:
            hash_, entry = reuse
            # Only while the engine still has it: it drops a torrent nobody has read for two minutes, and saying "ready"
            # for one it dropped leaves the player waiting while it is fetched again from scratch.
            if self.engine_has(hash_, entry['file_index']):
                entry['seen'] = time.time()
                entry['hold_until'] = time.time() + HOLD_SECONDS
                return hash_, entry['label'], 'ready'
            LOG.info('engine had dropped %s; starting it again', hash_[:8])
            with self.lock:
                self.known.pop(hash_, None)

        began = time.time()
        deadline = began + self.race_deadline
        arrived, started, finished = {}, {}, set()   # arrived: start (and end, when needed); started: the start alone
        signal = threading.Condition()

        def attempt(i, result, hash_):
            try:
                files = self.engine.open(hash_, sources_for(result, hash_, TRACKERS), timeout=max(1.0, deadline - time.time()))
                chosen = video_file(files, episode, result.get('fileIdx'))
                if chosen is not None:
                    index, name, size = chosen
                    seconds = self.engine.head_time(hash_, index, max(1, min(RACE_HEAD_BYTES, size)), deadline)
                    if seconds is not None:
                        with signal:
                            started[i] = (seconds, chosen)
                            signal.notify_all()
                        if self.race_tail and name.lower().endswith('.mkv') and size > 2 * self.race_tail:
                            if not self.engine.fetch(hash_, index, size - self.race_tail, size - 1, max(1.0, deadline - time.time())):
                                return
                            seconds = time.time() - began
                        with signal:
                            arrived[i] = (seconds, chosen)
            except EngineError as error:
                LOG.info('engine source %d did not open: %s', i + 1, error)
            finally:
                with signal:
                    finished.add(i)
                    signal.notify_all()

        def settled():
            now = time.time()
            if 0 in arrived or len(finished) == len(tried) or now >= deadline:
                return True
            if arrived and now - began >= self.race_patience:
                return True
            # Starts have come but no end yet: do not hold Play for an end that may never come.
            first_start = min((s for s, _ in started.values()), default=None)
            return first_start is not None and now - began >= max(self.race_patience, first_start + self.race_tail_patience)

        for i, (result, hash_) in enumerate(tried):
            threading.Thread(target=attempt, args=(i, result, hash_), daemon=True).start()
        with signal:
            while not settled():
                signal.wait(0.1)
            winner = min(arrived) if arrived else (min(started) if started else None)
            if winner is not None and winner not in arrived:
                arrived[winner] = started[winner]
                LOG.info('no source sent its end in time; playing source %d without it', winner + 1)
        for i, (_, hash_) in enumerate(tried):
            if i != winner and hash_ not in self.known:
                self.engine.remove(hash_)
        if winner is None:
            raise LookupError('no source')   # none sent anything in time
        result, hash_ = tried[winner]
        seconds, (index, name, size) = arrived[winner]
        entry = {'hash': hash_, 'engine': True, 'file_index': index, 'path': name, 'size': size,
                 'label': re.sub(r'[\._]+', ' ', Path(name).stem)[:120], 'seen': time.time(),
                 'advertised_seeders': result.get('seeders'), 'transcode': audio_unplayable(name),
                 'hold_until': time.time() + HOLD_SECONDS}
        with self.lock:
            self.known[hash_] = entry
        threading.Thread(target=self.prime, args=(hash_,), daemon=True).start()
        LOG.info('engine: source %d of %d plays (first bytes in %.1fs, chosen after %.1fs)', winner + 1, len(tried),
                 seconds, time.time() - began)
        return hash_, entry['label'], 'ready'

    def engine_has(self, hash_, file_idx):
        """Whether the streaming engine still holds this torrent (its stats are empty once it has dropped it)."""
        try:
            return bool(self.engine.stats(hash_, file_idx))
        except EngineError:
            return False

    def prime(self, hash_):
        """Fetches what the player reads before its first frame, so Play finds it here: the file's end, then its opening."""
        entry = self.known.get(hash_)
        if not entry or not entry.get('engine'):
            return
        size, index, began = entry['size'], entry['file_index'], time.time()
        if self.prime_tail and size > 2 * self.prime_tail:
            self.engine.fetch(hash_, index, size - self.prime_tail, size - 1, PRIME_SECONDS)
        if self.prime_head:
            self.engine.fetch(hash_, index, 0, min(self.prime_head, size) - 1, PRIME_SECONDS)
        LOG.info('primed %s (end and opening) in %.1fs', hash_[:8], time.time() - began)

    def hold_tick(self):
        """Keeps the newest streams started ahead of Play warm: a one-byte read stops the engine pausing or dropping them."""
        now = time.time()
        with self.lock:
            warm = sorted(((e['hold_until'], h, e['file_index']) for h, e in self.known.items()
                           if e.get('engine') and e.get('hold_until', 0) > now), reverse=True)[:HOLD_MAX]
        for _, hash_, index in warm:
            self.engine.fetch(hash_, index, 0, 0, 5)

    @staticmethod
    def same_file(entry, episode, file_idx):
        if file_idx is not None:
            return file_idx == entry.get('file_index')
        return episode is None or bool(episode_marker(*episode).search(entry.get('path') or ''))

    def refresh_end_pieces(self, hash_, row):
        """Has qBittorrent fetch the selected file's first and last pieces first.

        qBittorrent applies first/last-piece priority to the files selected when that setting was switched on, not to a
        file selected later in a pack. A player reads the end of a Matroska file (its seek index) before it shows a
        picture, so it would wait for the whole sequential download. Switching the setting off and on re-applies it.
        qBittorrent applies a new file selection in the background, so it is done again a few seconds later as well.
        """
        def toggle(was_on):
            try:
                self.qbit('/api/v2/torrents/toggleFirstLastPiecePrio', {'hashes': hash_})
                if was_on:
                    self.qbit('/api/v2/torrents/toggleFirstLastPiecePrio', {'hashes': hash_})
            except ServiceError as error:
                LOG.warning('could not prioritise the file ends: %s', error)

        toggle((row or {}).get('f_l_piece_prio', True))

        def later():
            for delay in self.end_piece_retries:
                time.sleep(delay)
                toggle(True)   # on after the first toggle, so off and on again
        if self.end_piece_retries:
            threading.Thread(target=later, daemon=True).start()

    def switch_file(self, hash_, episode, file_idx):
        """Serves another file of a torrent already here (the next episode in a pack), downloading only that file now."""
        row = self.torrent(hash_)
        files = self.qbit('/api/v2/torrents/files?hash=' + hash_) or []
        if not row or not files:
            raise ServiceError('this source is no longer available')
        chosen = video_file(files, episode, file_idx)
        if chosen is None:
            raise LookupError('no video file')
        index = chosen[0]
        others = '|'.join(str(f['index']) for f in files if f['index'] != index)
        if others:
            self.qbit('/api/v2/torrents/filePrio', {'hash': hash_, 'id': others, 'priority': 0})
        self.qbit('/api/v2/torrents/filePrio', {'hash': hash_, 'id': str(index), 'priority': 1})
        self.refresh_end_pieces(hash_, row)
        entry = self.entry_for(hash_, row['save_path'], files, chosen)
        with self.lock:
            entry['advertised_seeders'] = (self.known.get(hash_) or {}).get('advertised_seeders')
            self.known[hash_] = entry
        LOG.info('switched a pack to another of its files')
        return entry

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
                    self.pending[hash_]['failed_at'] = time.monotonic()
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
                    'advertisedSeeds': pending.get('advertised_seeders'),
                    'seeds': row.get('num_seeds', 0), 'peers': row.get('num_leechs', 0),
                    'speed': row.get('dlspeed', 0)}
        if entry.get('engine'):
            try:
                # Empty (null) once the engine has dropped the torrent: report nothing rather than fail the request.
                stats = self.engine.stats(hash_, entry['file_index']) or {}
            except EngineError:
                stats = {}
            progress = min(1.0, float(stats.get('streamProgress') or 0))
            # The engine fetches whatever part is read next, so the player may seek anywhere (seekAnywhere).
            return {'phase': 'ready', 'label': entry['label'], 'advertisedSeeds': entry.get('advertised_seeders'),
                    'seeds': stats.get('unchoked', 0), 'peers': stats.get('peers', 0),
                    'progress': round(progress, 4), 'torrentProgress': round(progress, 4),
                    'bufferedBytes': int(progress * entry['size']), 'fileIdx': entry['file_index'],
                    'speed': int(stats.get('downloadSpeed') or 0), 'size': entry['size'], 'seekAnywhere': True,
                    'engine': 'stremio'}
        row = self.torrent(hash_) or {}
        contiguous = max(0, self.downloaded_until(entry, 0))
        ready = contiguous >= min(READY_BYTES, entry['size'])
        # speed (bytes/s) and size (bytes of the film file) feed the player's loading screen and Stream card.
        return {'phase': 'ready' if ready else 'buffering', 'label': entry['label'],
                'advertisedSeeds': entry.get('advertised_seeders'),
                'seeds': row.get('num_seeds', 0), 'peers': row.get('num_leechs', 0),
                'progress': round(contiguous / entry['size'], 4) if entry['size'] else 0,
                'torrentProgress': round(row.get('progress', 0.0), 4), 'bufferedBytes': contiguous,
                'fileIdx': entry.get('file_index'), 'speed': row.get('dlspeed', 0), 'size': entry['size']}

    def await_film(self, hash_):
        """Waits for metadata, then selects only the film file, so the download follows its pieces in order."""
        deadline = time.time() + START_TIMEOUT
        with self.lock:
            episode = (self.pending.get(hash_) or {}).get('episode')
            file_idx = (self.pending.get(hash_) or {}).get('fileIdx')
        while time.time() < deadline:
            rows = self.qbit('/api/v2/torrents/info?category=' + self.category) or []
            row = next((r for r in rows if hash_ and r.get('hash') == hash_), None)
            if row is None and not hash_ and rows:
                row = max(rows, key=lambda r: r.get('added_on', 0))
            if row and not row.get('force_start'):
                # Metadata fetching can itself be queued behind ordinary downloads.
                self.qbit('/api/v2/torrents/setForceStart', {'hashes': hash_, 'value': 'true'})
            if row and row.get('state') not in ('metaDL', 'forcedMetaDL', 'checkingResumeData'):
                hash_ = row['hash']
                # Start it now: qBittorrent's active-download limit would otherwise queue the film behind the library's downloads.
                files = self.qbit('/api/v2/torrents/files?hash=' + hash_) or []
                if not files:
                    # Right after the add, qBittorrent can report a state before it has the file list. Wait for it.
                    time.sleep(2)
                    continue
                chosen = video_file(files, episode, file_idx)
                if chosen is None:
                    raise LookupError('no video file')
                index, name, size = chosen
                others = '|'.join(str(f['index']) for f in files if f['index'] != index)
                if others:
                    self.qbit('/api/v2/torrents/filePrio', {'hash': hash_, 'id': others, 'priority': 0})
                self.qbit('/api/v2/torrents/filePrio', {'hash': hash_, 'id': str(index), 'priority': 1})
                if others:
                    self.refresh_end_pieces(hash_, row)
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
                if self.known[hash_].get('engine'):
                    self.engine.remove(hash_)   # the engine keeps its own size-capped cache
                    self.known.pop(hash_, None)
                    continue
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
        if entry.get('engine'):
            self.serve_engine(entry)
            return
        total = entry['size']
        window = parse_range(self.headers.get('Range'), total)
        if window is None:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{total}')
            self.end_headers()
            return
        start, end = window
        LOG.info('read %s: bytes %d-%d of %d (from %.1f%%)', hash_[:8], start, end, total, 100.0 * start / max(total, 1))
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

    def serve_engine(self, entry):
        """Passes a read through to the streaming engine, which fetches those exact pieces first."""
        url = self.server.streams.engine.url(entry['hash'], entry['file_index'])
        asked = self.headers.get('Range')
        LOG.info('read %s via engine: %s', entry['hash'][:8], asked or 'whole file')
        request = urllib.request.Request(url, headers={'Range': asked} if asked else {})
        try:
            upstream = urllib.request.urlopen(request, timeout=STALL_SECONDS)
        except urllib.error.HTTPError as error:
            self.send_response(error.code)
            for name in ('Content-Range', 'Content-Length'):
                if error.headers.get(name):
                    self.send_header(name, error.headers[name])
            self.end_headers()
            return
        except (urllib.error.URLError, OSError):
            self.reply(502, {'error': 'The streaming engine is not answering'})
            return
        with upstream:
            self.send_response(upstream.status)
            self.send_header('Content-Type', 'video/x-matroska' if entry['path'].lower().endswith('.mkv')
                             else upstream.headers.get('Content-Type', 'video/mp4'))
            for name in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
                if upstream.headers.get(name):
                    self.send_header(name, upstream.headers[name])
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read1(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    entry['seen'] = time.time()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass  # The viewer left or seeked; the player opens a new read.

    def serve_transcoded(self, entry):
        """Serves the film as Matroska with AAC audio, for a TV that cannot decode the original soundtrack.

        ffmpeg reads from a pipe that this method fills with finished pieces only, and writes to the response.
        The result cannot be seeked, so it is served whole; the player can still play straight through.
        """
        engine = self.server.streams.engine if entry.get('engine') else None
        source = engine.url(entry['hash'], entry['file_index']) if engine else 'pipe:0'
        command = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', source,
                   '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ac', '2',
                   '-f', 'matroska', 'pipe:1']
        try:
            ffmpeg = subprocess.Popen(command, stdin=subprocess.DEVNULL if engine else subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.reply(503, {'error': 'Audio conversion is unavailable'})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'video/x-matroska')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        if not engine:   # ffmpeg reads the engine's address itself; a qBittorrent file is fed piece by piece
            threading.Thread(target=self.feed, args=(ffmpeg, entry), daemon=True).start()
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
        logged_wait = False
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
                    waited = time.time() - last_growth
                    if waited > STALL_SECONDS:
                        LOG.info('read %s: gave up waiting at byte %d after %ds', entry['hash'][:8], position, int(waited))
                        return
                    if not logged_wait and waited > 3:
                        logged_wait = True
                        piece = (entry['offset'] + position) // entry['piece_size']
                        LOG.info('read %s: waiting for piece %d (byte %d, %.1f%% of the file)', entry['hash'][:8], piece,
                                 position, 100.0 * position / max(entry['size'], 1))
                    time.sleep(QUICK_POLL_SECONDS)
                    continue
                if logged_wait:
                    LOG.info('read %s: piece arrived after %.1fs', entry['hash'][:8], time.time() - last_growth)
                    logged_wait = False
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
                          addons=ADDONS if config.addons is None else config.addons,
                          engine=StremioEngine(config.stremio_server) if config.stremio_server else None)
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

    def holding():
        while True:
            time.sleep(HOLD_EVERY_SECONDS)
            try:
                streams.hold_tick()
            except Exception:   # noqa: BLE001 - keeping streams warm must never stop the service
                LOG.warning('keeping streams warm failed', exc_info=True)

    if streams.engine is not None:
        threading.Thread(target=holding, daemon=True).start()
    server = LimitedServer((config.listen_host, config.port), Handler)
    server.streams = streams
    if config.seerr_api_key and config.voice_token:
        server.voice, server.voice_token = Voice(config.seerr_url, config.seerr_api_key), config.voice_token
        LOG.info('voice requests are on')
    server.serve_forever()


if __name__ == '__main__':
    main()
