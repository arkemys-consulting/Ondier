"""Turn public YouTube channel uploads into audio podcast feeds."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

import podcast_videos
import podcasts

CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
CHANNEL_NAME = re.compile(r"[^\s/?#@\\]{1,100}")
CHANNEL_TABS = {"featured", "videos", "shorts", "streams", "playlists", "community", "about", "posts"}
LOCKS = tuple(threading.Lock() for _ in range(16))
SLOTS = threading.BoundedSemaphore(4)
REFRESH = 30 * 60
REBUILD = 24 * 60 * 60
MAX_UPLOAD_PAGES = 30


def api(key, resource, **parameters):
    url = podcast_videos.YOUTUBE + resource + "?" + urlencode({"key": key, **parameters})
    return json.loads(podcast_videos.fetch(url, 2_000_000))


def digest(value):
    return sha256(value.encode()).hexdigest()


def channel_lookup(address):
    parts = urlsplit(address)
    if parts.scheme != "https" or parts.netloc.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    path = [unquote(segment) for segment in parts.path.strip("/").split("/")]
    if len(path) > 1 and path[-1] in CHANNEL_TABS:
        path.pop()
    if len(path) == 1 and path[0].startswith("@") and CHANNEL_NAME.fullmatch(path[0][1:]):
        return "forHandle", path[0]
    if len(path) == 2:
        if path[0] == "channel" and CHANNEL_ID.fullmatch(path[1]):
            return "id", path[1]
        if path[0] == "user" and CHANNEL_NAME.fullmatch(path[1]):
            return "forUsername", path[1]
    return None


def public_uploads(lookup):
    field, value = lookup
    component = {'id': 'channel/', 'forHandle': '', 'forUsername': 'user/'}[field] + quote(value, safe='@')
    address = f"https://www.youtube.com/{component}/videos"
    tools = Path(os.environ.get("QUIRE_YOUTUBE_TOOLS", Path(__file__).parent / "youtube-playback"))
    result = subprocess.run(
        [str(tools / "yt-dlp"), "--ignore-config", "--no-plugin-dirs", "--no-cache-dir",
         "--flat-playlist", "--playlist-end", "20", "--socket-timeout", "8", "--retries", "0",
         "--dump-single-json", "--", address],
        capture_output=True, check=True, timeout=16,
    )
    return json.loads(result.stdout)


def resolve(address):
    try:
        lookup = channel_lookup(address)
    except ValueError:
        lookup = None
    if lookup is None:
        return 400, {"error": "Enter a YouTube channel URL, such as youtube.com/@channel."}
    field, value = lookup
    if field == "id":
        return 200, {"channel": value}
    if not SLOTS.acquire(timeout=10):
        return 503, {"error": "YouTube is busy. Try again in a moment."}
    try:
        key = podcast_videos.key_at(Path(__file__).parent)
        if not key:
            raise ValueError("YouTube is not configured")
        channels = api(key, "channels", part="id", **{field: value}).get("items", [])
        if channels and CHANNEL_ID.fullmatch(channels[0]["id"]):
            return 200, {"channel": channels[0]["id"]}
        return 404, {"error": "YouTube could not find this channel."}
    except (OSError, ValueError, KeyError):
        try:
            info = public_uploads(lookup)
            channel = info.get("channel_id") or info.get("uploader_id") or info.get("id")
            if isinstance(channel, str) and CHANNEL_ID.fullmatch(channel):
                return 200, {"channel": channel}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return 503, {"error": "YouTube could not look up the channel. Try again in a moment."}
    finally:
        SLOTS.release()


def search_channels(query):
    query = query.strip()
    if not 1 <= len(query) <= 200:
        return 400, {"error": "Enter a channel name or handle up to 200 characters."}
    if not SLOTS.acquire(timeout=5):
        return 503, {"error": "YouTube is busy. Try again in a moment."}
    try:
        key = podcast_videos.key_at(Path(__file__).parent)
        if not key:
            return 503, {"error": "Add a YouTube Data API key to your Ondier installation to enable YouTube search."}
        matches = api(key, "search", part="snippet", type="channel", q=query, maxResults=20,
                      fields="items(id/channelId)").get("items", [])
        ids = [item["id"]["channelId"] for item in matches
               if CHANNEL_ID.fullmatch(item.get("id", {}).get("channelId", ""))]
        if not ids:
            return 200, {"items": []}
        channels = api(key, "channels", part="snippet", id=",".join(ids),
                       fields="items(id,snippet(title,customUrl,thumbnails/default/url))").get("items", [])
        details = {channel["id"]: channel["snippet"] for channel in channels}
        items = []
        for identifier in dict.fromkeys(ids):
            snippet = details.get(identifier)
            if snippet is not None and snippet.get("title"):
                items.append({"id": identifier, "title": snippet["title"], "handle": snippet.get("customUrl"),
                              "artwork": snippet.get("thumbnails", {}).get("default", {}).get("url")})
        return 200, {"items": items}
    except (OSError, ValueError, KeyError, TypeError):
        return 503, {"error": "YouTube search is unavailable. Try again in a moment."}
    finally:
        SLOTS.release()


def refresh(channel, previous, now):
    key = podcast_videos.key_at(Path(__file__).parent)
    if not key:
        raise ValueError("YouTube is not configured")
    result = api(key, "channels", part="snippet,contentDetails", id=channel)
    if not result.get("items"):
        return None
    metadata, = result["items"]
    snippet = metadata["snippet"]
    playlist = metadata["contentDetails"]["relatedPlaylists"]["uploads"]
    rebuild = now - previous.get("rebuiltAt", 0) >= REBUILD
    older = previous.get("feed", {}).get("episodes", [])
    known = {item["youtubeAudioID"] for item in older}
    feed_id = digest(f"https://www.youtube.com/channel/{channel}/videos")
    episodes = {}
    checked = set()
    token = ""
    for _ in range(MAX_UPLOAD_PAGES):
        page = api(key, "playlistItems", part="contentDetails", maxResults=50,
                   playlistId=playlist, **({"pageToken": token} if token else {}))
        ids = [item["contentDetails"]["videoId"] for item in page["items"]]
        checked.update(ids)
        if ids:
            details = api(key, "videos", part="snippet,contentDetails,status", id=",".join(ids))
            for item in details["items"]:
                video = item["id"]
                info = item["snippet"]
                if (info["channelId"] != channel or item["status"]["privacyStatus"] != "public"
                        or info.get("liveBroadcastContent") != "none"):
                    continue
                length = podcast_videos.duration(item.get("contentDetails", {}).get("duration") or "")
                if length <= 0:
                    continue
                date = podcast_videos.timestamp(info["publishedAt"]).timestamp()
                episodes[video] = {
                    "id": digest(f"{feed_id}\n{video}"),
                    "title": info["title"],
                    "date": date,
                    "duration": length,
                    "enclosure": f"https://www.youtube.com/watch?v={video}",
                    "mimeType": "audio/mp4",
                    "chapters": [],
                    "description": info.get("description", "")[:2000],
                    "youtubeAudioID": video,
                }
        token = page.get("nextPageToken")
        if not token:
            break
        if not rebuild and known.intersection(ids):
            for item in older:
                video = item["youtubeAudioID"]
                if video not in checked:
                    episodes.setdefault(video, item)
            break
    artwork = snippet.get("thumbnails", {})
    return {
        "checkedAt": now,
        "rebuiltAt": now if rebuild else previous["rebuiltAt"],
        "feed": {
            "title": snippet["title"],
            "publisher": "YouTube",
            "description": snippet.get("description", ""),
            "artwork": (artwork.get("high") or artwork.get("default") or {}).get("url"),
            "episodes": sorted(episodes.values(), key=lambda item: item["date"], reverse=True)[:MAX_UPLOAD_PAGES * 50],
        },
    }


def endpoint(channel):
    if not CHANNEL_ID.fullmatch(channel):
        return 400, {"error": "Invalid YouTube channel ID."}
    path = podcasts.ROOT / "youtube-audio" / f"{channel}.json"
    with LOCKS[hash(channel) % len(LOCKS)]:
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            cache = {}
        now = time.time()
        if now - cache.get("checkedAt", 0) >= REFRESH and SLOTS.acquire(timeout=10):
            try:
                updated = refresh(channel, cache, now)
                if updated is None:
                    return 404, {"error": "YouTube could not find this channel."}
                cache = updated
                path.parent.mkdir(mode=0o700, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(json.dumps(cache, ensure_ascii=False).encode())
                        stream.flush()
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                pass
            finally:
                SLOTS.release()
        if "feed" in cache:
            return 200, cache["feed"]
    return 503, {"error": "YouTube could not provide the podcast catalogue."}
