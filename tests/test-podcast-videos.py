"""Regression checks for episode/replay matching and the official channel lookup."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import podcast_videos as replay


NOW = datetime(2026, 11, 15, 5, tzinfo=timezone.utc)
DAY = NOW - timedelta(days=1)
EXPIRY = (NOW + timedelta(days=30)).timestamp()
OTHER_YOUTUBE = "https://www.youtube.com/watch?v=GhiJkl67890"
YOUTUBE = "https://www.youtube.com/watch?v=Abcdef12345"
CHANNEL = "UC" + "f" * 22
STORIO_SYNOPSIS = (
    "La construction des premières voies ferrées transforme les villes portuaires, les échanges commerciaux "
    "et les déplacements des ouvriers. Les mémoires des ingénieurs et les registres municipaux permettent "
    "de suivre le déplacement des ateliers, la croissance des quartiers populaires et les conflits autour "
    "de la propriété des terrains. Les voyageurs découvrent également de nouveaux itinéraires et une autre "
    "manière de mesurer les distances entre les villages."
)


class ReplayTests(unittest.TestCase):
    def test_full_edition_and_sms_share_a_topic_but_not_a_replay(self):
        full = replay.Episode("full", "Teddy Riner, un début de campagne - L’intégrale -", DAY, 3711, frozenset())
        sms = replay.Episode("sms", "Teddy Riner, un début de campagne - Vos questions SMS -", DAY, 312, frozenset())
        program = replay.Video(YOUTUBE, "Teddy Riner, un début de campagne - C dans l’air - 14.11.2026", DAY, 3739, EXPIRY)
        self.assertEqual(set(replay.choose_matches([full, sms], [program], "c-dans-l-air", NOW)), {"full"})
        sms_video = replay.Video(OTHER_YOUTUBE, sms.title, DAY, 325, EXPIRY)
        self.assertEqual(replay.choose_matches([full, sms], [program, sms_video], "c-dans-l-air", NOW)["sms"]["url"], sms_video.url)

    def test_focus_fondamental_matches_renamed_full_episodes_by_number(self):
        episode = replay.Episode("episode", "L’intelligence artificielle se retourne (FFP129)", DAY, 7539, frozenset())
        full = replay.Video(YOUTUBE, "Nvidia et Anthropic : faut-il se méfier ? #FFP129", DAY, 6940, EXPIRY)
        short = replay.Video(OTHER_YOUTUBE, "Un extrait de Nvidia : #FFP129", DAY, 51, EXPIRY)
        wrong = replay.Video(OTHER_YOUTUBE, "L’intelligence artificielle se retourne #FFP128", DAY, 7539, EXPIRY)
        self.assertEqual(replay.choose_matches([episode], [full, short, wrong], "focus-fondamental", NOW)["episode"]["url"], YOUTUBE)
        self.assertFalse(replay.choose_matches([episode], [short, wrong], "focus-fondamental", NOW))

    def test_focus_fondamental_matches_same_day_edited_video_with_shared_episode_description(self):
        intro = ("Les marchés boursiers accélèrent et provoquent un retournement inattendu. "
                 "Hugo et Mathieu analysent les entreprises coréennes spécialisées dans les "
                 "semi-conducteurs et expliquent comment évoluent leurs résultats après plusieurs trimestres.")
        episode = replay.Episode("episode", "Nouvel épisode du 19/04 17:31", DAY, 8111, frozenset(), intro)
        full = replay.Video(YOUTUBE, "Bilan des marchés et valorisation #FFP114", DAY, 6543, EXPIRY, intro + " Voir en vidéo.")
        short = replay.Video(OTHER_YOUTUBE, full.title, DAY, 600, EXPIRY, intro)
        repeat = replay.Video(OTHER_YOUTUBE, full.title, DAY + timedelta(days=1), full.duration, EXPIRY, intro)
        self.assertEqual(replay.choose_matches([episode], [full, short, repeat], "focus-fondamental", NOW)["episode"]["url"], YOUTUBE)
        self.assertFalse(replay.choose_matches([episode], [short, repeat], "focus-fondamental", NOW))

    def test_ambiguous_and_expired_replays_are_hidden(self):
        episode = replay.Episode("one", "La démocratie américaine fait-elle à nouveau rêver ?", NOW, 3751, frozenset())
        first = replay.Video(YOUTUBE, episode.title, DAY, 3760, EXPIRY)
        second = replay.Video(OTHER_YOUTUBE, episode.title, DAY, 3800, EXPIRY)
        self.assertFalse(replay.choose_matches([episode], [first, second], "c-politique", NOW))
        expired = replay.Video(first.url, first.title, DAY, first.duration, DAY.timestamp())
        self.assertFalse(replay.choose_matches([episode], [expired], "c-politique", NOW))

    def test_cdanslair_recent_reports_and_guest_clips_are_separate_from_full_replays(self):
        day = datetime(2026, 11, 14, 18, tzinfo=timezone.utc)
        program = "C dans l’air du 14 novembre 2026. Luc MARTIN, Rose DUPONT"
        full = replay.Episode("full", "La campagne présidentielle - L’intégrale", day, 3750, frozenset(), program)
        sms = replay.Episode("sms", "La campagne présidentielle - Vos questions SMS", day, 310, frozenset(), program)
        guest = replay.Episode("guest", "Alice Durand - La justice", day + timedelta(days=1), 720, frozenset(),
                               "C dans l'air l’invitée du 14 novembre 2026 avec Alice Durand")
        report = replay.Video(YOUTUBE, "Débat : nouvelle campagne", day, 550, EXPIRY, "Luc Martin et Rose Dupont")
        interview = replay.Video(OTHER_YOUTUBE, "Réformer la justice ?", day, 135, EXPIRY,
                                 "L’invitée de C dans l’air Alice Durand dans l’émission Cdanslair du 14 novembre 2026")
        next_day = replay.Video("https://www.youtube.com/watch?v=0123456789A", "Autre débat", day + timedelta(days=1),
                                570, EXPIRY, report.description)
        clips = replay.cdanslair_clip_links([full, sms, guest], [report, interview, next_day], {}, NOW)
        self.assertEqual([v["link"]["url"] for v in clips["full"]], [report.url])
        self.assertEqual([v["link"]["url"] for v in clips["guest"]], [interview.url])
        self.assertNotIn("sms", clips)
        self.assertNotIn("full", replay.cdanslair_clip_links([full], [report], {"full": True}, NOW))

    def test_historical_episode_uses_broadcast_date_and_remains_matchable(self):
        older = NOW - timedelta(days=365)
        episode = replay.Episode("archive", "Les réformes de la justice", older, 3732, frozenset())
        video = replay.Video(YOUTUBE, "Les réformes de la justice - C dans l’air - " + older.strftime("%d.%m.%Y"),
                             older + timedelta(days=31), 3740, EXPIRY)
        self.assertIn("archive", replay.choose_matches([episode], [video], "c-dans-l-air", NOW))

    def test_guest_can_be_published_later_in_week_without_matching_a_different_panel(self):
        episode = replay.Episode("guest", "L’invité de la semaine : Marc Crépon", DAY, 1170, frozenset())
        replay_date = DAY - timedelta(days=3)
        correct = replay.Video(YOUTUBE, "Marc Crépon décrypte les violences - C Politique du 11 novembre 2026",
                               replay_date, 1169, EXPIRY)
        wrong = replay.Video(OTHER_YOUTUBE, "Marc Crépon, le reportage - C Politique du 11 novembre 2026",
                             replay_date, 90, EXPIRY)
        self.assertEqual(replay.choose_matches([episode], [correct, wrong], "c-politique", NOW)["guest"]["url"], YOUTUBE)

    def test_renamed_integral_matches_the_same_panel_and_air_date(self):
        episode = replay.Episode("panel", "L’avenir de l’Europe est-il en Asie ?", NOW, 3689, frozenset(),
                                 "<p>Pierre GROSSER</p><p>Céline SPECTOR</p><p>Muriel DOMENACH</p>")
        correct = replay.Video(YOUTUBE, "L'Europe vers l'Asie : La fin de l'ère américaine ? - C Politique du 14 nov. 2026",
                               NOW, 3668, EXPIRY, "Pierre GROSSER\nCéline SPECTOR\nMuriel DOMENACH")
        wrong = replay.Video(OTHER_YOUTUBE, correct.title, NOW, 3688, EXPIRY, "Pierre GROSSER\nOlivier TESQUET")
        self.assertEqual(replay.choose_matches([episode], [correct, wrong], "c-politique", NOW)["panel"]["url"], YOUTUBE)

    def test_arte_short_debate_keeps_its_own_video(self):
        episode = replay.Episode("debate", "[DÉBAT] Peut-on réparer les crimes de l’esclavage ?", DAY, 1584, frozenset())
        correct = replay.Video(YOUTUBE, "Mémoire de l’esclavage, le temps des réparations ? | 28 minutes | ARTE",
                               DAY, 1373, EXPIRY, "La question des réparations de l'esclavage fait débat.")
        full = replay.Video(OTHER_YOUTUBE, "Peut-on réparer les crimes de l’esclavage ? | 28 minutes | ARTE",
                            DAY, 2738, EXPIRY, correct.description)
        self.assertEqual(replay.choose_matches([episode], [correct, full], "28-minutes", NOW)["debate"]["url"], YOUTUBE)

    def test_storiavoce_never_pairs_different_parts_of_an_interview(self):
        episode = replay.Episode("chapter", "Ottoman : la défense des domaines, avec Olivier Bouquet [3/3]",
                                 DAY, 1373, frozenset())
        correct = replay.Video(YOUTUBE, "Ottoman : la défense des domaines [3/3]", DAY, 1374, EXPIRY)
        other = replay.Video(OTHER_YOUTUBE, "Ottoman : la défense des domaines [2/3]", DAY, 1373, EXPIRY)
        self.assertEqual(replay.choose_matches([episode], [correct, other], "storiavoce", NOW)["chapter"]["url"], YOUTUBE)

    def test_storiavoce_matches_a_renamed_and_delayed_video_by_its_synopsis(self):
        episode = replay.Episode("episode", "L’essor des villes", NOW - timedelta(days=90), 2227, frozenset(),
                                 "<p>" + STORIO_SYNOPSIS + "</p>")
        full = replay.Video(YOUTUBE, "Un territoire en transition", DAY, 2728, EXPIRY, STORIO_SYNOPSIS)
        clip = replay.Video(OTHER_YOUTUBE, "Extrait de l’entretien", DAY, 215, EXPIRY, STORIO_SYNOPSIS)
        self.assertEqual(replay.choose_matches([episode], [full, clip], "storiavoce", NOW)["episode"]["url"],
                         full.url)

    def test_storiavoce_synopsis_does_not_override_an_existing_link_or_a_chapter_number(self):
        full = replay.Episode("full", "L’essor des villes", DAY, 2300, frozenset(), STORIO_SYNOPSIS)
        repeat = replay.Episode("repeat", "Un territoire en transition", DAY, 2300, frozenset(), STORIO_SYNOPSIS)
        current = replay.Video(YOUTUBE, full.title, DAY, 2300, EXPIRY, STORIO_SYNOPSIS)
        self.assertEqual(set(replay.choose_matches([full, repeat], [current], "storiavoce", NOW)), {"full"})
        chapter = replay.Episode("chapter", "L’essor des villes [1/3]", DAY, 2300, frozenset(), STORIO_SYNOPSIS)
        other = replay.Video(OTHER_YOUTUBE, "Un territoire en transition [2/3]", DAY, 2300, EXPIRY, STORIO_SYNOPSIS)
        self.assertFalse(replay.choose_matches([chapter], [other], "storiavoce", NOW))

    def test_storiavoce_recurring_synopsis_does_not_match_an_unrelated_episode(self):
        episode = replay.Episode("episode", "L’essor des villes", NOW - timedelta(days=90), 2300,
                                 frozenset(), STORIO_SYNOPSIS)
        videos = [replay.Video(url, title, DAY, length, EXPIRY, STORIO_SYNOPSIS)
                  for url, title, length in [(YOUTUBE, "L’âge du fer", 2301),
                                             (OTHER_YOUTUBE, "Histoire des Andes", 3000),
                                             ("https://www.youtube.com/watch?v=0123456789A", "Les ports grecs", 900)]]
        self.assertFalse(replay.choose_matches([episode], videos, "storiavoce", NOW))

    def test_late_youtube_upload_requires_guest_or_exact_title_and_duration(self):
        episode = replay.Episode("one", "Effacer les femmes de l’Histoire - Avec Delphine Peresan-Roudil",
                                 DAY - timedelta(days=3), 5990, frozenset())
        video = replay.Video(YOUTUBE, "Effacer les femmes de l’Histoire", DAY, 6060, EXPIRY,
                             "Entretien avec Delphine Peresan-Roudil, historienne de l’art.")
        trailer = replay.Video(YOUTUBE, video.title, DAY, 240, EXPIRY, video.description)
        wrong_guest = replay.Video(YOUTUBE, video.title, DAY, 6060, EXPIRY, "Entretien avec une autre personne.")
        self.assertIn("one", replay.choose_matches([episode], [video], "rivenzi", NOW))
        self.assertFalse(replay.choose_matches([episode], [trailer, wrong_guest], "rivenzi", NOW))

    def test_republished_interview_requires_unique_title_guest_and_duration(self):
        title = "Comment vivaient les chevaliers ? - Entretien avec Nath Dos Reis"
        episode = replay.Episode("interview", title, DAY, 7967, frozenset())
        original_date = DAY - timedelta(days=252)
        video = replay.Video(YOUTUBE, title, original_date, 7968, EXPIRY)
        clip = replay.Video(OTHER_YOUTUBE, title, original_date, 945, EXPIRY)
        duplicate = replay.Video(OTHER_YOUTUBE, title, original_date, 7972, EXPIRY)
        self.assertEqual(replay.choose_matches([episode], [video, clip], "rivenzi", NOW)["interview"]["url"], YOUTUBE)
        self.assertFalse(replay.choose_matches([episode], [video, duplicate], "rivenzi", NOW))
        self.assertFalse(replay.choose_matches([episode], [video], "c-ce-soir", NOW))

    def test_youtube_catalogue_only_accepts_public_videos_from_the_verified_channel(self):
        playlist = "uploads-playlist"
        def fetch(url, limit):
            path = urlsplit(url).path.rsplit("/", 1)[-1]
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["key"], ["test-key"])
            if path == "channels":
                self.assertEqual(params["id"], [CHANNEL])
                items = [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": playlist}}}]
            elif path == "playlistItems":
                self.assertEqual(params["playlistId"], [playlist])
                if "pageToken" not in params:
                    return json.dumps({"items": [{"contentDetails": {"videoId": "GhiJkl67890"}}], "nextPageToken": "second"}).encode()
                items = [{"contentDetails": {"videoId": id}} for id in ("Abcdef12345", "Abcdef12346")]
            else:
                items = [{"id": id, "snippet": {"channelId": channel, "title": "Replay", "publishedAt": DAY.isoformat(),
                                                "liveBroadcastContent": "none"},
                          "contentDetails": {"duration": "PT1H2M15S", "regionRestriction": {"blocked": ["FR"]} if id.endswith("6") else {}},
                          "status": {"privacyStatus": "public", "embeddable": True}}
                         for id, channel in (("Abcdef12345", CHANNEL), ("GhiJkl67890", "other"), ("Abcdef12346", CHANNEL))]
            return json.dumps({"items": items}).encode()
        with patch.object(replay, "fetch", fetch):
            videos = replay.youtube_catalogue(CHANNEL, "test-key", [], NOW)
        self.assertEqual([(video.url, video.duration) for video in videos], [(YOUTUBE, 3735)])

    def test_france_replay_catalogue_uses_publisher_metadata_and_separates_audio(self):
        programme = "https://www.france.tv/france-5/c-politique/"
        page = programme + "saison-18/9024025-emission-du-dimanche-15-novembre-2026.html"
        audio = programme + "9095774-audio-seulement.html"
        suite = "https://www.france.tv/france-5/c-politique-la-suite/9024028-emission-du-dimanche-15-novembre-2026.html"

        def fetch(url, limit):
            if url == programme:
                return f'<a href="{page}">Replay</a><a href="{audio}">Audio</a>'.encode()
            if url.endswith("c-politique-la-suite/"):
                return f'<a href="{suite}">La suite</a>'.encode()
            data = {"@type": "VideoObject" if url != audio else "AudioObject", "url": url, "duration": "PT1H20M",
                    "name": "C politique", "uploadDate": DAY.isoformat(),
                    "expires": (NOW + timedelta(days=1)).isoformat()}
            return ('<script type="application/ld+json">' + json.dumps(data) + '</script>').encode()

        with patch.object(replay, "fetch", fetch):
            videos = replay.france_replays([programme, "https://www.france.tv/france-5/c-politique-la-suite/"], NOW, {})
        self.assertEqual([(item.url, item.duration) for item in videos], [(page, 4800), (suite, 4800)])

        link = {"url": page, "expiresAt": EXPIRY}
        index = {"france-tv:show": {"links": {"episode": link}}}
        with patch.object(replay, "load_index", return_value=index), patch.object(replay.time, "time", return_value=NOW.timestamp()):
            result = replay.endpoint()
        self.assertEqual(result["links"], {})
        self.assertEqual(result["franceTV"], {"episode": link})

    def test_url_validation_rejects_channel_links_and_external_hosts(self):
        self.assertEqual(replay.official_url("https://youtu.be/Abcdef12345"), YOUTUBE)
        self.assertIsNone(replay.official_url("https://www.youtube.com/@rivenzi"))
        self.assertIsNone(replay.official_url("https://www.youtube.com@elsewhere.test/watch?v=Abcdef12345"))
        self.assertIsNone(replay.official_url("https://www.france.tv/france-5/c-dans-l-air/"))

    def test_librairie_matches_renamed_integral_by_panel_and_ignores_clips(self):
        program = "https://www.france.tv/france-5/la-grande-librairie/la-grande-librairie-saison-19/"
        guests = "Susie & Aliyah Morgenstern, Hervé Le Tellier et Barbara Cassin"
        episode = replay.Episode("full", "Trouver les mots", NOW, 5566, frozenset(),
                                 f"<p>On en parle avec {guests}.</p>")
        short = replay.Episode("clip", "Droit dans les yeux - Barbara Cassin", NOW, 105, frozenset())
        integral = replay.Video(program + "9001369-emission-du-vendredi-13-novembre-2026.html",
                                f"La grande librairie {guests}", DAY - timedelta(days=1), 5541, EXPIRY)
        extract = replay.Video(program + "9085631-droit-dans-les-yeux-barbara-cassin.html",
                               short.title, DAY, 107, EXPIRY)
        other = replay.Video(program + "9001370-autres-invites.html",
                             "La grande librairie Laure Murat, Marc Levy et Frédéric Worms", DAY, 5560, EXPIRY)
        links = replay.choose_matches([episode, short], [integral, extract, other], "la-grande-librairie", NOW)
        self.assertEqual({key: link["url"] for key, link in links.items()}, {"full": integral.url})

    def test_c_ce_soir_can_publish_both_replay_and_youtube_fallback(self):
        france = {"url": "https://www.france.tv/france-5/c-ce-soir/9004285-le-debat.html", "expiresAt": EXPIRY}
        youtube = {"url": YOUTUBE, "expiresAt": EXPIRY}
        index = {"france-tv:show": {"links": {"episode": france}}, "c-ce-soir": {"links": {"episode": youtube}}}
        with patch.object(replay, "load_index", return_value=index), patch.object(replay.time, "time", return_value=NOW.timestamp()):
            self.assertEqual(replay.endpoint()["franceTV"], {"episode": france})
            self.assertEqual(replay.endpoint()["links"], {"episode": youtube})
            france["expiresAt"] = DAY.timestamp()
            self.assertEqual(replay.endpoint()["franceTV"], {})
            self.assertEqual(replay.endpoint()["links"], {"episode": youtube})

    def test_c_ce_soir_uses_the_shared_synopsis_when_france_tv_renames_a_debate(self):
        synopsis = ("Hier, à Trappes, la famille de Pengo Bossio Wangi a lancé un appel à la prise de conscience "
                    "et à dépasser le choc et l’émotion pour en faire quelque chose, avec les invités de Karim Rissouli.")
        episode = replay.Episode("full", "Attentat de Trappes : une parole raciste banalisée ?", NOW, 3945,
                                 frozenset(), f"<p>{synopsis}</p>")
        official = replay.Video("https://www.france.tv/france-5/c-ce-soir/9004285-le-debat.html",
                                "Attentat de Trappes : une prise de conscience à la hauteur ?", DAY, 3924,
                                EXPIRY, synopsis)
        self.assertEqual(replay.choose_matches([episode], [official], "c-ce-soir", NOW)["full"]["url"], official.url)


if __name__ == "__main__":
    unittest.main()
