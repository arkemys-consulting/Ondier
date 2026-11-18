"""Open verified podcast replays and relay YouTube HLS for Quire clients."""

from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import json
import math
import os
import re
import secrets
import struct
import subprocess
import threading
import time

import podcast_videos

FRANCE_ID = re.compile(r"france-tv-(\d{6,12})")
PREFIX = "/podcasts/streams/"
ROUTE = re.compile(PREFIX + r"([a-f0-9]{48})/(\d{1,6})(\.(?:m3u8|m4a|vtt|bin))")
BYTE_RANGE = re.compile(r"bytes=(?:\d{1,11}-\d{0,11}|-\d{1,11})")
PLAYLIST = "application/vnd.apple.mpegurl"
URI = re.compile(r'(?<=\bURI=)"([^"\r\n]+)"')
MAX_PLAYLIST = 2_000_000
MAX_SEGMENT = 32_000_000
AUDIO_CHUNK = 512 * 1024
SLOTS = threading.BoundedSemaphore(3)
LOCK = threading.Lock()
SESSIONS = {}


def upstream_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.netloc != host:
        return False
    return host == "cloudreplay.ftven.fr" or host.endswith(".googlevideo.com") or (
        host == "www.youtube.com" and parts.path == "/api/timedtext"
    )


class MediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, target):
        if not upstream_url(target):
            raise ValueError("Unexpected media redirect")
        return super().redirect_request(request, fp, code, msg, headers, target)


def upstream(url, user_agent, byte_range=None):
    if not upstream_url(url):
        raise ValueError("Unexpected media host")
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    if byte_range and BYTE_RANGE.fullmatch(byte_range):
        headers["Range"] = byte_range
    return build_opener(MediaRedirect()).open(Request(url, headers=headers), timeout=12)


def read_playlist(response):
    data = response.read(MAX_PLAYLIST + 1)
    if len(data) > MAX_PLAYLIST or not data.startswith(b"#EXTM3U"):
        raise ValueError("Invalid upstream playlist")
    return data.decode("utf-8")


@dataclass
class Stream:
    token: str
    expires: float
    agent: str
    duration: float
    resources: list = field(default_factory=list)
    indices: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    master: bytes = b""
    audio_video_id: str = ""
    video_id: str = ""
    desktop_master: bytes = b""

    def add(self, kind, value):
        with self.lock:
            key = (kind, value)
            if key not in self.indices:
                if len(self.resources) >= 60_000:
                    raise ValueError("Too many media resources")
                self.indices[key] = len(self.resources)
                self.resources.append(key)
            return f"{PREFIX}{self.token}/{self.indices[key]}.{kind}"


def rewrite(stream, playlist, origin, captions=()):
    master_variant = False
    output = []
    for line in playlist.splitlines():
        if urlsplit(origin).hostname == "cloudreplay.ftven.fr" and line.startswith(("#EXT-X-SESSION-KEY:", "#EXT-X-KEY:")):
            if (not re.match(r"METHOD=(?:NONE|AES-128)(?:,|$)", line.split(":", 1)[1])
                    or ("KEYFORMAT=" in line and 'KEYFORMAT="identity"' not in line)):
                raise ValueError("Unsupported France TV stream key format")
        if line == "#EXTM3U":
            output.append(line)
            output.extend(captions)
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            master_variant = True
            if captions and "SUBTITLES=" not in line:
                line += ',SUBTITLES="quire-captions"'
        elif line and not line.startswith("#"):
            url = urljoin(origin, line)
            if not upstream_url(url):
                raise ValueError("Unexpected media URL")
            kind = "m3u8" if master_variant else "bin"
            line = stream.add(kind, url)
            master_variant = False
        elif 'URI="' in line:
            kind = "m3u8" if line.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:",
                                               "#EXT-X-RENDITION-REPORT:")) else "bin"
            def replace(match):
                url = urljoin(origin, match[1])
                if not upstream_url(url):
                    raise ValueError("Unexpected playlist URI")
                return '"' + stream.add(kind, url) + '"'
            line = URI.sub(replace, line)
        output.append(line)
    return ("\n".join(output) + "\n").encode()


def hls_attribute(line, name):
    match = re.search(r'(?:^|,)' + name + r'=(?:"([^"]*)"|([^,]*))', line.partition(":")[2])
    return (match[1] if match[1] is not None else match[2]) if match else ""


def set_hls_flag(line, name, value):
    flag = f"{name}={value}"
    updated, count = re.subn(r"([:,])" + name + r"=(?:YES|NO)(?=,|$)", r"\g<1>" + flag, line)
    return updated if count else f"{line},{flag}"


