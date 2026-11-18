"""Ondier's editable video sources, episode overrides and published selections."""
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError

import feeds
import podcasts
import podcast_videos as matcher
import podcast_youtube_audio as youtube

PROVIDERS = ('franceTV', 'youtube')
DEFAULTS = json.loads(Path(__file__).with_name('video-matching-defaults.json').read_text())
PROFILES = ('standard', *dict.fromkeys(config['profile'] for config in DEFAULTS.values()))
RULES = {'dateWindowDays': 45, 'maxDurationDifferenceSeconds': 3600, 'minimumScore': 140}
EMPTY = dict(youtube='', channel='', franceCatalogues=[], providerOrder=list(PROVIDERS),
             fallback=False, profile='standard', rules={})
WAKE = threading.Event()
LOG = logging.getLogger(__name__)


def migrate():
    with podcasts.LOCK:
        state = podcasts.load()
        if 'videoMatching' not in state:
            state['videoMatching'] = {show['id']: DEFAULTS[show['url']] for show in state['subscriptions']
                                      if show['url'] in DEFAULTS}
            podcasts.save(state)


def settings(state, show):
    return state.get('videoMatching', {}).get(show['id'], DEFAULTS.get(show['url'], EMPTY))


def public_france(address, catalogue=False):
    address = podcasts.string(address, 2048).strip()
    parts = urlsplit(address)
    if parts.scheme == 'https' and parts.netloc in ('france.tv', 'www.france.tv'):
        path = parts.path
        canonical = matcher.FRANCE_ORIGIN + path
        if catalogue and matcher.FRANCE_CATALOGUE.fullmatch(path):
            return canonical.rstrip('/') + '/'
        if not catalogue and matcher.france_reference(canonical):
            return canonical
    raise ValueError('Enter a valid France.tv ' + ('programme URL.' if catalogue else 'replay URL.'))


def validated(payload, previous):
    if not isinstance(payload, dict):
        raise ValueError('Invalid video settings.')
    address = podcasts.string(payload.get('youtube', ''), 2048).strip()
    raw_catalogues = payload.get('franceCatalogues', [])
    order = payload.get('providerOrder', list(PROVIDERS))
    profile = payload.get('profile', 'standard')
    fallback = payload.get('fallback', False)
    raw_rules = payload.get('rules', {})
    if not isinstance(raw_catalogues, list) or len(raw_catalogues) > 8:
        raise ValueError('Enter up to eight France.tv programme URLs.')
    catalogues = list(dict.fromkeys(public_france(url, catalogue=True) for url in raw_catalogues))
    if order not in (list(PROVIDERS), list(reversed(PROVIDERS))) or type(fallback) is not bool or profile not in PROFILES:
        raise ValueError('Invalid video provider or matching profile.')
    if not isinstance(raw_rules, dict) or any(key not in RULES or type(value) is not int or not 0 <= value <= RULES[key]
                                             for key, value in raw_rules.items()):
        raise ValueError('Invalid matching rule limits.')
    channel = ''
    if address:
        if address == previous['youtube']:
            channel = previous['channel']
        else:
            status, result = youtube.resolve(address)
            if status != 200:
                raise ValueError(result['error'])
            channel = result['channel']
    return dict(youtube=address, channel=channel, franceCatalogues=catalogues, providerOrder=order,
                fallback=fallback, profile=profile, rules=raw_rules)


def episode_override(payload):
    if not isinstance(payload, dict) or payload.get('mode') not in ('automatic', 'hidden', 'manual'):
        raise ValueError('Choose Automatic, Hide Video or Custom Replay.')
    mode = payload['mode']
    if mode == 'automatic':
        return None
    if mode == 'hidden':
        return {'mode': mode}
    raw = podcasts.string(payload.get('url', ''), 2048).strip()
    return {'mode': mode, 'url': matcher.official_url(raw) or public_france(raw)}


def provider_key(show, config, provider):
    source = config['channel'] if provider == 'youtube' else config['franceCatalogues']
    options = (source, config['profile'], config['rules'])
    signature = sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
    return f"{provider}:{show['id']}:{signature}"


