"""Match podcast episodes to videos published by their official channels and France TV."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import podcasts

INTERVAL = 30 * 60
HISTORY_INTERVAL = 12 * 60 * 60
HISTORY_PAGES = 70
PARIS = ZoneInfo("Europe/Paris")
YOUTUBE = "https://www.googleapis.com/youtube/v3/"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
FRANCE_ORIGIN = "https://www.france.tv"
FRANCE_CHANNEL = r"(?:france-[2-5]|france-tv|slash|documentaires)"
FRANCE_PATH = re.compile(rf"/{FRANCE_CHANNEL}/([a-z0-9-]+)/(?:(?:[a-z0-9-]+)/)?(\d{{6,12}})-[a-z0-9-]+\.html")
FRANCE_CATALOGUE = re.compile(rf"/{FRANCE_CHANNEL}/[a-z0-9-]+/?")
NEWS_SHOWS = {"c-dans-l-air", "c-ce-soir", "c-politique", "28-minutes"}
MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
          "septembre", "octobre", "novembre", "decembre")
MONTH_NUMBERS = {month: index for index, month in enumerate(MONTHS, 1)}
MONTH_NUMBERS.update(jan=1, janv=1, fev=2, fevr=2, avr=4, juil=7, sept=9, sep=9,
                     oct=10, nov=11, dec=12)
STOP_WORDS = set("avec dans de des du en est et la le les leur mon nos par pas pour que qui son sur tes une vos un aux elle ces ils cette ses ce se au plus".split())
NEWS_FILLER = set("debat sujet emission reportage temps question questions aujourd hui arte minutes club"
                  " international numero version partie complet complete nouveau nouvelle".split())


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    date: datetime
    duration: float
    direct: frozenset
    description: str = ""


@dataclass(frozen=True)
class Video:
    url: str
    title: str
    date: datetime
    duration: float
    expires: float
    description: str = ""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url, limit):
    request = Request(url, headers={"User-Agent": "Quire Contents/1.0"})
    with build_opener(NoRedirects()).open(request, timeout=10) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Publisher response exceeds size limit")
    return data


def timestamp(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("Unqualified publisher timestamp")
    return date


def duration(value):
    try:
        parts = value.strip().split(":")
        if 1 <= len(parts) <= 3 and all(re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
            return sum(float(part) * 60 ** n for n, part in enumerate(reversed(parts)))
        iso = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
        return sum(float(part or 0) * unit for part, unit in zip(iso.groups(), (3600, 60, 1))) if iso else 0
    except ValueError:
        return 0


@lru_cache(maxsize=8192)
def normalized(title):
    for _ in range(3):
        decoded = unescape(title)
        if decoded == title:
            break
        title = decoded
    plain = re.sub(r"<[^>]+>", " ", title).casefold().replace("œ", "oe").replace("æ", "ae")
    plain = "".join(c for c in unicodedata.normalize("NFKD", plain) if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def official_url(raw):
    if not raw or not isinstance(raw, str):
        return None
    try:
        url = urlsplit(raw.strip())
    except ValueError:
        return None
    if url.scheme != "https" or url.username or url.password:
        return None
    if url.netloc in {"youtube.com", "www.youtube.com", "youtu.be"}:
        identifier = url.path[1:] if url.netloc == "youtu.be" else (
            parse_qs(url.query).get("v", [""])[0] if url.path == "/watch"
            else url.path.removeprefix("/embed/") if url.path.startswith("/embed/") else ""
        )
        if VIDEO_ID.fullmatch(identifier):
            return f"https://www.youtube.com/watch?v={identifier}"
    return None


def france_reference(raw):
    if not isinstance(raw, str):
        return None
    try:
        url = urlsplit(raw)
    except ValueError:
        return None
    path = FRANCE_PATH.fullmatch(url.path)
    if url.scheme == "https" and url.netloc == "www.france.tv" and not url.query and not url.fragment and path:
        return path.groups()
    return None


class FranceReplayPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.structured = []
        self.script = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.script = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            try:
                self.structured.append(json.loads("".join(self.script)))
            except ValueError:
                pass
            self.script = None


def france_page(url):
    page = FranceReplayPage()
    page.feed(fetch(url, 2_000_000).decode("utf-8"))
    return page


def france_metadata(url):
    for item in france_page(url).structured:
        if not isinstance(item, dict) or item.get("@type") != "VideoObject" or item.get("url") != url:
            continue
        length = duration(item.get("duration", ""))
        if length < 90 or length > 10_800 or not item.get("name"):
            continue
        try:
            return Video(url, item["name"], timestamp(item["uploadDate"]), length,
                         timestamp(item["expires"]).timestamp(), item.get("description") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return None


def france_replays(catalogues, now, cache):
    urls = {}
    loaded = False

    def collect(page):
        for href in page.links:
            address = urljoin(FRANCE_ORIGIN, href)
            ref = france_reference(address)
            if ref and any(address.startswith(catalogue) for catalogue in catalogues):
                urls.setdefault(ref[1], address)

    for origin in catalogues:
        try:
            page = france_page(origin)
        except (OSError, ValueError):
            continue
        loaded = True
        collect(page)
        seasons = dict.fromkeys(urljoin(FRANCE_ORIGIN, href) for href in page.links)
        season_path = re.compile(re.escape(origin) + r"(?:[a-z0-9-]+-)?saison-\d{1,3}/")
        for season in [url for url in seasons if season_path.fullmatch(url)][:2]:
            try:
                collect(france_page(season))
            except (OSError, ValueError):
                continue
    if not loaded:
        raise OSError("The France.tv catalogues could not be loaded")
    candidates = list(urls.values())[:140]
    checked_at = now.timestamp()

    def get(url):
        last, video = cache.get(url, (0, None))
        if checked_at - last < (6 * 3600 if video else 2 * 3600):
            return last, video
        try:
            return checked_at, france_metadata(url)
        except (OSError, ValueError, TypeError):
            return last, video

    videos = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for url, (last, video) in zip(candidates, pool.map(get, candidates)):
            if last > 0:
                cache[url] = last, video
            if video and video.expires > checked_at and video.date <= now + timedelta(minutes=5):
                videos.append(video)
    for url, (last, _) in tuple(cache.items()):
        if checked_at - last >= 48 * 3600:
            del cache[url]
    return videos


def load_episodes(feed, now, *, show_id=None, raw=None):
    show_id = show_id or sha256(feed.encode()).hexdigest()
    root = ElementTree.fromstring(fetch(feed, 12_000_000) if raw is None else raw)
    episodes = {}
    for item in root.findall("channel/item")[:1500]:
        title = item.findtext("title", "").strip()
        enclosure = next((node for node in item.findall("enclosure") if
                          not node.get("type") or node.get("type", "").lower() == "application/octet-stream"
                          or node.get("type", "").lower().startswith("audio/")), None)
        try:
            date = parsedate_to_datetime(item.findtext("pubDate", ""))
        except (ValueError, TypeError):
            continue
        if not title or enclosure is None or not enclosure.get("url") or date.tzinfo is None \
                or date > now + timedelta(days=1):
            continue
        audio = urljoin(feed, enclosure.get("url", ""))
        guid = item.findtext("guid", "").strip() or audio
        identifier = sha256(f"{show_id}\n{guid}".encode()).hexdigest()
        direct = [item.findtext("link"), *(n.get("url") for n in item.findall("enclosure")
                                           if n.get("type", "").lower().startswith("video/"))]
        for alt in item.findall("{*}alternateEnclosure"):
            if alt.get("type", "").lower().startswith("video/"):
                direct.extend(source.get("uri") for source in alt.findall("{*}source"))
        direct = frozenset(url for item_url in direct if (url := official_url(item_url)))
        episodes.setdefault(identifier, Episode(identifier, title, date, duration(item.findtext("{*}duration", "")),
                                                direct, item.findtext("description", "")[:6000]))
    return tuple(episodes.values())


def title_parts(value):
    title = normalized(value)
    title = re.sub(r"\bjoueurdugrenier\b", "joueur du grenier", title)
    title = re.sub(r"^(?:l invite de )?c (?:dans l air|ce soir|politique(?: la suite)?) ", "", title)
    title = re.sub(r" (?:l integrale|integrale|vos questions sms|vos questions par sms)$", "", title)
    title = re.sub(r" c (?:dans l air|ce soir|politique)(?: la suite)?"
                   r"(?: (?:du )?\d{1,2}(?:er)? (?:[a-z]+|\d{1,2}) (?:20)?\d{2})?$", "", title)
    title = re.sub(r" 28 minutes(?: arte)?$", "", title)
    title = re.sub(r"^debat |(?<=le club) 28$", "", title)
    return title


def broadcast_date(title):
    suffix = re.search(r" c (?:dans l air|ce soir|politique(?: la suite)?) (?:du )?"
                       r"(\d{1,2})(?:er)? ([a-z]+|\d{1,2}) ((?:20)?\d{2})$",
                       normalized(title))
    return day_from(suffix)


def day_from(parts):
    if not parts:
        return None
    try:
        day, month, year = parts.groups()
        month = int(month) if month.isdigit() else MONTH_NUMBERS[month]
        return date(int(year) + (2000 if len(year) == 2 else 0), month, int(day))
    except (KeyError, ValueError):
        return None


def air_date(video, show):
    if france_reference(video.url):
        day = day_from(re.search(r"-emission-du-[a-z]+-(\d{1,2})(?:er)?-([a-z]+)-(20\d{2})\.html$", video.url))
        if day is not None:
            return day
    explicit = broadcast_date(video.title)
    if explicit is not None:
        return explicit
    description = normalized(video.description[:1750])
    if show == "c-dans-l-air":
        return day_from(re.search(r"\bemission (?:c dans l air|cdanslair) du "
                                  r"(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) )?"
                                  r"(\d{1,2})(?:er)? ([a-z]+) (20\d{2})", description))
    if show == "rivenzi":
        return day_from(re.search(r"\brediffusion du live (?:histoire )?du (\d{1,2}) (\d{1,2}) (20\d{2})",
                                  description))
    return None


def video_day(video, show):
    return air_date(video, show) or video.date.astimezone(PARIS).date()


def first_guest(title):
    parts = re.split(r"\b(?:avec|ft|feat)\b", title)
    if len(parts) < 2:
        return ()
    words = tuple(word for word in parts[-1].split()[:5] if word not in STOP_WORDS)[:2]
    return words if len(words) == 2 else ()


def political_guest(title):
    match = re.match(r"l invite (?:de la semaine|du \d{1,2}(?:er)? [a-z]+(?: 20\d{2})?) (\w+ \w+)", title)
    return match[1] if match else None


def news_words(title):
    words = set(title.split()) - STOP_WORDS - NEWS_FILLER
    return {re.sub(r"^repar(?:er|ation|ations)$", "repar", word) for word in words}


def arte_score(episode, video, days, left, right, difference):
    if abs(days) > 1:
        return None
    # ARTE publishes a full edition, a debate and sometimes a short interview.
    if (episode.duration >= 2000) != (video.duration >= 2000):
        return None
    left_words, right_words = news_words(left), news_words(right)
    shared = left_words & right_words
    smaller = min(len(left_words), len(right_words))
    if difference <= 130 and len(shared) >= 4 and smaller and len(shared) / smaller >= .75:
        return 94
    if days == 0 and difference <= 90 and episode.duration >= 2350:
        before_slash = episode.title.split(" / ", 1)
        after_slash = video.title.split(" / ", 1)
        if len(before_slash) == len(after_slash) == 2:
            first = news_words(normalized(before_slash[0]))
            other = news_words(normalized(after_slash[0]))
            if len(first) >= 2 and first == other and len(shared - first) >= 1:
                return 93
    debate = normalized(episode.title).startswith("debat ")
    if days == 0 and debate and 1190 <= video.duration <= 1740 and difference <= 250 and len(shared) >= 2:
        # A differently titled debate must also identify the same specific subject in its synopsis.
        description = news_words(normalized(video.description[:1200]))
        if shared <= description and (len(shared) >= 3 or {"esclavage", "repar"} <= shared):
            return 96
    return None


def interview_part(title):
    numbered = re.search(r"\b(?:partie|episode)\s+([1-9])\b", normalized(title))
    if numbered:
        return int(numbered[1])
    fraction = re.search(r"(?<![\w/])([1-9]|iv|iii|ii|i)\s*/\s*([1-9]|iv|iii|ii|i)(?![\w/])", title, re.I)
    if fraction:
        numerator = fraction[1].lower()
        return int(numerator) if numerator.isdigit() else ("i", "ii", "iii", "iv").index(numerator) + 1
    return None


def storio_score(left, right, days, difference):
    if abs(days) > 2 or difference > 115:
        return None
    # Storiavoce confirmed the correct given name on the episode's publisher page.
    left = left.replace("marie dominique vincent", "marie benedicte vincent")
    left_topic, _, left_name = left.partition(" avec ")
    right_topic, _, right_name = right.partition(" avec ")
    first, second = news_words(left_topic), news_words(right_topic)
    shared = first & second
    ratio = SequenceMatcher(None, left_topic, right_topic, autojunk=False).ratio()
    if left_name and right_name:
        left_names = [name for name in left_name.split() if len(name) > 1]
        right_names = [name for name in right_name.split() if len(name) > 1]
        names_agree = left_names == right_names or (
            len(left_names) == len(right_names) and
            all(SequenceMatcher(None, a, b, autojunk=False).ratio() >= .85 for a, b in zip(left_names, right_names))
        )
        if not names_agree:
            return None
        if len(shared) >= 3 and ratio >= .79:
            return 105
    if left_topic == right_topic and len(shared) >= 3:
        return 105
    # Older shows often have exactly the same recording under a longer video title.
    if difference <= 5 and len(shared) >= 3 and ratio >= .66 and len(shared) >= .65 * min(len(first), len(second)):
        return 86
    return None


def add_storiavoce_synopsis_matches(episodes, videos, links, now):
    """Recover official videos with a renamed title or a delayed upload."""
    remaining = [episode for episode in episodes if episode.id not in links and episode.duration > 0]
    if not remaining:
        return

    descriptions = {episode.id: normalized(episode.description).split()[:400] for episode in remaining}
    phrases = {tuple(words[offset:offset + 8]) for words in descriptions.values() for offset in range(len(words) - 7)}
    by_phrase = defaultdict(list)
    catalogue = []
    for video in videos:
        if video.expires <= now.timestamp() or not video.duration:
            continue
        words = normalized(video.description).split()[:400]
        if len(words) < 26:
            continue
        for phrase in phrases.intersection(tuple(words[offset:offset + 8]) for offset in range(len(words) - 7)):
            by_phrase[phrase].append(len(catalogue))
        catalogue.append((video, words, interview_part(video.title)))

    occupied = {link["url"] for link in links.values()}
    proposals = defaultdict(list)
    for episode in remaining:
        words = descriptions[episode.id]
        day = episode.date.astimezone(PARIS).date()
        topic = news_words(title_parts(episode.title))
        candidates = {index for offset in range(len(words) - 7)
                      for index in by_phrase.get(tuple(words[offset:offset + 8]), ())}
        episode_part = interview_part(episode.title)
        ranked = []
        for index in candidates:
            video, synopsis, video_part = catalogue[index]
            if episode_part is not None and video_part is not None and episode_part != video_part:
                continue
            difference = abs(video.duration - episode.duration)
            if difference > max(15, episode.duration * .25):
                continue
            matches = SequenceMatcher(None, words, synopsis, autojunk=False).get_matching_blocks()
            # Recurring biographies and promotional text must not identify an episode.
            shared = max((size for _, start, size in matches if size >= 26 and any(
                len(by_phrase[tuple(synopsis[offset:offset + 8])]) <= 2
                for offset in range(start, start + size - 7)
            )), default=0)
            if shared >= 26 and (difference <= max(15, episode.duration * .10) or shared >= 64):
                score = shared
                if difference <= 2 and abs((day - video.date.astimezone(PARIS).date()).days) <= 2 \
                        and len(topic & news_words(title_parts(video.title))) >= 2:
                    # The same upload date, runtime and topic carry more weight than a recurring guest bio.
                    score += 60
                ranked.append((score, video))
        ranked.sort(key=lambda candidate: candidate[0], reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 20):
            continue
        score, video = ranked[0]
        if video.url not in occupied:
            proposals[video.url].append((score, episode.id, video))

    for candidates in proposals.values():
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 20:
            continue
        _, episode_id, video = candidates[0]
        links[episode_id] = {"url": video.url, "expiresAt": int(min(video.expires, now.timestamp() + 48 * 3600))}


@lru_cache(maxsize=4096)
def panel_names(description):
    guests = re.findall(r"\b([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:-[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+)?"
                        r"\s+[A-ZÀ-ÖØ-Þ]{3,}(?:-[A-ZÀ-ÖØ-Þ]{3,})*)\b", description)
    return frozenset(normalized(name) for name in guests)


def same_political_panel(episode, video):
    # The synopsis lists the full panel using a capitalized first name and uppercase surname.
    description = re.sub(r"<[^>]+>", " ", unescape(episode.description))
    synopsis = f" {normalized(video.description[:6000])} "
    return sum(f" {guest} " in synopsis for guest in panel_names(description)) >= 2


def librairie_score(episode, video, days, difference):
    if not 0 <= days <= 2 or min(episode.duration, video.duration) < 3000 or difference > 75:
        return None
    replay_title = normalized(video.title).removeprefix("la grande librairie ")
    if replay_title == normalized(episode.title) and len(replay_title.split()) >= 2:
        return 110
    # France TV often titles the integral with the guest list and the RSS uses the theme.
    guest_list = re.sub(r"^la grande librairie\s*[:–-]?\s*", "", video.title, flags=re.IGNORECASE)
    guests = map(normalized, re.split(r"\s*(?:,|&|\bet\b)\s*", guest_list, flags=re.IGNORECASE))
    names = {guest for guest in guests if len(guest.split()) >= 2}
    synopsis = f" {normalized(re.sub(r'<[^>]+>', ' ', unescape(episode.description)))} "
    if sum(f" {name} " in synopsis for name in names) >= 2:
        return 105
    # Special editions and some archive pages put the guests in the synopsis.
    names = {normalized(name) for name in re.findall(
        r"\b[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ-]+\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ-]+\b", video.description[:1500])}
    if len({name for name in names if f" {name} " in synopsis} - {"la grande", "grande librairie", "augustin trapenard"}) >= 2:
        return 105
    tribute = re.fullmatch(r"emission speciale hommage a (.+)", replay_title)
    if tribute and len(tribute[1].split()) >= 2 and normalized(episode.title).endswith(" " + tribute[1]):
        return 105
    return None


def cdanslair_episode_day(episode):
    description = normalized(episode.description[:400])
    broadcast = re.search(r"^c dans l air (?:l invitee? )?du (\d{1,2})(?:er)? ([a-z]+) (20\d{2})",
                          description)
    return day_from(broadcast) or episode.date.astimezone(PARIS).date()


def cdanslair_guest_names(episode):
    if not re.match(r"^c dans l air l invitee? du ", normalized(episode.description[:400])):
        return ()
    name, *topic = re.split(r"\s[-–—]\s", episode.title, maxsplit=1)
    if not topic:
        return ()
    return tuple(guest for guest in normalized(name).split(" et ") if len(guest.split()) >= 2)


def cdanslair_clip_links(episodes, videos, exact, now):
    # The channel releases reports first, followed by whole programmes weeks later.
    by_day = defaultdict(list)
    for video in videos:
        if not 60 <= video.duration <= 1500 or video.expires <= now.timestamp():
            continue
        broadcast = air_date(video, "c-dans-l-air")
        published = video.date.astimezone(PARIS).date()
        by_day[broadcast or published].append((video, broadcast))

    candidates = defaultdict(list)
    for episode in episodes:
        if episode.id in exact:
            continue
        guest_names = cdanslair_guest_names(episode)
        if not guest_names and episode.duration < 2700:
            continue
        day = cdanslair_episode_day(episode)
        full_description = re.sub(r"<[^>]+>", " ", unescape(episode.description))
        for video, broadcast in by_day[day]:
            if video.duration >= episode.duration - 45:
                continue
            description = normalized(video.description)
            guest_clip = bool(re.search(r"\bl invitee? de c dans l air\b", description[:450]))
            if guest_names:
                if broadcast == day and guest_clip and any(f" {name} " in f" {description} " for name in guest_names):
                    candidates[video.url].append((3, episode, video))
                continue
            if guest_clip:
                continue
            if same_political_panel(episode, video):
                candidates[video.url].append((2, episode, video))
            elif broadcast == day and any(
                    f" {name} " in f" {description} " for name in panel_names(full_description)):
                candidates[video.url].append((1, episode, video))

    linked = defaultdict(list)
    for matches in candidates.values():
        matches.sort(key=lambda item: item[0], reverse=True)
        score, episode, video = matches[0]
        if len(matches) == 1 or score > matches[1][0]:
            linked[episode.id].append((score, video))
    return {identifier: [
        {"title": unescape(video.title), "duration": video.duration,
         "link": {"url": video.url, "expiresAt": int(min(video.expires, now.timestamp() + 48 * 3600))}}
        for _, video in sorted(items, key=lambda item: (-item[0], -item[1].duration, item[1].title))[:6]
    ] for identifier, items in linked.items()}


def matching_score(episode, video, show, now, date_window=None, max_difference=None):
    ep_day = cdanslair_episode_day(episode) if show == "c-dans-l-air" else episode.date.astimezone(PARIS).date()
    air_day = air_date(video, show)
    video_day = air_day or video.date.astimezone(PARIS).date()
    days = (ep_day - video_day).days
    published_title = normalized(episode.title)
    guest_name = political_guest(published_title) if show == "c-politique" else None
    if date_window is not None:
        date_matches = abs(days) <= date_window
    elif guest_name:
        date_matches = 0 <= days <= 5
    else:
        date_matches = -2 <= days <= 1 if show in NEWS_SHOWS else -14 <= days <= 14
    if video.expires <= now.timestamp() or not date_matches:
        return None
    if not episode.duration or not video.duration:
        return None
    difference = abs(episode.duration - video.duration)
    if show == "la-grande-librairie":
        return librairie_score(episode, video, days, difference)
    if show == "c-ce-soir" and france_reference(video.url) and 0 <= days <= 1 \
            and min(episode.duration, video.duration) >= 3300 and difference <= 75:
        audio = normalized(re.sub(r"<[^>]+>", " ", unescape(episode.description))).split()[:240]
        replay = normalized(video.description).split()[:240]
        if SequenceMatcher(None, audio, replay, autojunk=False).find_longest_match().size >= 24:
            return 120
    video_title = normalized(video.title)
    if show == "focus-fondamental":
        episode_number = re.search(r"\b(ffp|fff|lcr)\s*(\d+)\b", published_title)
        video_number = re.search(r"\b(ffp|fff|lcr)\s*(\d+)\b", video_title)
        if episode_number and video_number and episode_number.groups() == video_number.groups() \
                and difference <= max(15, episode.duration * .12):
            # Full video editions can be edited and carry a different title from the RSS episode.
            return 135
        if days == 0 and difference <= max(15, episode.duration * .22):
            # Baggr uses the same episode opening across formats, even when the RSS title is incomplete.
            intro = normalized(episode.description[:1600]).split()[:28]
            if len(intro) == 28 and len(set(intro) - STOP_WORDS) >= 14 \
                    and intro == normalized(video.description[:1600]).split()[:28]:
                return 134
        if episode_number and video_number:
            return None

    tolerance = (max(45, min(125, episode.duration * .12)) if episode.duration < 1800
                 else max(135, min(400, episode.duration * .12)))
    if max_difference is not None:
        tolerance = max_difference
    arte_debate = show == "28-minutes" and published_title.startswith("debat ")
    if difference > (max(tolerance, 250) if arte_debate else tolerance):
        return None

    sms = r"\b(?:questions (?:par )?sms|reponses aux questions)\b"
    left, right = title_parts(episode.title), title_parts(video.title)
    if bool(re.search(sms, published_title)) != bool(re.search(sms, video_title)):
        if show == "c-dans-l-air" and air_day == ep_day and re.search(sms, published_title) \
                and episode.duration <= 520 and difference <= 12 and left == right:
            return 110
        return None
    if video.url in episode.direct:
        return 140

    if not left or not right:
        return None
    if show == "storiavoce":
        left_part, right_part = interview_part(episode.title), interview_part(video.title)
        if left_part is not None and right_part is not None and left_part != right_part:
            return None
    left_words = set(left.split()) - STOP_WORDS
    right_words = set(right.split()) - STOP_WORDS
    if left == right and len(left_words) >= 2:
        return 110
    if guest_name and air_day is not None:
        if f" {guest_name} " in f" {video_title} " and difference <= 90:
            return 95
    if show == "c-dans-l-air" and air_day == ep_day and episode.duration >= 2700 and difference <= 75 \
            and same_political_panel(episode, video):
        return 105
    if show == "c-dans-l-air" and (reference := france_reference(video.url)) \
            and reference[0] == "l-invite-de-c-dans-l-air" and 0 <= days <= 1 and difference <= 30:
        guest, separator, topic = episode.title.partition(" - ")
        name = normalized(guest)
        if separator and len(name.split()) >= 2 and f" {name} " in f" {video_title} ":
            shared = news_words(normalized(topic)) & news_words(video_title)
            if sum(len(word) >= 3 for word in shared) >= 2:
                return 108
    if show == "c-politique" and air_day is not None and 0 <= days <= 1 and episode.duration >= 2350 \
            and difference <= 75 and news_words(left) & news_words(right) and same_political_panel(episode, video):
        return 103
    if show == "28-minutes":
        arte = arte_score(episode, video, days, left, right, difference)
        if arte is not None:
            return arte
        if difference > tolerance:
            return None
    if show == "storiavoce":
        storiavoce = storio_score(left, right, days, difference)
        if storiavoce is not None:
            return storiavoce
    if show == "rivenzi" and air_day and abs(days) <= 2 and difference <= episode.duration * .02:
        # A reupload can rename the topic while explicitly giving the original live date.
        _, separator, guests = left.rpartition(" avec ")
        if separator and " et " in guests and right.endswith(" avec " + guests):
            return 105
    left_guest = first_guest(left)
    right_guest = first_guest(right)
    if left_guest and right_guest and left_guest != right_guest:
        return None
    overlap = len(left_words & right_words)
    if overlap >= 4 and SequenceMatcher(None, left, right, autojunk=False).ratio() >= .86:
        return 85
    if show not in NEWS_SHOWS and abs(days) <= 3 and difference <= max(45, episode.duration * .025) \
            and left_guest and left_guest == right_guest:
        return 90
    if show == "c-ce-soir" and ep_day - timedelta(days=1) == air_day \
            and episode.date.astimezone(PARIS).hour < 9 and episode.duration >= 3300 and difference <= 90:
        # The RSS publishes the previous evening's full edition the next morning.
        if len(left_words & (right_words | set(normalized(video.description).split()))) >= 2:
            return 70

    # YouTube can omit a guest from the title, but the name must appear in the description.
    guest = re.split(r"\bavec\b", left, maxsplit=1)
    if len(guest) == 2:
        subject, names = map(str.strip, guest)
        if len(set(subject.split()) - STOP_WORDS) >= 3 and subject == right.split(" avec ", 1)[0].strip() \
                and len(names.split()) >= 2 and f" {names} " in f" {normalized(video.description)} ":
            return 90
    return None


def choose_matches(episodes, videos, show, now, rules=None):
    rules = rules or {}
    date_window = rules.get("dateWindowDays")
    max_difference = rules.get("maxDurationDifferenceSeconds")
    minimum_score = rules.get("minimumScore", 0)
    proposals = defaultdict(list)
    by_day = defaultdict(list)
    by_title = defaultdict(list)
    for video in videos:
        by_day[video_day(video, show)].append(video)
        if show not in NEWS_SHOWS and video.expires > now.timestamp():
            by_title[title_parts(video.title)].append(video)
    offsets = range(-date_window, date_window + 1) if date_window is not None else (
        range(-5, 3) if show == "c-politique" else range(-1, 3) if show in NEWS_SHOWS else range(-14, 15))
    def duration_allowed(episode, video):
        return max_difference is None or abs(episode.duration - video.duration) <= max_difference

    for episode in episodes:
        day = cdanslair_episode_day(episode) if show == "c-dans-l-air" else episode.date.astimezone(PARIS).date()
        candidates = (video for offset in offsets for video in by_day.get(day + timedelta(days=offset), ()))
        ranked = sorted(((score, v) for v in candidates if duration_allowed(episode, v)
                         and (score := matching_score(episode, v, show, now, date_window, max_difference)) is not None),
                        key=lambda item: item[0], reverse=True)
        if ranked:
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 20:
                continue
            score, video = ranked[0]
        else:
            # Some interview feeds republish episodes with a new date. Require the
            # same distinctive title and guest, with room for a new cut of the interview.
            title = title_parts(episode.title)
            if not episode.duration or not first_guest(title) or len(set(title.split()) - STOP_WORDS) < 5:
                continue
            tolerance = episode.duration * (.15 if show == "rivenzi" else .025)
            exact = [video for video in by_title.get(title, ()) if video.duration
                     and abs(video.duration - episode.duration) <= max(12, tolerance)
                     and duration_allowed(episode, video)
                     and (date_window is None or abs((day - video_day(video, show)).days) <= date_window)]
            if len(exact) != 1:
                continue
            score, video = 110, exact[0]
        if score >= minimum_score:
            proposals[video.url].append((score, episode.id, video))
    links = {}
    for candidates in proposals.values():
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        score, episode_id, video = candidates[0]
        if len(candidates) > 1:
            other = candidates[1][0]
            if score - other < 20 and not (score >= 110 > other):
                continue
        links[episode_id] = {"url": video.url, "expiresAt": int(min(video.expires, now.timestamp() + 48 * 3600))}
    if show == "storiavoce" and minimum_score <= 105:
        supplemental = dict(links)
        add_storiavoce_synopsis_matches(episodes, videos, supplemental, now)
        episodes_by_id = {episode.id: episode for episode in episodes}
        videos_by_url = {video.url: video for video in videos}
        for identifier in supplemental.keys() - links.keys():
            match = supplemental[identifier]
            episode = episodes_by_id[identifier]
            video = videos_by_url[match["url"]]
            days = (episode.date.astimezone(PARIS).date() - video_day(video, show)).days
            if duration_allowed(episode, video) and (date_window is None or abs(days) <= date_window):
                links[identifier] = match
    return links


def youtube_catalogue(channel, api_key, episodes, now, *, history=True, previous=()):
    def request(resource, **params):
        return json.loads(fetch(YOUTUBE + resource + "?" + urlencode({**params, "key": api_key}), 3_000_000))["items"]

    channels = request("channels", part="contentDetails", id=channel)
    if len(channels) != 1 or channels[0].get("id") != channel:
        raise ValueError("Unknown YouTube channel")
    uploads = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = set()
    params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
    cutoff = min((episode.date for episode in episodes), default=now) - timedelta(days=30)
    old_pages = 0
    for _ in range(HISTORY_PAGES if history else 3):
        page = json.loads(fetch(YOUTUBE + "playlistItems?" + urlencode({**params, "key": api_key}), 3_000_000))
        items = [item["contentDetails"] for item in page["items"]]
        ids.update(item["videoId"] for item in items)
        if items and all(item.get("videoPublishedAt") and timestamp(item["videoPublishedAt"]) < cutoff for item in items):
            old_pages += 1
        else:
            old_pages = 0
        if not page.get("nextPageToken") or old_pages >= 2:
            break
        params["pageToken"] = page["nextPageToken"]
    ids.update(url.split("=")[-1] for episode in episodes for url in episode.direct
               if url.startswith("https://www.youtube.com/watch?v="))
    ids = sorted(id for id in ids if VIDEO_ID.fullmatch(id))
    result = [video for video in previous if video.expires > now.timestamp() and video.url.rsplit("=", 1)[-1] not in ids]
    for start in range(0, len(ids), 50):
        for item in request("videos", part="snippet,contentDetails,status", id=",".join(ids[start:start+50])):
            snippet = item["snippet"]
            details = item["contentDetails"]
            region = details.get("regionRestriction", {})
            if snippet["channelId"] != channel or snippet.get("liveBroadcastContent") != "none" \
                    or item["status"].get("privacyStatus") != "public" or item["status"].get("embeddable") is not True \
                    or "FR" in region.get("blocked", []) or ("allowed" in region and "FR" not in region["allowed"]) \
                    or not VIDEO_ID.fullmatch(item["id"]):
                continue
            result.append(Video("https://www.youtube.com/watch?v=" + item["id"], snippet["title"],
                                timestamp(snippet["publishedAt"]), duration(details["duration"]),
                                now.timestamp() + 48 * 3600, snippet.get("description", "")))
    return result


def direct_youtube_links(channel, handle, episodes, now):
    """An episode's own video URL can be checked with the public oEmbed endpoint."""
    links = {}
    for episode in episodes:
        if len(episode.direct) != 1:
            continue
        url = next(iter(episode.direct))
        if not url.startswith("https://www.youtube.com/watch?v="):
            continue
        query = urlencode({"url": url, "format": "json"})
        try:
            video = json.loads(fetch("https://www.youtube.com/oembed?" + query, 20_000))
            author = urlsplit(video["author_url"])
            if author.hostname in {"www.youtube.com", "youtube.com"} and (
                author.path == "/channel/" + channel or author.path.casefold() == handle
            ):
                links[episode.id] = {"url": url, "expiresAt": int(now.timestamp() + 48 * 3600)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return links


def key_at(service):
    key = os.environ.get("QUIRE_YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    try:
        return (service / "youtube-api-key").read_text().strip()
    except OSError:
        return ""


def index_path():
    return podcasts.ROOT / "video-links.json"


def load_index():
    try:
        return json.loads(index_path().read_text())
    except (OSError, ValueError):
        return {}


def endpoint():
    now = time.time()
    results = load_index()

    def valid(match):
        return match["expiresAt"] > now and official_url(match["url"]) == match["url"]

    def valid_replay(match):
        return match["expiresAt"] > now and france_reference(match["url"]) is not None

    clips = {
        identifier: [clip for clip in items if valid(clip["link"])]
        for show in results.values()
        for identifier, items in show.get("clips", {}).items()
    }
    return {
        "links": {identifier: match for show in results.values() for identifier, match in show["links"].items()
                  if valid(match)},
        "clips": {identifier: items for identifier, items in clips.items() if items},
        "franceTV": {identifier: match for key, show in results.items() if key.startswith("france-tv:")
                     for identifier, match in show["links"].items() if valid_replay(match)},
    }