def original_audio_first(playlist):
    lines = playlist.splitlines()
    groups = {}
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:") and hls_attribute(line, "TYPE") == "AUDIO":
            groups.setdefault(hls_attribute(line, "GROUP-ID"), []).append(index)
    for indices in groups.values():
        tracks = [lines[index] for index in indices]
        original = next((track for track in tracks if hls_attribute(track, "NAME").casefold().endswith(" - original")), None)
        if original is None:
            continue
        ordered = sorted(tracks, key=lambda track: track != original)
        for index, track in zip(indices, ordered):
            is_original = index == indices[0]
            track = set_hls_flag(track, "DEFAULT", "YES" if is_original else "NO")
            lines[index] = set_hls_flag(track, "AUTOSELECT", "YES") if is_original else track
    return "\n".join(lines)


def desktop_playlist(playlist):
    lines = playlist.splitlines()
    variants = [(index, int(bandwidth), hls_attribute(line, "AUDIO"))
                for index, line in enumerate(lines[:-1])
                if line.startswith("#EXT-X-STREAM-INF:") and not lines[index + 1].startswith("#")
                and (bandwidth := hls_attribute(line, "BANDWIDTH")).isdigit()]
    if len(variants) < 2:
        return playlist
    selected, _, audio_group = max(variants, key=lambda variant: variant[1])
    excluded = {position for index, _, _ in variants if index != selected for position in (index, index + 1)}
    return "\n".join(line for index, line in enumerate(lines)
                     if index not in excluded and not (line.startswith("#EXT-X-MEDIA:")
                         and hls_attribute(line, "TYPE") == "AUDIO" and hls_attribute(line, "GROUP-ID") != audio_group))


def captions_for(stream, info):
    languages = dict(info.get("subtitles") or {})
    for language, tracks in (info.get("automatic_captions") or {}).items():
        if language.endswith("-orig"):
            languages.setdefault(language.removesuffix("-orig"), tracks)
    result = []
    for language, tracks in languages.items():
        if len(result) >= 24:
            break
        if not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,12}){0,3}", language):
            continue
        track = next((track for track in tracks if track.get("ext") == "vtt" and
                      upstream_url(track.get("url", ""))), None)
        if track is None:
            continue
        name = re.sub(r'["\x00-\x1f\x7f]', "", track.get("name") or language)[:80]
        uri = stream.add("m3u8", ("captions", track["url"]))
        result.append(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="quire-captions",NAME="{name}",'
                      f'LANGUAGE="{language}",DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,URI="{uri}"')
    return result


def video_info(url, *, audio_only=False):
    tools = Path(os.environ.get("QUIRE_YOUTUBE_TOOLS", Path(__file__).parent / "youtube-playback"))
    options = ["--extractor-args", "youtube:skip=hls,dash"] if audio_only else []
    result = subprocess.run(
        [str(tools / "yt-dlp"), "--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--no-playlist",
         "--socket-timeout", "8", "--retries", "0", "--js-runtimes", f"deno:{tools / 'deno'}",
         *options, "--dump-single-json", "--", url],
        capture_output=True, check=True, timeout=24,
    )
    return json.loads(result.stdout)


def resolve(video_id):
    info = video_info("https://www.youtube.com/watch?v=" + video_id)
    duration = float(info.get("duration") or 0)
    if info.get("id") != video_id or not 0 < duration <= 28_800:
        raise ValueError("Invalid video")
    agent = (info.get("http_headers") or {}).get("User-Agent", "Quire Contents/1.0")
    manifests = dict.fromkeys(f["manifest_url"] for f in info.get("formats", [])
                              if f.get("protocol") in {"m3u8", "m3u8_native"} and f.get("manifest_url"))
    for manifest in list(manifests)[:3]:
        try:
            with upstream(manifest, agent) as response:
                playlist = read_playlist(response)
                origin = response.geturl()
            if "#EXT-X-STREAM-INF:" not in playlist or "TYPE=AUDIO" not in playlist or "mp4a" not in playlist:
                continue
            now = time.time()
            expiry = re.search(r"/expire/(\d+)", manifest)
            expires = min(now + 5 * 3600, float(expiry[1]) - 60) if expiry else now + 3600
            if expires <= now:
                continue
            stream = Stream(secrets.token_hex(24), expires, agent, duration, video_id=video_id)
            stream.add("m3u8", manifest)
            stream.add("m3u8", None)
            playlist = original_audio_first(playlist)
            captions = captions_for(stream, info)
            stream.master = rewrite(stream, playlist, origin, captions)
            # mpv selects the highest bitrate by default. Expose it directly so
            # libavformat does not open each resolution and audio group at startup.
            stream.desktop_master = rewrite(stream, desktop_playlist(playlist), origin, captions)
            return stream
        except (OSError, HTTPException, ValueError):
            continue
    raise ValueError("No compatible audio/video playlist")


