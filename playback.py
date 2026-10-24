"""Short-lived media capabilities usable by players without forwarding Seerr cookies."""
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import time


class PlaybackTickets:
    def __init__(self, key=None, lifetime=6 * 3600, clock=time.time):
        self.key = key if key is not None else secrets.token_bytes(32)
        self.lifetime = lifetime
        self.clock = clock

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('xb') as handle:
                path.chmod(0o600)
                handle.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError('Playback key file must contain 32 bytes')
        return cls(key)

    def issue(self, hash_):
        expires = int(self.clock()) + self.lifetime
        body = f'{hash_}:{expires}'.encode()
        signature = hmac.new(self.key, body, hashlib.sha256).hexdigest()
        return f'{expires}.{signature}'

    def valid(self, hash_, ticket):
        match = re.fullmatch(r'(\d{1,12})\.([a-f0-9]{64})', ticket)
        if not match:
            return False
        expires = int(match[1])
        if expires <= self.clock():
            return False
        expected = hmac.new(self.key, f'{hash_}:{expires}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, match[2])
