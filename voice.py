"""Turns a spoken "download X" into a Seerr request, for a Google Home routine, Home Assistant or IFTTT to call.

The caller sends the words it heard; this module cleans them, asks Seerr's search for the best film or series, and files
the request exactly as the Cinema app would. Seerr then hands it to Radarr or Sonarr and the torrent client.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERB = re.compile(r'^(?:(?:hey google\W+)?(?:please\s+)?(?:can you\s+|could you\s+)?)(?:download|request|get|add|queue|grab|fetch)(?:\s+(?:me\s+)?|$)', re.I)
KIND_WORD = re.compile(r'^(?:the\s+)?(movie|film|show|series|tv show|tv series)\s+(?:called\s+|named\s+)?', re.I)
TRAILER = re.compile(r'\s+(?:please|for me|to cinema|on cinema)$', re.I)
YEAR = re.compile(r'\s*[\(\[]?\b((?:19|20)\d\d)\b[\)\]]?\s*$')
SHOW_WORDS = {'show', 'series', 'tv show', 'tv series'}


def parse_query(text):
    """(title, 'movie' | 'tv' | None, year or None) from what was said, or (None, None, None) when nothing is left."""
    words = ' '.join(str(text).split())[:200]
    words = VERB.sub('', words)
    kind = None
    found = KIND_WORD.match(words)
    if found:
        kind = 'tv' if found.group(1).lower() in SHOW_WORDS else 'movie'
        words = words[found.end():]
    words = TRAILER.sub('', words).strip(' .,!?')
    year = None
    found = YEAR.search(words)
    if found and found.start() > 0:
        year = int(found.group(1))
        words = words[:found.start()].strip(' .,!?(')
    return (words or None), kind, year


def release_year(result):
    date = result.get('releaseDate') or result.get('firstAirDate') or ''
    return int(date[:4]) if date[:4].isdigit() else None


def choose(results, kind=None, year=None):
    """Seerr already ranks by relevance; take the first film or series, preferring the stated kind and year."""
    candidates = [r for r in results if r.get('mediaType') in ('movie', 'tv') and isinstance(r.get('id'), int)]
    for matches in (
        lambda r: (kind is None or r['mediaType'] == kind) and (year is None or release_year(r) == year),
        lambda r: kind is None or r['mediaType'] == kind,
        lambda r: True,
    ):
        for result in candidates:
            if matches(result):
                return result
    return None


def display(result):
    name = result.get('title') or result.get('name') or 'that title'
    year = release_year(result)
    return f'{name} ({year})' if year else name


class Voice:
    """Needs a Seerr API key: a voice routine has no signed-in user. Every answer carries a short sentence to speak back."""

    def __init__(self, seerr_url, api_key, opener=urllib.request.urlopen, per_minute=12, clock=time.time):
        self.seerr, self.api_key, self.opener = seerr_url.rstrip('/'), api_key, opener
        self.per_minute, self.clock, self.recent, self.lock = per_minute, clock, [], threading.Lock()

    def allow(self):
        now = self.clock()
        with self.lock:
            self.recent = [t for t in self.recent if now - t < 60]
            if len(self.recent) >= self.per_minute:
                return False
            self.recent.append(now)
            return True

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.seerr + path, data=data, method=method, headers={
            'X-Api-Key': self.api_key, 'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            return error.code, {}
        except (urllib.error.URLError, ValueError, OSError):
            return None, {}

    def request(self, text):
        """{'status': requested | already | available | not_found | unclear | unavailable, 'title': ..., 'say': ...}"""
        title, kind, year = parse_query(text)
        if not title:
            return {'status': 'unclear', 'say': "I didn't catch what to download."}
        code, found = self.call('GET', '/api/v1/search?' + urllib.parse.urlencode({'query': title, 'page': 1}))
        if code is None or code >= 500 or code in (401, 403):
            return {'status': 'unavailable', 'say': "I couldn't reach Seerr."}
        result = choose(found.get('results', []) if isinstance(found, dict) else [], kind, year)
        if result is None:
            return {'status': 'not_found', 'say': f"I couldn't find {title}."}
        name = display(result)
        if (result.get('mediaInfo') or {}).get('status') == 5:
            return {'status': 'available', 'title': name, 'say': f'{name} is already in your library.'}
        body = {'mediaType': result['mediaType'], 'mediaId': result['id']}
        if result['mediaType'] == 'tv':
            body['seasons'] = 'all'
        code, _ = self.call('POST', '/api/v1/request', body)
        if code in (200, 201, 202):
            return {'status': 'requested', 'title': name, 'say': f'Requested {name}.'}
        if code == 409:
            return {'status': 'already', 'title': name, 'say': f'{name} has already been requested.'}
        return {'status': 'unavailable', 'title': name, 'say': f"Seerr wouldn't take the request for {name}."}
