"""Fetch public feeds, cache their original XML and produce standard client feeds."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate, parsedate_to_datetime
from functools import lru_cache
from hashlib import sha256
from html import unescape
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as XML
import gzip
import ipaddress
import json
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time

import podcasts
import podcast_videos
import podcast_youtube_audio

PODCAST = 'https://podcastindex.org/namespace/1.0'
ITUNES = 'http://www.itunes.com/dtds/podcast-1.0.dtd'
PSC = 'http://podlove.org/simple-chapters'
USER_AGENT = 'Ondier/1.0 Podcast server'
MAX_XML = 20_000_000
REFRESH_SECONDS = 30 * 60
SLOTS = threading.BoundedSemaphore(6)
LOCKS = tuple(threading.Lock() for _ in range(32))
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]')
XML.register_namespace('itunes', ITUNES)
XML.register_namespace('podcast', PODCAST)


def request_public(url, headers=None, *, max_bytes=MAX_XML):
    """Connect to the validated public address while retaining TLS host verification."""
    for _ in range(7):
        parts = urlsplit(podcasts.public_url(url))
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError('Use a public HTTP or HTTPS feed.')
        addresses = list(dict.fromkeys(record[4][0] for record in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
                                for address in addresses):
            raise ValueError('Use a publicly hosted feed.')
        cls = HTTPSConnection if parts.scheme == 'https' else HTTPConnection
        connection = cls(parts.hostname, port, timeout=10)
        address = addresses[0]
        connection._create_connection = lambda _, timeout, source: socket.create_connection((address, port), timeout, source)
        try:
            path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
            if parts.query:
                path += '?' + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~[]")
            connection.request('GET', path, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity', **(headers or {})})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader('Location'):
                url = urljoin(url, response.getheader('Location'))
                continue
            if response.status not in (200, 304):
                raise OSError(f'The publisher returned HTTP {response.status}.')
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError('This feed exceeds the size limit.')
            return url, response.status, dict(response.getheaders()), data
        finally:
            connection.close()
    raise ValueError('The publisher returned too many redirects.')


def localname(tag):
    return tag.rsplit('}', 1)[-1].casefold()


def nodes(parent, name):
    return [node for node in parent if localname(node.tag) == name]


def text(parent, name):
    return next((''.join(node.itertext()).strip() for node in nodes(parent, name)), '')


def clean(value):
    for _ in range(3):
        value = unescape(value)
    return CONTROL.sub('', re.sub(r'<[^>]*>', ' ', value)).strip()


def link(value, base):
    try:
        return podcasts.public_url(urljoin(base, value)) if value else None
    except ValueError:
        return None


def parse(raw, origin, identifier):
    # This rejects external/general entity declarations before ElementTree reads the document.
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw.replace(b'\0', b''), re.I):
        raise ValueError('Unsupported podcast XML.')
    root = XML.fromstring(raw)
    channel = next(iter(nodes(root, 'channel')), root)
    if localname(channel.tag) not in ('channel', 'feed'):
        raise ValueError('This URL does not return a podcast feed.')
    radio = (urlsplit(origin).hostname or '').endswith(('radiofrance.fr', 'radiofrance-podcast.net'))
    provider_image = channel.find(f'{{{ITUNES}}}image')
    generic_image = next(iter(nodes(channel, 'image')), None)
    cover = provider_image.get('href') if provider_image is not None else None
    cover = cover or (text(generic_image, 'url') if generic_image is not None else '')
    data = {'title': clean(text(channel, 'title')), 'publisher': clean(text(channel, 'author')),
            'description': clean(text(channel, 'description') or text(channel, 'subtitle')),
            'artwork': link(cover, origin), 'episodes': [],
            'categories': sorted({value for top in nodes(channel, 'category')
                                  for node in top.iter()
                                  if (value := clean(node.get('text') or node.text or '')[:150])})}
    hosts = {}
    for person in channel.findall(f'{{{PODCAST}}}person'):
        if person.get('group', 'cast').lower() != 'cast' or person.get('role', 'host').lower() not in ('host', 'co-host'):
            continue
        name = clean(''.join(person.itertext()))[:160]
        if name:
            hosts.setdefault(name.casefold(), {'name': name, 'portrait': link(person.get('img'), origin)})
    if hosts:
        data['hosts'] = list(hosts.values())[:12]
    images = [(image, link(image.get('href'), origin)) for image in channel.findall(f'{{{PODCAST}}}image')
              if image.get('type', 'image/').lower().startswith('image/')]
    banners = [(int(width) if width.isascii() and width.isdigit() and len(width) <= 8 else 0, address)
               for image, address in images if address and 'banner' in image.get('purpose', '').lower().split()
               for width in [image.get('width', '')]]
    if banners:
        data['banner'] = max(banners)[1]
    if not data['artwork']:
        data['artwork'] = next((address for image, address in images if address and
                               (not image.get('purpose') or 'artwork' in image.get('purpose', '').lower().split())), None)
    if radio:
        page = text(channel, 'link')
        data['radioFranceArchive'] = (page if (urlsplit(page).hostname or '') in {'radiofrance.fr','www.radiofrance.fr'}
                                      else 'https://www.radiofrance.fr/podcasts')
    seen = set()
    for item in (nodes(channel, 'item') or nodes(channel, 'entry'))[:1500]:
        enclosure = next(iter(nodes(item, 'enclosure')), None)
        if enclosure is None:
            enclosure = next((n for n in nodes(item, 'link') if n.get('rel') == 'enclosure'), None)
        if enclosure is None:
            continue
        address = link(enclosure.get('url') or enclosure.get('href'), origin)
        title = clean(text(item, 'title'))
        if not address or not title or (radio and podcast_videos.normalized(title).startswith('retrouvez tous les episodes')):
            continue
        guid = text(item, 'guid') or text(item, 'id') or address
        episode_id = sha256(f'{identifier}\n{guid}'.encode()).hexdigest()
        if episode_id in seen:
            continue
        seen.add(episode_id)
        raw_date = text(item, 'pubdate') or text(item, 'published')
        try:
            date = parsedate_to_datetime(raw_date).timestamp()
        except (ValueError, TypeError, OverflowError):
            date = None
        source_type = enclosure.get('type') or 'audio/mpeg'
        size = enclosure.get('length', '')
        transcript_types = ('application/json', 'text/vtt', 'application/x-subrip', 'text/plain', 'text/html')
        transcripts = [{'url': url, 'type': node.get('type')}
                       for node in item.findall(f'{{{PODCAST}}}transcript')
                       if node.get('type') in transcript_types and (url := link(node.get('url'), origin))]
        chapters = item.find(f'{{{PODCAST}}}chapters')
        inline = []
        for chapter in item.findall(f'{{{PSC}}}chapters/{{{PSC}}}chapter'):
            start = chapter.get('start', '')
            if re.fullmatch(r'(?:\d{1,5}:){0,2}\d{1,5}(?:\.\d{1,6})?', start) and (label := clean(chapter.get('title', ''))):
                seconds = sum(float(value) * 60**index for index, value in enumerate(reversed(start.split(':'))))
                inline.append({'startTime': seconds, 'title': label[:400]})
        data['episodes'].append({'id': episode_id, 'guid': guid, 'title': title, 'date': date,
                                 'duration': podcast_videos.duration(text(item, 'duration')), 'enclosure': address,
                                 'transcripts': transcripts[:5], 'chapters': inline[:1000],
                                 'chapterURL': link(chapters.get('url'), origin) if chapters is not None else None,
                                 'length': size if size.isascii() and size.isdigit() else '0', 'mimeType': source_type,
                                 'description': clean(text(item, 'encoded') or text(item, 'description') or text(item, 'summary'))})
    return data


def rss_entry(url, identifier, force=False):
    if not podcasts.ID.fullmatch(identifier):
        raise ValueError('Invalid show ID.')
    path = podcasts.ROOT / 'rss' / (identifier + '.json.gz')
    with LOCKS[int(identifier[:4], 16) % len(LOCKS)]:
        try:
            with gzip.open(path, 'rt') as source:
                cached = json.load(source)
        except FileNotFoundError:
            cached = None
        if cached and cached['url'] == url and not force and time.time() - cached['checkedAt'] < REFRESH_SECONDS:
            return cached
        if not SLOTS.acquire(timeout=6):
            if cached and cached['url'] == url:
                return cached
            raise OSError('The feed server is busy.')
        try:
            conditional = {}
            if cached and cached['url'] == url:
                if cached['etag']:
                    conditional['If-None-Match'] = cached['etag']
                if cached['modified']:
                    conditional['If-Modified-Since'] = cached['modified']
            origin, status, headers, raw = request_public(url, conditional)
            if status == 304 and cached:
                entry = cached
            else:
                parse(raw, origin, identifier)
                from base64 import b64encode
                response_headers = {key.casefold(): value for key, value in headers.items()}
                entry = {'url': url, 'origin': origin, 'etag': response_headers.get('etag'),
                         'modified': response_headers.get('last-modified'), 'raw': b64encode(raw).decode()}
            entry['checkedAt'] = time.time()
            path.parent.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.rss-', delete=False) as f:
                temporary = Path(f.name)
            try:
                with gzip.open(temporary, 'wt', compresslevel=4) as output:
                    json.dump(entry, output, separators=(',', ':'))
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return entry
        except (ValueError, OSError, HTTPException, XML.ParseError):
            if cached and cached['url'] == url:
                return cached
            raise
        finally:
            SLOTS.release()


def original(entry):
    from base64 import b64decode
    return b64decode(entry['raw'])


def channel_id(url):
    result = podcast_youtube_audio.channel_lookup(url)
    return result[1] if result and result[0] == 'id' else None


def show_feed(show, force=False):
    channel = channel_id(show['url'])
    if channel:
        status, feed = podcast_youtube_audio.endpoint(channel)
        if status != 200:
            raise OSError(feed['error'])
        return feed
    entry = rss_entry(show['url'], show['id'], force)
    return parse(original(entry), entry['origin'], show['id'])


def apple_search(query):
    if not query or len(query) > 200:
        raise ValueError('Enter a podcast title up to 200 characters.')
    address = 'https://itunes.apple.com/search?' + urlencode(dict(term=query, country='fr', media='podcast', entity='podcast', limit=25))
    result = json.loads(podcast_videos.fetch(address, 3_000_000))
    return {'results': [item for item in result.get('results', []) if item.get('feedUrl')]}


def refresh_loop(archive, indexed):
    while True:
        with podcasts.LOCK:
            shows = list(podcasts.load()['subscriptions'])
        def update(show):
            try:
                archive.record(show, show_feed(show))
            except (OSError, ValueError, HTTPException, XML.ParseError, sqlite3.Error):
                pass
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(update, shows))
        try:
            archive.fill_transcripts([show['id'] for show in shows])
        except sqlite3.Error:
            pass
        indexed.set()
        time.sleep(REFRESH_SECONDS)


def syndicated(show, feed, feed_url, media_url, *, duration_filter=True):
    rss = XML.Element('rss', version='2.0')
    channel = XML.SubElement(rss, 'channel')
    XML.SubElement(channel, 'title').text = clean(feed.get('title') or show['title'])
    XML.SubElement(channel, 'link').text = feed_url
    XML.SubElement(channel, 'description').text = clean(feed.get('description') or show['title'])
    XML.SubElement(channel, f'{{{ITUNES}}}author').text = clean(feed.get('publisher') or show['publisher'])
    if cover := feed.get('artwork') or show.get('artwork'):
        XML.SubElement(channel, f'{{{ITUNES}}}image', href=cover)
    minimum = minimum_minutes(show) * 60 if duration_filter else 0
    for item in feed['episodes']:
        if minimum and (item.get('duration') or 0) < minimum:
            continue
        node = XML.SubElement(channel, 'item')
        XML.SubElement(node, 'title').text = clean(item['title'])
        XML.SubElement(node, 'guid', isPermaLink='false').text = item['id']
        if item.get('date'):
            XML.SubElement(node, 'pubDate').text = formatdate(item['date'], usegmt=True)
        if item.get('duration'):
            XML.SubElement(node, f'{{{ITUNES}}}duration').text = str(int(item['duration']))
        enclosure = media_url(item) if item.get('youtubeAudioID') else item['enclosure']
        XML.SubElement(node, 'enclosure', url=enclosure, length=item.get('length', '0'),
                       type=item.get('mimeType') or 'audio/mpeg')
        if item.get('description'):
            XML.SubElement(node, 'description').text = clean(item['description'])
        for transcript in item.get('transcripts') or []:
            XML.SubElement(node, f'{{{PODCAST}}}transcript', url=transcript['url'], type=transcript['type'])
        if item.get('chapterURL'):
            XML.SubElement(node, f'{{{PODCAST}}}chapters', url=item['chapterURL'], type='application/json')
    return XML.tostring(rss, encoding='utf-8', xml_declaration=True)


def minimum_minutes(show):
    if 'minimumEpisodeMinutesOverride' in show:
        return show['minimumEpisodeMinutesOverride']
    return {
        'la grande librairie': 50, 'avec philosophie': 30, 'la science cqfd': 30,
        'allons y voir': 30, 'plan large': 30,
    }.get(podcast_videos.normalized(show['title']), 0)
