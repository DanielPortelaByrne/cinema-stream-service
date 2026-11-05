"""Explicit, portable configuration. Only the service reads upstream credentials."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import urllib.parse


def secret(env, name):
    value, file = env.get(name, ''), env.get(name + '_FILE', '')
    if value and file:
        raise ValueError(f'Set only {name} or {name}_FILE')
    if file:
        try:
            return Path(file).read_text(encoding='utf-8').strip()
        except OSError:
            raise ValueError(f'Cannot read {name}_FILE') from None
    return value


def endpoint(env, name, default):
    value = env.get(name, default).strip().rstrip('/')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f'{name} must be an HTTP(S) base URL without credentials or a query')
    try:
        parsed.port
    except ValueError:
        raise ValueError(f'{name} has an invalid port') from None
    return value


@dataclass(frozen=True)
class Settings:
    prowlarr_url: str
    prowlarr_api_key: str = field(repr=False)
    qbit_url: str
    qbit_username: str = field(repr=False)
    qbit_password: str = field(repr=False)
    seerr_url: str
    media_root: Path
    qbit_media_root: str
    state_root: Path
    listen_host: str
    port: int
    # Both are optional. The voice endpoint is switched on only when both are set.
    seerr_api_key: str = field(default='', repr=False)
    voice_token: str = field(default='', repr=False)
    # The qBittorrent category this service owns: it only ever adopts and deletes torrents in it.
    category: str = 'cinema-stream'
    # Stremio add-on base URLs asked alongside Prowlarr. None means the built-in public ones; () turns them off.
    addons: tuple = None
    # Stremio's streaming server (for example the stremio/server container). Empty: streams use qBittorrent.
    stremio_server: str = ''

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        key = secret(env, 'PROWLARR_API_KEY')
        username = secret(env, 'QBITTORRENT_USERNAME')
        password = secret(env, 'QBITTORRENT_PASSWORD')
        for name, value in (('PROWLARR_API_KEY', key), ('QBITTORRENT_USERNAME', username), ('QBITTORRENT_PASSWORD', password)):
            if not value:
                raise ValueError(f'{name} (or {name}_FILE) is required')
        voice_token = secret(env, 'VOICE_TOKEN')
        if voice_token and len(voice_token) < 24:
            raise ValueError('VOICE_TOKEN must be at least 24 characters (it is the only thing guarding the voice endpoint)')
        category = env.get('STREAM_CATEGORY', 'cinema-stream').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', category):
            raise ValueError('STREAM_CATEGORY must be 1 to 40 letters, digits, - or _')
        addons = None
        if 'STREAM_ADDONS' in env:
            listed = [part.strip() for part in env['STREAM_ADDONS'].split(',') if part.strip()]
            addons = () if listed == ['none'] or not listed else tuple(endpoint({'A': part}, 'A', '') for part in listed)
        media = env.get('QBITTORRENT_MEDIA_ROOT', '/media').rstrip('/')
        if not media.startswith('/') or media == '' or '\\' in media or '..' in media.split('/'):
            raise ValueError('QBITTORRENT_MEDIA_ROOT must be an absolute POSIX directory')
        try:
            port = int(env.get('STREAM_PORT', '5057'))
            if not 1 <= port <= 65535:
                raise ValueError()
        except ValueError:
            raise ValueError('STREAM_PORT must be between 1 and 65535') from None
        return cls(
            endpoint(env, 'PROWLARR_URL', 'http://127.0.0.1:9696'), key,
            endpoint(env, 'QBITTORRENT_URL', 'http://127.0.0.1:8080'), username, password,
            endpoint(env, 'SEERR_URL', 'http://127.0.0.1:5055'),
            Path(env.get('MEDIA_ROOT', './media')).resolve(), media,
            Path(env.get('STATE_ROOT', './state')).resolve(), env.get('LISTEN_HOST', '127.0.0.1'), port,
            secret(env, 'SEERR_API_KEY'), voice_token, category, addons,
            endpoint(env, 'STREMIO_SERVER', '') if env.get('STREMIO_SERVER', '').strip() else '',
        )
