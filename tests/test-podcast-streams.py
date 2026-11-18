"""Regression checks for relaying an indexed podcast HLS playlist."""
from pathlib import Path
from unittest.mock import patch
import struct
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import podcast_streams as relay


class PodcastStreamsTests(unittest.TestCase):
    def setUp(self):
        self.origin = "https://manifest.googlevideo.com/video/master.m3u8"
        self.stream = relay.Stream("0" * 48, time.time() + 300, "Test", 60)
        self.stream.add("m3u8", self.origin)

    def test_master_rewrites_audio_and_video_and_injects_native_captions(self):
        manifest = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="aac"
video/index.m3u8
"""
        captions = relay.captions_for(self.stream, {
            "automatic_captions": {
                "fr-orig": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=test"}],
                "en": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=test&lang=en"}],
            }
        })
        rewritten = relay.rewrite(self.stream, manifest, self.origin, captions).decode()
        self.assertEqual(1, rewritten.count("TYPE=SUBTITLES"))
        self.assertIn('SUBTITLES="quire-captions"', rewritten)
        self.assertNotIn("https:", rewritten)
        self.assertIn(("m3u8", "https://manifest.googlevideo.com/video/audio/index.m3u8"), self.stream.resources)
        self.assertIn(("m3u8", "https://manifest.googlevideo.com/video/video/index.m3u8"), self.stream.resources)

    def test_media_urls_and_redirects_must_stay_on_the_allowlist(self):
        for url in ["http://manifest.googlevideo.com/x", "https://googlevideo.com.other.test/x",
                    "https://www.youtube.com/anything", "https://127.0.0.1/x"]:
            self.assertFalse(relay.upstream_url(url))
            with self.assertRaises(ValueError):
                relay.rewrite(self.stream, "#EXTM3U\n" + url, self.origin)
            with self.assertRaises(ValueError):
                relay.MediaRedirect().redirect_request(None, None, 302, "", {}, url)

    def test_youtube_original_audio_is_default_for_each_rendition_group(self):
        manifest = """#EXTM3U