def france_manifest(url):
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc == "cloudreplay.ftven.fr"
            and parts.path.endswith(".m3u8") and not parts.fragment)


def resolve_france(url):
    info = video_info(url)
    duration = float(info.get("duration") or 0)
    if info.get("extractor_key") != "FranceTV" or not 0 < duration <= 10_800:
        raise ValueError("Invalid France TV replay")
    manifests = dict.fromkeys(f["manifest_url"] for f in info.get("formats", [])
                              if f.get("protocol") in {"m3u8", "m3u8_native"} and f.get("has_drm") is False
                              and f.get("manifest_url") and france_manifest(f["manifest_url"]))
    for manifest in list(manifests)[:3]:
        with build_opener(podcast_videos.NoRedirects()).open(manifest, timeout=8) as response:
            playlist = read_playlist(response)
        if "#EXT-X-STREAM-INF:" in playlist and all(
            (re.match(r"METHOD=(?:NONE|AES-128)(?:,|$)", line.split(":", 1)[1])
             and ("KEYFORMAT=" not in line or 'KEYFORMAT="identity"' in line))
            for line in playlist.splitlines() if line.startswith(("#EXT-X-SESSION-KEY:", "#EXT-X-KEY:"))
        ):
            return {"url": manifest, "duration": duration}
    raise ValueError("No compatible France TV HLS playlist")


def open_france_stream(video_id, index=None):
    if index is None:
        index = podcast_videos.endpoint()
    urls = set()
    for replay in index.get("franceTV", {}).values():
        url = replay["url"]
        if (ref := podcast_videos.france_reference(url)) is not None and ref[1] == video_id:
            urls.add(url)
    if len(urls) != 1:
        return 404, {"error": "This France TV replay is no longer available."}
    if not SLOTS.acquire(blocking=False):
        return 503, {"error": "The video server is busy. Try again in a moment."}
    try:
        return 200, resolve_france(next(iter(urls)))
    except (OSError, HTTPException, ValueError, subprocess.SubprocessError):
        return 503, {"error": "France TV could not deliver this replay to the player."}
    finally:
        SLOTS.release()


def relay_france(result):
    manifest = result["url"]
    if not france_manifest(manifest):
        raise ValueError("Invalid France TV playlist")
    agent = "Ondier/1.0"
    with upstream(manifest, agent) as response:
        playlist = read_playlist(response)
        origin = response.geturl()
    stream = Stream(secrets.token_hex(24), time.time() + 3600, agent, result["duration"])
    stream.add("m3u8", manifest)
    stream.master = rewrite(stream, playlist, origin)
    return register(stream, "m3u8")


def open_stream(video_id, desktop=False, *, index=None):
    if replay := FRANCE_ID.fullmatch(video_id):
        return open_france_stream(replay[1], index)
    unavailable = (404, {"error": "This episode no longer has a verified video."})
    if not podcast_videos.VIDEO_ID.fullmatch(video_id):
        return unavailable
    watch = "https://www.youtube.com/watch?v=" + video_id
    if index is None:
        index = podcast_videos.endpoint()
    if not (any(match["url"] == watch for match in index["links"].values())
            or any(clip["link"]["url"] == watch
                   for clips in index.get("clips", {}).values() for clip in clips)):
        return unavailable
    return open_youtube_video(video_id, desktop=desktop)


def open_youtube_video(video_id, desktop=False):
    now = time.time()
    with LOCK:
        cached = next((stream for stream in SESSIONS.values()
                       if stream.video_id == video_id and stream.expires > now + 300), None)
    if cached:
        return video_response(cached, desktop)
    if not SLOTS.acquire(blocking=False):
        return 503, {"error": "The video server is busy. Try again in a moment."}
    try:
        stream = resolve(video_id)
    except (OSError, HTTPException, ValueError, subprocess.SubprocessError):
        return 503, {"error": "This YouTube video could not be streamed. Try again in a moment."}
    finally:
        SLOTS.release()
    register(stream, "m3u8")
    return video_response(stream, desktop)


