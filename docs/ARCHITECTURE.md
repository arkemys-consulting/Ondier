# Architecture

Ondier runs as one Python HTTP service with a JavaScript web client. SQLite stores accounts and indexed episodes; subscriptions and playback preferences are persisted in the data directory.

- `app/server.py` serves the web client, authenticated API, private RSS feeds and gPodder sync routes.
- `app/accounts.py` manages first-account setup, password sign-in, browser sessions and revocable API keys.
- `app/archive.py`, `feeds.py`, `podcasts.py` and `listening.py` handle feed parsing, the episode index, subscriptions, playlists and listening state.
- `app/podcast_streams.py`, `podcast_videos.py`, `podcast_youtube_audio.py` and `video_matching.py` handle supported audio/video routes and episode-to-video matching.
- `app/backups.py` creates and restores signed backups.
- `app/web/` contains the browser interface, application icon and vendored hls.js player.

Docker sets `ONDIER_DATA=/data`. For a Python-only development launch, set that variable to a writable directory and provide `ONDIER_ORIGIN`, `ONDIER_ACCESS_KEY` and a setup-key file through `ONDIER_SETUP_KEY_FILE`. The Dockerfile documents the supported runtime dependencies.

Browser requests use a session cookie; native clients use a named API key as a Bearer token. `GET /api/client` verifies a client connection. The `/podcasts/` routes expose subscriptions, progress, episode search and video links. `/api/saved`, `/api/queue`, `/api/listening` and `/api/video-playback/<id>` provide the richer Ondier listening state.

The server also offers gPodder-compatible routes under `/api/2/` and a Nextcloud-compatible authorization flow. RSS alone does not synchronize playback positions.