#EXT-X-MEDIA:URI="en-low.m3u8",TYPE=AUDIO,GROUP-ID="low",NAME="English - dubbed-auto",DEFAULT=NO,AUTOSELECT=YES
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="low",NAME="Français - original",DEFAULT=NO,URI="fr-low.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="high",NAME="English - dubbed-auto",DEFAULT=YES,URI="en-high.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="high",NAME="Français - original",DEFAULT=NO,URI="fr-high.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English - original",DEFAULT=NO,URI="en-subs.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="low"
low.m3u8
#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=900,BANDWIDTH=2000,AUDIO="high",SUBTITLES="subs"
high.m3u8
"""
        master = relay.original_audio_first(manifest)
        for group in ('low', 'high'):
            tracks = [line for line in master.splitlines() if f'GROUP-ID="{group}"' in line]
            self.assertIn('Français - original', tracks[0])
            self.assertIn('DEFAULT=YES', tracks[0])
            self.assertIn('AUTOSELECT=YES', tracks[0])
            self.assertIn('English - dubbed-auto', tracks[1])
            self.assertIn('DEFAULT=NO', tracks[1])
        desktop = relay.desktop_playlist(master)
        self.assertEqual(1, desktop.count('#EXT-X-STREAM-INF:'))
        self.assertIn('AUDIO="high"', desktop)
        self.assertIn('en-high.m3u8', desktop)
        self.assertIn('en-subs.m3u8', desktop)
        self.assertNotIn('GROUP-ID="low"', desktop)
        self.assertNotIn('\nlow.m3u8', desktop)

    def test_video_session_reuses_resolution_for_both_playlists(self):
        video_id = "RkSeuaSyVd0"
        self.stream.video_id = video_id
        self.stream.expires = time.time() + 1800
        self.stream.desktop_master = b"#EXTM3U\n"
        with patch.object(relay, "resolve", return_value=self.stream) as resolve, patch.dict(relay.SESSIONS, clear=True):
            full_status, full = relay.open_youtube_video(video_id)
            desktop_status, desktop = relay.open_youtube_video(video_id, desktop=True)
        self.assertEqual((200, 200), (full_status, desktop_status))
        self.assertTrue(full["path"].endswith("/0.m3u8"))
        self.assertEqual(full["path"][:-6] + "1.m3u8", desktop["path"])
        resolve.assert_called_once_with(video_id)

    def test_verified_clip_can_open_in_same_player_as_full_episode(self):
        clip_id = "RkSeuaSyVd0"
        clip = {"link": {"url": f"https://www.youtube.com/watch?v={clip_id}"}}
        index = {"links": {}, "clips": {"episode": [clip]}}
        with patch.object(relay.podcast_videos, "endpoint", return_value=index), \
             patch.object(relay, "resolve", return_value=self.stream) as resolve, \
             patch.dict(relay.SESSIONS, clear=True):
            status, body = relay.open_stream(clip_id)
        self.assertEqual(200, status)
        self.assertEqual(60, body["duration"])
        resolve.assert_called_once_with(clip_id)

    def test_france_tv_resolves_only_an_exact_indexed_replay(self):
        url = "https://www.france.tv/france-5/c-politique/9024025-emission-du-dimanche.html"
        reply = {"url": "https://cloudreplay.ftven.fr/show/master.m3u8", "duration": 4792}
        with patch.object(relay.podcast_videos, "endpoint",
                          return_value={"franceTV": {"episode": {"url": url}}}), \
             patch.object(relay, "resolve_france", return_value=reply) as resolve:
            self.assertEqual((200, reply), relay.open_stream("france-tv-9024025"))
            self.assertEqual(404, relay.open_stream("france-tv-9024026")[0])
            self.assertEqual(404, relay.open_stream(url)[0])
        resolve.assert_called_once_with(url)

    def test_france_manifest_requires_the_official_cdn_and_https(self):
        self.assertTrue(relay.france_manifest("https://cloudreplay.ftven.fr/show/master.m3u8"))
        self.assertFalse(relay.france_manifest("http://cloudreplay.ftven.fr/show/master.m3u8"))
        self.assertFalse(relay.france_manifest("https://cloudreplay.ftven.fr@other.test/show/master.m3u8"))

    def test_unindexed_video_never_runs_resolver(self):
        with patch.object(relay.podcast_videos, "endpoint", return_value={"links": {}}), \
             patch.object(relay, "resolve") as resolve:
            status, _ = relay.open_stream("RkSeuaSyVd0")
        self.assertEqual(404, status)
        resolve.assert_not_called()

    def test_audio_manifest_uses_segment_offsets_from_the_fragment_index(self):
        init = struct.pack(">I4sI4s", 8, b"ftyp", 8, b"moov")
        index = struct.pack(">I4sIIIIIHHIIIIII", 56, b"sidx", 0, 1, 1000, 0, 7, 0, 2,
                            500, 9800, 0, 150, 3200, 0)
        playlist = relay.audio_playlist(init + index, 729, "/audio.m4a").decode()
        self.assertIn('#EXT-X-MAP:URI="/audio.m4a",BYTERANGE="16@0"', playlist)
        self.assertIn("#EXT-X-TARGETDURATION:10", playlist)
        self.assertIn("#EXTINF:9.800000,\n#EXT-X-BYTERANGE:500@79\n/audio.m4a", playlist)
        self.assertIn("#EXTINF:3.200000,\n#EXT-X-BYTERANGE:150@579\n/audio.m4a", playlist)
        self.assertEqual(b"", relay.audio_playlist(init + index, 728, "/audio.m4a"))
        self.assertEqual(b"", relay.audio_playlist(init + index[:-1], 729, "/audio.m4a"))

    def test_audio_response_keeps_progressive_compatibility(self):
        self.assertTrue(relay.audio_response(self.stream, True)[1]["path"].endswith("/0.m4a"))
        self.stream.master = b"#EXTM3U\n"
        self.assertTrue(relay.audio_response(self.stream, True)[1]["path"].endswith("/1.m3u8"))
        self.assertTrue(relay.audio_response(self.stream, False)[1]["path"].endswith("/0.m4a"))


if __name__ == "__main__":
    unittest.main()