def video_response(stream, desktop):
    resource = 1 if desktop and stream.desktop_master else 0
    return 200, {"path": f"{PREFIX}{stream.token}/{resource}.m3u8", "duration": stream.duration}


def register(stream, extension):
    with LOCK:
        for key, previous in tuple(SESSIONS.items()):
            if previous.expires <= time.time():
                del SESSIONS[key]
        if len(SESSIONS) >= 32:
            del SESSIONS[next(iter(SESSIONS))]
        SESSIONS[stream.token] = stream
    return 200, {"path": f"{PREFIX}{stream.token}/0.{extension}", "duration": stream.duration}


def audio_playlist(header, length, media_path):
    offset = 0
    kinds = []
    while offset + 8 <= len(header):
        size, kind = struct.unpack_from(">I4s", header, offset)
        end = offset + size
        if size < 8 or end > len(header):
            break
        if kind == b"sidx":
            if kinds != [b"ftyp", b"moov"] or size < 32:
                break
            version = header[offset + 8]
            timescale = struct.unpack_from(">I", header, offset + 16)[0]
            if version not in (0, 1) or not timescale:
                break
            cursor = offset + (28 if version == 0 else 36)
            if cursor + 4 > end:
                break
            field = "I" if version == 0 else "Q"
            _, first_offset = struct.unpack_from(">" + field * 2, header, offset + 20)
            _, count = struct.unpack_from(">HH", header, cursor)
            if count == 0 or cursor + 4 + count * 12 != end:
                break
            segments = []
            media_offset = end + first_offset
            for reference, ticks, _ in struct.iter_unpack(">III", header[cursor + 4:end]):
                if not 0 < reference < 2**31 or not ticks or media_offset + reference > length:
                    return b""
                segments.append((ticks / timescale, reference, media_offset))
                media_offset += reference
            target = math.ceil(max(duration for duration, _, _ in segments))
            lines = [
                "#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
                "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0",
                f'#EXT-X-MAP:URI="{media_path}",BYTERANGE="{offset}@0"',
            ]
            for duration, size, start in segments:
                lines += [f"#EXTINF:{duration:.6f},", f"#EXT-X-BYTERANGE:{size}@{start}", media_path]
            return ("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n").encode()
        kinds.append(kind)
        offset = end
    return b""


def audio_response(stream, hls):
    resource = "1.m3u8" if hls and stream.master else "0.m4a"
    return 200, {"path": f"{PREFIX}{stream.token}/{resource}", "duration": stream.duration}


def open_audio_stream(video_id, channel, hls=False):
    now = time.time()
    with LOCK:
        cached = next((stream for stream in SESSIONS.values()
                       if stream.audio_video_id == video_id and stream.expires > now + 300), None)
    if cached:
        return audio_response(cached, hls)
    if not SLOTS.acquire(blocking=False):
        return 503, {"error": "The audio server is busy. Try again in a moment."}
    try:
        info = video_info("https://www.youtube.com/watch?v=" + video_id, audio_only=True)
        duration = float(info.get("duration") or 0)
        if info.get("id") != video_id or info.get("channel_id") != channel or not 0 < duration < math.inf:
            raise ValueError("Invalid audio source")
        formats = [f for f in info.get("formats", [])
                   if f.get("protocol") == "https" and f.get("ext") == "m4a"
                   and f.get("vcodec") == "none" and f.get("acodec") == "mp4a.40.2"
                   and upstream_url(f.get("url", ""))]
        for entry in reversed(formats):
            url = entry["url"]
            agent = (entry.get("http_headers") or {}).get("User-Agent", "Quire Contents/1.0")
            try:
                with upstream(url, agent, "bytes=0-65535") as response:
                    content_range = response.headers.get("Content-Range", "")
                    length = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
                    if (response.status != 206 or response.headers.get_content_type() != "audio/mp4"
                            or not length or int(length[1]) != min(65536, int(length[2])) - 1):
                        continue
                    header = response.read(int(length[1]) + 1)
            except (OSError, HTTPException, ValueError):
                continue
            now = time.time()
            expires = parse_qs(urlsplit(url).query).get("expire", [""])[0]
            deadline = min(now + 5 * 3600, int(expires) - 60) if expires.isdigit() else now + 3600
            if deadline <= now:
                continue
            stream = Stream(secrets.token_hex(24), deadline, agent, duration, audio_video_id=video_id)
            size = int(length[2])
            path = stream.add("m4a", (url, size))
            stream.master = audio_playlist(header, size, path)
            if stream.master:
                stream.add("m3u8", None)
            register(stream, "m4a")
            return audio_response(stream, hls)
    except (OSError, HTTPException, ValueError, subprocess.SubprocessError):
        pass
    finally:
        SLOTS.release()
    return 503, {"error": "This YouTube episode could not be streamed as audio."}