def cached_result(results, show, config, provider):
    if not (config['channel'] if provider == 'youtube' else config['franceCatalogues']):
        return {}
    key = provider_key(show, config, provider)
    item = results.get(key)
    if item is not None:
        return item
    default = DEFAULTS.get(show['url'])
    if default and key == provider_key(show, default, provider):
        old = config['profile'] if provider == 'youtube' else 'france-tv:' + sha256(show['url'].encode()).hexdigest()
        return results.get(old, {})
    return {}


def descriptor(link):
    address = link['url']
    if replay := matcher.france_reference(address):
        return {'url': address, 'label': 'France.tv', 'id': 'france-tv-' + replay[1]}
    return {'url': address, 'label': 'YouTube', 'id': address.rsplit('=', 1)[-1]}


def endpoint(state=None, *, now=None):
    if state is None:
        with podcasts.LOCK:
            state = podcasts.load()
    now = time.time() if now is None else now
    results = matcher.load_index()
    index = {'links': {}, 'franceTV': {}, 'clips': {}, 'selections': {}, 'hidden': []}
    for show in state['subscriptions']:
        config = settings(state, show)
        overrides = state.get('videoOverrides', {}).get(show['id'], {})
        indexed = {provider: cached_result(results, show, config, provider) for provider in PROVIDERS}
        links = {provider: {identifier: match for identifier, match in item.get('links', {}).items()
                            if match['expiresAt'] > now}
                 for provider, item in indexed.items()}
        for identifier in links['franceTV'].keys() | links['youtube'].keys() | overrides.keys():
            override = overrides.get(identifier, {})
            if override.get('mode') == 'hidden':
                index['hidden'].append(identifier)
                continue
            chosen = [links[provider][identifier] for provider in config['providerOrder'] if identifier in links[provider]]
            if override.get('mode') == 'manual':
                chosen = [{'url': override['url'], 'expiresAt': 253402300799}]
            else:
                chosen = chosen[:2 if config['fallback'] else 1]
            if chosen:
                index['selections'][identifier] = {'primary': chosen[0], 'fallback': chosen[1] if len(chosen) > 1 else None}
                for match in chosen:
                    provider = 'franceTV' if matcher.france_reference(match['url']) else 'links'
                    index[provider][identifier] = match
        for identifier, clips in indexed['youtube'].get('clips', {}).items():
            if overrides.get(identifier, {}).get('mode') == 'hidden':
                continue
            active = [clip for clip in clips if clip['link']['expiresAt'] > now]
            if active:
                index['clips'][identifier] = active
    return index


