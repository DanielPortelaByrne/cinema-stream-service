# Cinema Stream Service

One self-hosted streaming backend for [Cinema's Android TV APK](https://github.com/DanielPortelaByrne/cinema-tv)
and [Cinema Flatpak for Steam Deck/Linux](https://github.com/DanielPortelaByrne/cinema-flatpak).

The apps ask this service for a film or episode. It searches your Prowlarr indexers, selects a seeded release,
starts a sequential qBittorrent download, and serves the completed pieces to the player over HTTP. The APK uses
ExoPlayer and the Flatpak uses mpv. Both use the same API and can connect to the same service instance.

## What you need

- Your own working **Prowlarr**, with indexers configured and its API key.
- **qBittorrent 4.6 or 5.x**, with its Web UI enabled and username/password.
- **Seerr**, with accounts for the people who use Cinema. Jellyseerr/Overseerr-compatible sessions use the same API.
- A shared folder that both qBittorrent and this service can read and write.
- Docker Engine with Docker Compose, or Python 3.11+ for a native service.

This repo supplies the streaming service. It does not install or configure the other services. Jellyfin and
Sonarr/Radarr are not required by this backend itself; Cinema uses those separately for its library and requests.

## Clone and start

```bash
git clone https://github.com/DanielPortelaByrne/cinema-stream-service.git
cd cinema-stream-service
cp .env.example .env
mkdir -p media state
```

Edit `.env`:

1. Set your Prowlarr, qBittorrent and Seerr addresses and credentials. These addresses must be reachable **from
   inside the backend container**. The example uses services whose ports are published on the Docker host;
   container names also work when the containers share a Docker network.
2. Set `MEDIA_ROOT` to the existing media folder qBittorrent uses, and `QBITTORRENT_MEDIA_ROOT` to the path
   qBittorrent sees for that exact folder. See the mapping example below.
3. Set `PUID` and `PGID` to the user/group that owns the shared files (`id -u` and `id -g` on Linux).
   The service and qBittorrent must have permission to read/write the streaming cache. Ensure `state/` is writable too.
4. For a TV or Deck on the LAN, change `STREAM_BIND_ADDRESS` to your server's LAN IP, or `0.0.0.0` to listen on
   all host interfaces. The example starts with loopback access only.

Then run:

```bash
docker compose build
docker compose run --rm stream-service --check-config
docker compose up -d
curl http://127.0.0.1:5057/health
```

If you bound a specific LAN IP, use that address for the health check instead. A healthy running service returns
`{"ok": true}`. This checks the HTTP service; upstream credentials and indexer availability are exercised by a stream request.
Logs: `docker compose logs -f stream-service`. Update: `git pull && docker compose up -d --build`.

Keep this service on a trusted LAN or private network. For access across the public internet, use HTTPS and
network access controls. Playback URLs are temporary bearer links; anyone with a valid link can play that stream
until it expires. Do not put your `.env`, state folder, or playback URLs in an issue or public log.

### Shared folder mapping

For example, suppose the host stores media at `/srv/media`, and qBittorrent's container mounts it at `/downloads`:

| Setting or mount | Value |
| --- | --- |
| `.env`: `MEDIA_ROOT` | `/srv/media` |
| qBittorrent mount | `/srv/media:/downloads` |
| `.env`: `QBITTORRENT_MEDIA_ROOT` | `/downloads` |
| Stream-service mount (Compose supplies this) | `/srv/media:/media` |

The service asks qBittorrent to download into `/downloads/Downloads/stream`, then reads those same files at
`/media/Downloads/stream` inside its own container. Matching paths by name is insufficient: both mounts must
refer to the same physical files. A qBittorrent instance on another machine needs a genuinely shared filesystem.

Only torrents in the `cinema-stream` category and its streaming cache are eligible for automatic cleanup.
Idle streams are removed after four hours, including their files. Existing downloads in another category are
not adopted or deleted. The normal Jellyfin library and the app's Queue download feature remain separate.

## Connect either app

In **both** the Android APK and Flatpak:

1. Configure and sign into the same Seerr instance set in this service's `SEERR_URL`.
2. Open **Settings → Helper services** and set **Stream service address** to `http://YOUR-SERVER:5057`.
   This must be an address reachable by the TV or Deck. `localhost` would point at the playback device itself.
3. Open a film that is not in your library and choose **Stream now**, or play a released episode you do not have yet.

No backend API keys or qBittorrent passwords are entered into the players for streaming. Cinema authenticates
the start request with its existing Seerr session and plays the returned media URL. The request-helper field
is for a different optional service; leave it blank unless you run a request helper too.

You can point both apps at one backend, or give each household its own cloned/configured instance. The backend
does not require any particular person's Jellyfin server, media-stack folders, or Sonarr database.

## Native Python service

The Python server uses only the standard library. FFmpeg is optional for older clients needing AAC audio conversion;
the current APK and Flatpak request the original soundtrack and decode it themselves. The Docker image includes FFmpeg.

Export the variables from `.env.example` into the process environment, using addresses reachable by the host
and `MEDIA_ROOT` set to the host's shared media folder. A `.env` file is loaded automatically by Compose;
**native Python does not load it**. Then run:

```bash
python stream_service.py --check-config
python stream_service.py
```

Native mode listens on `127.0.0.1:5057` unless `LISTEN_HOST` and `STREAM_PORT` are set. Set `LISTEN_HOST=0.0.0.0`
for LAN clients. `STATE_ROOT` defaults to `./state`. `PROWLARR_API_KEY`, `QBITTORRENT_USERNAME`, and
`QBITTORRENT_PASSWORD` each support a corresponding `_FILE` variable for mounted secret files instead.

## API and compatibility

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Service liveness, no authentication |
| `POST /prepare` | Warm the indexer search; valid Seerr `connect.sid` cookie required |
| `POST /start` | Start or reuse a stream; valid Seerr cookie required |
| `GET /stream/<hash>/<ticket>` | Media bytes; supports single HTTP byte ranges |
| `GET /status/<hash>/<ticket>` | Loading phase, download speed, peers and size |

`/prepare` and `/start` accept JSON such as:

```json
{"title":"Example Film","year":"2026","runtime":110,"audio":"any"}
```

For an episode, also send integer `season` and `episode`. `/start` returns HTTP 200 with:

```json
{"path":"/stream/<hash>/<ticket>","label":"Example Film","phase":"connecting"}
```

The stream path remains relative to the configured service address. Both current apps append `?audio=original`;
Android obtains progress by replacing `/stream/` with `/status/`. The ticket is in the path to preserve this
existing client contract. No app update is needed to use this backend. Links expire six hours after issue;
start the stream again to obtain a new link. The signing key persists in `STATE_ROOT/playback.key`.

Movies and individual episodes are supported. Season packs are not selected. Source ranking estimates whether
a release can arrive fast enough to play, but startup and seeking still depend on seeders and download speed.
The service waits for finished torrent pieces instead of serving unwritten areas of a sparse file.

## Tests

```bash
python -m unittest discover -v
```

Tests use temporary media and local fake Prowlarr, qBittorrent and Seerr servers. They cover authentication,
configuration, session expiry, movie/episode selection, sequential downloads, HTTP ranges, path mapping, cleanup
ownership and the API pattern used by both apps. No real indexer or torrent download is needed for the tests.

Derived from the streaming backend in the original Android TV launcher project. MIT licensed; see [LICENSE](LICENSE).

## Download by voice

A Google Home routine, Home Assistant or IFTTT can ask for a film or series by sending the words it heard. The service
searches Seerr, picks the best match and files the request, so Radarr or Sonarr and your torrent client do the rest.
It is off unless `SEERR_API_KEY` and `VOICE_TOKEN` are both set in `.env`.

```bash
curl -X POST http://YOUR-SERVER:5057/voice/request \
  -H "Authorization: Bearer YOUR_VOICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "download Dune Part Two"}'
# {"status": "requested", "title": "Dune: Part Two (2024)", "say": "Requested Dune: Part Two (2024)."}
```

`status` is `requested`, `already` (asked for before), `available` (already in your library), `not_found`, `unclear`
or `unavailable`; `say` is a sentence a speaker can read back. "the show ..." or "the movie ..." and a year ("Dune 2021")
narrow the match. Series are requested for every season. At most 12 requests a minute. The token is the only guard, so keep
the service on your LAN and never put the token in a public place.

## How a release is chosen

- **Sources.** Each enabled Prowlarr indexer is asked on its own, all at once, so one slow or broken indexer cannot hold up
  the others. Two public Stremio add-ons (Torrentio and MediaFusion) are asked alongside them, and the torrents a Cinema
  app sends as `candidates` (from the owner's own add-ons) join the pool. The add-ons see the title being searched for and
  nothing else; set `STREAM_ADDONS=none` to use only your indexers.
- **Episodes.** An episode's own release is preferred, but a pack of that season is accepted too (its size is shared over
  about ten episodes when judging whether it can keep up). Packs of several seasons, extras and samples are refused.
- **Pick.** It has to start quickly, so only releases with at least half the best seed count compete, and the best
  quality among them wins. A bare info hash becomes a magnet with public trackers added so peers can be found.
- **qBittorrent.** The service only adopts and deletes torrents in its own category (`STREAM_CATEGORY`).