def send_audio(handler, stream, value, head):
    url, length = value
    byte_range = handler.headers.get("Range")
    first, last = 0, length - 1
    if byte_range:
        if not BYTE_RANGE.fullmatch(byte_range):
            return handler.send_error(416)
        start, end = byte_range.removeprefix("bytes=").split("-")
        if start:
            first = int(start)
            if end:
                last = min(last, int(end))
        else:
            first = max(0, length - int(end))
        if first > last:
            return handler.send_error(416)

    def headers():
        handler.send_response(206 if byte_range else 200)
        handler.send_header("Content-Type", "audio/mp4")
        handler.send_header("Content-Length", str(last - first + 1))
        if byte_range:
            handler.send_header("Content-Range", f"bytes {first}-{last}/{length}")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "private, no-store")
        handler.end_headers()

    if head:
        return headers()
    sent = False
    try:
        # YouTube throttles open-ended ranges. Keep the client's range intact
        # while fetching it in smaller ranges for startup and seeking.
        for offset in range(first, last + 1, AUDIO_CHUNK):
            end = min(last, offset + AUDIO_CHUNK - 1)
            expected = f"bytes {offset}-{end}/{length}"
            with upstream(url, stream.agent, f"bytes={offset}-{end}") as response:
                if (response.status != 206 or response.headers.get_content_type() != "audio/mp4"
                        or response.headers.get("Content-Range") != expected):
                    raise ValueError("Invalid upstream audio range")
                size = end - offset + 1
                block = response.read(size)
                if len(block) != size:
                    raise ValueError("Incomplete upstream audio range")
                if not sent:
                    headers()
                    sent = True
                handler.wfile.write(block)
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
    except (OSError, HTTPException, ValueError):
        if sent:
            handler.close_connection = True
        else:
            handler.send_error(502)


def send_resource(handler, route, head=False):
    match = ROUTE.fullmatch(route)
    token, number, suffix = match.groups() if match else ("", "0", "")
    with LOCK:
        stream = SESSIONS.get(token)
    index = int(number)
    if stream is None or stream.expires <= time.time() or index >= len(stream.resources):
        handler.send_error(404)
        return
    kind, value = stream.resources[index]
    if suffix != "." + kind:
        handler.send_error(404)
        return
    if kind == "m4a":
        return send_audio(handler, stream, value, head)
    sent = False
    try:
        if (index == 0 and kind == "m3u8") or (index == 1 and stream.audio_video_id and kind == "m3u8"):
            return handler.send_body(200, stream.master, PLAYLIST, head=head)
        if index == 1 and kind == "m3u8" and stream.desktop_master:
            return handler.send_body(200, stream.desktop_master, PLAYLIST, head=head)
        if isinstance(value, tuple) and value[0] == "captions":
            target = stream.add("vtt", value[1])
            duration = stream.duration
            data = (f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:{math.ceil(duration)}\n"
                    f"#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n"
                    f"#EXTINF:{duration:.3f},\n{target}\n#EXT-X-ENDLIST\n")
            return handler.send_body(200, data.encode(), PLAYLIST, head=head)
        with upstream(value, stream.agent, handler.headers.get("Range") if kind == "bin" else None) as response:
            if kind == "m3u8":
                data = rewrite(stream, read_playlist(response), response.geturl())
                return handler.send_body(200, data, PLAYLIST, head=head)
            if kind == "vtt":
                data = response.read(MAX_PLAYLIST + 1)
                if not data.startswith(b"WEBVTT") or len(data) > MAX_PLAYLIST:
                    raise ValueError("Invalid captions")
                return handler.send_body(200, data, "text/vtt", head=head)
            size = response.headers.get("Content-Length")
            if size and (not size.isdigit() or int(size) > MAX_SEGMENT):
                raise ValueError("Media segment too long")
            sent = True
            handler.send_response(response.status)
            for field in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if field in response.headers:
                    handler.send_header(field, response.headers[field])
            handler.send_header("Cache-Control", "private, max-age=60")
            handler.end_headers()
            if not head:
                budget = MAX_SEGMENT
                while budget > 0 and (block := response.read(min(65536, budget))):
                    handler.wfile.write(block)
                    budget -= len(block)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except (OSError, HTTPException, ValueError):
        if sent:
            handler.close_connection = True
        else:
            handler.send_error(502)