def save_index(results):
    podcasts.ROOT.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=podcasts.ROOT, prefix='.video-links-', delete=False, mode='w') as stream:
        path = Path(stream.name)
        try:
            json.dump(results, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
            path.replace(matcher.index_path())
        finally:
            path.unlink(missing_ok=True)


def matching_episodes(show, now):
    rss = feeds.rss_entry(show['url'], show['id'])
    raw = feeds.original(rss)
    canonical = feeds.parse(raw, rss['origin'], show['id'])
    exact = {episode.id: episode for episode in matcher.load_episodes(rss['origin'], now, show_id=show['id'], raw=raw)}
    return [
        exact.get(item['id']) or matcher.Episode(item['id'], item['title'],
            datetime.fromtimestamp(item['date'], timezone.utc), item['duration'], frozenset(), item['description'][:6000])
        for item in canonical['episodes'] if item['date'] is not None and item['date'] <= now.timestamp() + 86400
    ]


def public_youtube_catalogue(channel, now):
    address = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel
    raw = matcher.fetch(address, 1_000_000)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Invalid YouTube channel feed')
    feed = ElementTree.fromstring(raw)
    uploads = youtube.public_uploads(('id', channel))
    if uploads.get('channel_id') != channel and uploads.get('id') != channel:
        raise ValueError('Unexpected YouTube channel')
    metadata = {entry['id']: entry for entry in uploads.get('entries', []) if entry and entry.get('id')}
    videos = []
    for entry in feed.findall('{*}entry')[:20]:
        identifier = entry.findtext('{*}videoId', '')
        if entry.findtext('{*}channelId') != channel or not matcher.VIDEO_ID.fullmatch(identifier):
            continue
        item = metadata.get(identifier, {})
        length = item.get('duration')
        if (type(length) not in (int, float) or not math.isfinite(length) or not 0 < length <= 28800
                or item.get('availability') not in (None, 'public')
                or item.get('live_status') in ('is_live', 'is_upcoming')):
            continue
        try:
            published = matcher.timestamp(entry.findtext('{*}published', ''))
        except ValueError:
            continue
        videos.append(matcher.Video('https://www.youtube.com/watch?v=' + identifier, entry.findtext('{*}title', ''),
                                    published, length, now.timestamp() + 48 * 3600,
                                    entry.findtext('{*}group/{*}description', '')))
    return videos


def refresh(service, catalogues, france_pages):
    now = datetime.now(timezone.utc)
    stamp = now.timestamp()
    with podcasts.LOCK:
        state = podcasts.load()
    results = matcher.load_index()
    # Keep the original index as a fallback for migrated sources until their first successful refresh.
    active = {key: value for key, value in results.items()
              if key.count(':') < 2}
    key = matcher.key_at(service)
    for show in state['subscriptions']:
        config = settings(state, show)
        if not (config['channel'] or config['franceCatalogues']):
            continue
        for provider in PROVIDERS:
            result_key = provider_key(show, config, provider)
            if result_key in results:
                active[result_key] = results[result_key]
        try:
            episodes = matching_episodes(show, now)
        except (OSError, HTTPException, ValueError, KeyError, TypeError, OverflowError, ElementTree.ParseError) as error:
            LOG.warning('Video episode lookup: %s', type(error).__name__)
            continue
        profile, rules = config['profile'], config['rules']
        if config['franceCatalogues']:
            try:
                videos = matcher.france_replays(config['franceCatalogues'], now, france_pages)
                active[provider_key(show, config, 'franceTV')] = {
                    'links': matcher.choose_matches(episodes, videos, profile, now, rules),
                }
            except (OSError, HTTPException, ValueError, KeyError, TypeError) as error:
                LOG.warning('France.tv video lookup: %s', type(error).__name__)
        if config['channel']:
            identifier = provider_key(show, config, 'youtube')
            try:
                previous_time, previous = catalogues.get(identifier, (0, ()))
                full = stamp - previous_time >= matcher.HISTORY_INTERVAL
                public_feed = not key
                if key:
                    try:
                        videos = matcher.youtube_catalogue(config['channel'], key, episodes, now, history=full,
                                                            previous=() if full else previous)
                        catalogues[identifier] = (stamp if full else previous_time, videos)
                    except HTTPError as error:
                        if error.code not in (403, 429):
                            raise
                        public_feed = True
                if public_feed:
                    videos = public_youtube_catalogue(config['channel'], now)
                links = matcher.choose_matches(episodes, videos, profile, now, rules)
                prior = cached_result(active, show, config, 'youtube') if public_feed else {}
                retained = {episode: match for episode, match in prior.get('links', {}).items()
                            if match['expiresAt'] > stamp}
                links = retained | links
                active[identifier] = {'links': links}
                if profile == 'c-dans-l-air':
                    active[identifier]['clips'] = prior.get('clips', {}) | matcher.cdanslair_clip_links(episodes, videos, links, now)
            except (OSError, HTTPException, ValueError, KeyError, TypeError, subprocess.SubprocessError,
                    ElementTree.ParseError) as error:
                LOG.warning('YouTube video lookup: %s', type(error).__name__)
    catalogues_keys = set(catalogues) - active.keys()
    for identifier in catalogues_keys:
        catalogues.pop(identifier)
    save_index(active)


def run(service):
    catalogues, france_pages = {}, {}
    while True:
        WAKE.clear()
        started = time.monotonic()
        try:
            refresh(service, catalogues, france_pages)
        except (OSError, ValueError) as error:
            LOG.warning('Unable to update video index: %s', type(error).__name__)
        WAKE.wait(max(1, matcher.INTERVAL - (time.monotonic() - started)))
