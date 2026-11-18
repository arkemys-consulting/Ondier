"""Regression checks for YouTube channel catalogues."""
from pathlib import Path
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import podcast_youtube_audio as youtube


class ChannelCatalogueTests(unittest.TestCase):
    def test_scheduled_upload_does_not_break_the_catalogue_and_appears_after_release(self):
        channel = "UC" + "a" * 22
        scheduled = {
            "id": "a" * 11,
            "snippet": {"channelId": channel, "title": "Premiere", "liveBroadcastContent": "upcoming",
                        "publishedAt": "2026-11-17T10:00:00Z"},
            "status": {"privacyStatus": "public"},
            "contentDetails": {},
        }
        published = {
            **scheduled,
            "id": "b" * 11,
            "snippet": {**scheduled["snippet"], "title": "Interview", "liveBroadcastContent": "none",
                        "publishedAt": "2026-11-16T10:00:00Z"},
            "contentDetails": {"duration": "PT23M"},
        }
        incomplete = {**published, "id": "c" * 11, "contentDetails": {}}
        responses = {
            "channels": {"items": [{"snippet": {"title": "Bookshop"}, "contentDetails": {
                "relatedPlaylists": {"uploads": "uploads"}
            }}]},
            "playlistItems": {"items": [{"contentDetails": {"videoId": item["id"]}}
                                       for item in (scheduled, published, incomplete)]},
            "videos": {"items": [scheduled, published, incomplete]},
        }

        with patch.object(youtube.podcast_videos, "key_at", return_value="test"), \
             patch.object(youtube, "api", side_effect=lambda key, resource, **params: responses[resource]):
            initial = youtube.refresh(channel, {}, 1_800_000_000)
            self.assertEqual([published["id"]], [item["youtubeAudioID"] for item in initial["feed"]["episodes"]])
            scheduled["snippet"]["liveBroadcastContent"] = "none"
            scheduled["contentDetails"]["duration"] = "PT30M"
            refreshed = youtube.refresh(channel, initial, 1_800_003_600)

        self.assertEqual([scheduled["id"], published["id"]],
                         [item["youtubeAudioID"] for item in refreshed["feed"]["episodes"]])
        self.assertEqual(initial["feed"]["episodes"][0]["id"], refreshed["feed"]["episodes"][1]["id"])


if __name__ == "__main__":
    unittest.main()
