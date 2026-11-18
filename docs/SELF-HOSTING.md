# Self-hosting Ondier

## First installation

Run `bash install.sh` from the repository root. The script creates `.env` and `data/setup-key` only when they do not already exist, then builds and starts the container.

The default address is `http://localhost:8788`. The port is bound to the host’s loopback interface. The first account requires the key printed by `cat data/setup-key`; that key no longer creates accounts after setup has completed.

For another port, choose it before the first installation:

```sh
ONDIER_PORT=8790 ONDIER_ORIGIN=http://localhost:8790 bash install.sh
```

For an existing installation, edit `ONDIER_PORT` and `ONDIER_ORIGIN` in `.env`, then rerun the installer.

## HTTPS and remote access

Place a trusted HTTPS reverse proxy or a private-network HTTPS service in front of the loopback port. Set `ONDIER_ORIGIN` in `.env` to the exact HTTPS address used by your browser and clients, then run `bash install.sh` again.

Use the same address in Quire and other clients. A changed installation address requires updating those clients. Retain the installation key when moving your data.

## YouTube and video matching

RSS subscriptions and Apple Podcasts search work without a YouTube API key. To enable searching YouTube by channel name, set `QUIRE_YOUTUBE_API_KEY` in `.env` and rerun the installer. Ondier uses yt-dlp and Deno for supported YouTube media routes.

Open a show, then **Show Settings → Video Matching** to choose a YouTube channel or France.tv catalogue, provider order, and matching rules. Use **Edit Video Match** in an episode’s options for a manual override. Playback availability depends on the publisher and the viewer’s region.

## What is stored

`data/` contains subscriptions, playback state, indexed episode metadata, account records, API-key hashes, cached feeds, video matches, and backups. The installation key stays in `.env`.

The episode archive preserves metadata, not a permanent copy of every publisher’s audio file. A removed or expired media URL may stop playing even when its episode remains searchable.

## Backups and restore

**Settings → Download Backup** exports a signed ZIP. Daily backups are kept in `data/backups/`; only the seven most recent are retained. Store `.env` separately with your backups. Treat private RSS URLs, app passwords and backup archives as private.

Verify an archive without restoring it:

```sh
docker compose run --rm --no-deps ondier \
  python backups.py verify /data/backups/ondier-YYYY-MM-DD.zip
```

To restore a chosen backup:

```sh
docker compose stop ondier
docker compose run --rm --no-deps ondier \
  python backups.py restore /data/backups/ondier-YYYY-MM-DD.zip
docker compose up -d --no-build
```

Restoration creates a rollback backup. Sign in again afterwards. API keys that were valid at the backup date remain valid.

## Updates

Keep `.env` and `data/`, then run:

```sh
git pull
bash install.sh
```

Check `docker compose ps` for the container state and `docker compose logs --tail=100 ondier` for startup errors.
