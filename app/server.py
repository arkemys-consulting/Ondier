"""Ondier: private podcast catalogue, player, syndication and playback sync."""
from base64 import b64decode
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.etree import ElementTree as XML
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time

from accounts import Accounts
from archive import Archive, PLAYLISTS, parse_opml, playlist_payload
import backups
import feeds
import listening
import podcasts
import podcast_streams
import podcast_videos
import podcast_youtube_audio as youtube
import video_matching

WEB = Path(__file__).parent / 'web'
VERSION = '1.2.0'
EDITORIAL = Path(__file__).parent / 'editorial'
ORIGIN = os.environ['ONDIER_ORIGIN'].rstrip('/')
OWNER = os.environ.get('ONDIER_OWNER', '').casefold()
KEY = os.environ['ONDIER_ACCESS_KEY']


def secret_file(name):
    return Path(path).read_text().strip() if (path := os.environ.get(name)) else ''


TOKEN = secret_file('ONDIER_CONTENTS_TOKEN_FILE')
SETUP_KEY = secret_file('ONDIER_SETUP_KEY_FILE')
origin_parts = urlsplit(ORIGIN)
local_http = origin_parts.scheme == 'http' and origin_parts.hostname in ('localhost', '127.0.0.1', '::1')
if (not (origin_parts.scheme == 'https' or local_http) or not origin_parts.hostname or
        origin_parts.username is not None or origin_parts.password is not None or
        origin_parts.path or origin_parts.query or origin_parts.fragment):
    raise RuntimeError('Configure an HTTPS origin or a loopback HTTP origin.')
COOKIE_SECURE = '' if local_http else ' Secure;'
SIMPLE_SYNC = '/index.php/apps/gpoddersync/'
FEED = re.compile(r'/rss/([0-9a-f]{64})/([0-9a-f]{40})\.xml')
MEDIA = re.compile(r'/media/([0-9a-f]{64})/([0-9a-f]{40})/([A-Za-z0-9_-]{11})\.m4a')
LOGIN = re.compile(r'/connect/([A-Za-z0-9_-]{32})')
LOGIN_LOCK = threading.Lock()
IMPORT_LOCK = threading.Lock()
IMPORT_JOBS = {}
LIST_FEED = re.compile(r'/lists/([0-9a-f]{32})/([0-9a-f]{40})\.xml')
LOGINS = {}
ACCOUNT_COOKIE = "ondier_session"
if len(KEY) < 32 or (TOKEN and len(TOKEN) < 32) or (SETUP_KEY and len(SETUP_KEY) < 32):
    raise RuntimeError('Configure strong Ondier secrets.')
if not (OWNER or SETUP_KEY):
    raise RuntimeError('Configure an initial account setup key or a Tailscale owner.')


def current():
    with podcasts.LOCK:
        return podcasts.load()


def presentation():
    path = EDITORIAL / 'catalogue.json'
    if not path.is_file():
        path = Path(__file__).resolve().parents[1] / 'Quire/EditorialArtwork/catalogue.json'
    if not path.is_file():
        return {}
    catalogue = json.loads(path.read_bytes())
    banners = catalogue.get('podcasts', {})
    hosts = catalogue.get('podcastHosts', {})
    return {
        key: {
            'banner': f'{ORIGIN}/editorial/{banners[key]}.jpg' if key in banners else None,
            'hosts': [{
                'name': host['name'],
                'portrait': (f"{ORIGIN}/editorial/{host['portraitAsset']}.jpg"
                             if host.get('portraitAsset') else host.get('portrait'))
            } for host in hosts.get(key, [])],
        }
        for key in banners.keys() | hosts.keys()
    }


PRESENTATION = presentation()


def presented(feed, identifier):
    editorial = PRESENTATION.get(identifier, {})
    return {**feed, 'banner': editorial.get('banner') or feed.get('banner'),
            'hosts': feed.get('hosts') or editorial.get('hosts') or []}


def podcast_id(url):
    return sha256(url.encode()).hexdigest()


def find(identifier, state=None):
    return next((s for s in (state or current())['subscriptions'] if s['id'] == identifier), None)


def capability(identifier):
    return hmac.new(KEY.encode(), ('feed:' + identifier).encode(), 'sha256').hexdigest()[:40]


def sync_password():
    return hmac.new(KEY.encode(), b'gpodder-password', 'sha256').hexdigest()


def sync_cookie():
    return hmac.new(KEY.encode(), b'gpodder-session', 'sha256').hexdigest()


def feed_url(show):
    identifier = show['id']
    return f'{ORIGIN}/rss/{identifier}/{capability(identifier)}.xml'


def media_url(show, episode):
    identifier = show['id']
    return f'{ORIGIN}/media/{identifier}/{capability(identifier)}/{episode["youtubeAudioID"]}.m4a'


def opml(shows):
    root = XML.Element('opml', version='2.0')
    XML.SubElement(XML.SubElement(root, 'head'), 'title').text = 'Ondier'
    body = XML.SubElement(root, 'body')
    for show in shows:
        XML.SubElement(body, 'outline', type='rss', text=show['title'], title=show['title'], xmlUrl=feed_url(show))
    return XML.tostring(root, encoding='utf-8', xml_declaration=True)


def save_subscription(show, operation='upsert'):
    status, data = podcasts.endpoint('subscriptions', {'id': show['id'], 'operation': operation,
                                                        'updatedAt': time.time(), 'subscription': show})
    if status != 200:
        raise ValueError(data['error'])


def follow(url, as_youtube=False):
    url = podcasts.public_url(url.strip())
    if as_youtube:
        status, result = youtube.resolve(url)
        if status != 200:
            raise ValueError(result['error'])
        url = f'https://www.youtube.com/channel/{result["channel"]}/videos'
    identifier = podcast_id(url)
    if show := find(identifier):
        return show
    feed = load_feed({'id': identifier, 'url': url})
    if not feed.get('episodes'):
        raise ValueError('This feed has no playable episodes.')
    show = {'id': identifier, 'url': url, 'title': (feed.get('title') or urlsplit(url).hostname)[:400],
            'publisher': (feed.get('publisher') or '')[:400], 'artwork': feed.get('artwork'), 'notify': False}
    save_subscription(show)
    return show


def load_feed(show, force=False):
    feed = feeds.show_feed(show, force)
    ARCHIVE.record(show, feed)
    return feed


def list_url(identifier):
    return f'{ORIGIN}/lists/{identifier}/{capability("list:" + identifier)}.xml'


def listed(state, identifier):
    if not PLAYLISTS.fullmatch(identifier):
        raise LookupError
    try:
        return state.get('playlists', {})[identifier]
    except KeyError:
        raise LookupError from None


def row(show, episode, state, *, links=None, excerpt=None):
    data = {**episode, 'description': (episode.get('description') or '')[:1800]}
    data['audio'] = media_url(show, episode) if episode.get('youtubeAudioID') else episode['enclosure']
    if links is not None:
        data['video'] = available_video(episode, links)
        data['clips'] = links.get('clips', {}).get(episode['id'], [])
    return {'show': show, 'episode': data, 'progress': state['progress'].get(episode['id']), 'excerpt': excerpt}


def pages(state, query, *, native=False):
    mode = 'search' if native else query.get('view', 'search')
    term = query.get('q', '').strip()
    offset = int(query.get('offset', '0'))
    limit = int(query.get('limit', '30'))
    if mode not in ('search', 'latest', 'history', 'list') or len(term) > 200 or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 60:
        raise ValueError('Invalid episode search.')
    listing = listed(state, query.get('list', '')) if mode == 'list' else None
    if listing and listing['kind'] == 'smart':
        term = ' '.join(part for part in (listing['query'], term) if part)
    items, more = ARCHIVE.page(state, mode=mode, query=term, listing=listing, offset=offset, limit=limit)
    links = None if native else video_matching.endpoint(state)
    return {'episodes': [row(show, ep, state, links=links, excerpt=excerpt) for show, ep, excerpt in items],
            'hasMore': more, 'offset': offset + len(items)}


def list_rss(state, identifier):
    listing = listed(state, identifier)
    rows, _ = ARCHIVE.page(state, mode='list', query=listing['query'] if listing['kind'] == 'smart' else '',
                           listing=listing, limit=250)
    # Different shows use different media URLs. Preserve each episode's native GUID and enclosure.
    url = list_url(identifier)
    feed = {'episodes': [{**ep, 'enclosure': media_url(show, ep) if ep.get('youtubeAudioID') else ep['enclosure']}
                         for show, ep, _ in rows]}
    show = {'title': listing['name'], 'publisher': 'Ondier'}
    return feeds.syndicated(show, feed, url, lambda episode: episode['enclosure'], duration_filter=False)


def preview_opml(value):
    if not isinstance(value, str):
        raise ValueError('Choose an OPML file.')
    state = current()
    existing = {address for show in state['subscriptions'] for address in (show['url'], feed_url(show))}
    return [dict(item, duplicate=item['url'] in existing) for item in parse_opml(value.encode())]


def start_import(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 500:
        raise ValueError('Select at least one feed.')
    urls = list(dict.fromkeys(podcasts.public_url(value) for value in values))
    state = current()
    existing = {address for show in state['subscriptions'] for address in (show['url'], feed_url(show))}
    identifier = secrets.token_hex(16)
    job = {'total': len(urls), 'done': 0, 'added': 0, 'duplicates': 0, 'failed': []}
    with IMPORT_LOCK:
        if any(record['done'] < record['total'] for record in IMPORT_JOBS.values()):
            return 409, {'error': 'An OPML import is already running.'}
        IMPORT_JOBS.clear()
        IMPORT_JOBS[identifier] = job
    def run():
        for url in urls:
            try:
                if url in existing or find(podcast_id(url)):
                    result = 'duplicates'
                else:
                    youtube_link = youtube.channel_lookup(url) is not None
                    show = follow(url, as_youtube=youtube_link)
                    result = 'added' if show['url'] not in existing else 'duplicates'
                    existing.add(show['url'])
            except (OSError, ValueError, HTTPException, XML.ParseError, sqlite3.Error) as error:
                with IMPORT_LOCK:
                    job['failed'].append({'url': url, 'reason': str(error)[:300]})
                    job['done'] += 1
                continue
            with IMPORT_LOCK:
                job[result] += 1
                job['done'] += 1
    threading.Thread(target=run, daemon=True, name='ondier-opml').start()
    return 202, {'id': identifier}


def available_video(episode, links):
    key = episode['id']
    if key in links['hidden']:
        return None
    if selected := links['selections'].get(key):
        return video_matching.descriptor(selected['primary'])
    if episode.get('youtubeAudioID'):
        return {'url': episode['enclosure'], 'label': 'YouTube', 'id': episode['youtubeAudioID']}
    return None


def clip_id(clip):
    return parse_qs(urlsplit(clip.get('link', {}).get('url', '')).query).get('v', [''])[0]


def episode_list(show, force=False):
    data = load_feed(show, force)
    index = video_matching.endpoint()
    minimum = feeds.minimum_minutes(show)
    if minimum:
        data['episodes'] = [episode for episode in data['episodes'] if episode.get('duration', 0) >= minimum * 60]
    data['minimumEpisodeMinutes'] = minimum
    for episode in data['episodes']:
        episode['video'] = available_video(episode, index)
        episode['clips'] = index.get('clips', {}).get(episode['id'], [])
        if episode.get('youtubeAudioID'):
            episode['audio'] = media_url(show, episode)
        else:
            episode['audio'] = episode['enclosure']
    return presented(data, show['id'])


def listening_state(state, section, ids_only=False):
    if section == 'queue':
        ids = state.get('queue', [])
        stamps = {}
    else:
        stamps = {key: value for key, value in state.get('savedEpisodes', {}).items() if value is not None}
        ids = sorted(stamps, key=stamps.get, reverse=True)
    if ids_only:
        return {'ids': ids}
    shows = {show['id']: show for show in state['subscriptions']}
    entries = {ep['id']: (shows[show_id], ep) for show_id, ep in ARCHIVE.locate(ids) if show_id in shows}
    links = video_matching.endpoint(state)
    return {'ids': ids, 'episodes': [
        {**row(*entries[key], state, links=links), 'savedAt': stamps.get(key)}
        for key in ids if key in entries
    ]}


def sync_show(address, shows):
    return next((s for s in shows if address in (s['url'], feed_url(s))), None)


def sync_episode(action, show, cache):
    guid, address = action.get('guid'), action.get('episode')
    if episode := ARCHIVE.match(show['id'], guid, address):
        return episode
    if show['id'] not in cache:
        cache[show['id']] = load_feed(show)['episodes']
    return next((ep for ep in cache[show['id']] if (guid and guid in (ep['id'], ep.get('guid')))
                 or address == ep['enclosure'] or (ep.get('youtubeAudioID') and address == media_url(show, ep))), None)


def episode_positions(state, accept):
    pending = {ep: progress for ep, progress in state['progress'].items() if accept(progress)}
    if not pending:
        return
    shows = {show['id']: show for show in state['subscriptions']}
    for show_id, episode in ARCHIVE.locate(pending):
        if show_id in shows:
            yield shows[show_id], episode, pending.pop(episode['id'])
    if not pending:
        return
    for show in shows.values():
        try:
            for ep in load_feed(show)['episodes']:
                progress = pending.pop(ep['id'], None)
                if progress:
                    yield show, ep, progress
        except (ValueError, OSError, HTTPException, XML.ParseError, sqlite3.Error):
            pass
        if not pending:
            break


def sync_actions(since, state):
    actions = []
    for show, ep, progress in episode_positions(state, lambda value: value['updatedAt'] >= since):
        total = round(ep.get('duration') or 0)
        position = total if progress['finished'] and total else round(progress['position'])
        actions.append({'podcast': feed_url(show), 'episode': media_url(show, ep) if ep.get('youtubeAudioID') else ep['enclosure'],
                        'guid': ep['id'], 'action': 'play', 'started': 0, 'position': position,
                        'total': max(total, position + (0 if progress['finished'] else 1)),
                        'timestamp': datetime.fromtimestamp(progress['updatedAt'], timezone.utc).isoformat(timespec='seconds').replace('+00:00', '')})
    return actions


def continue_episodes():
    entries = [{'show':show, 'episode':{**ep, 'audio':media_url(show,ep) if ep.get('youtubeAudioID') else ep['enclosure']},
                'progress':progress}
               for show, ep, progress in episode_positions(current(), lambda value: value['position'] > 0 and not value['finished'])
               if ep.get('duration', 0) >= 60 * feeds.minimum_minutes(show)]
    return sorted(entries, key=lambda row: row['progress']['updatedAt'], reverse=True)[:12]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_body(self, status, data, content_type, *, head=False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'private, no-store')
        if cookie := getattr(self, 'account_cookie_header', None):
            self.send_header('Set-Cookie', cookie)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https: data:; media-src 'self' https: blob:; connect-src 'self' https://cloudreplay.ftven.fr; frame-ancestors 'none'; style-src 'self'; script-src 'self'")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def reply(self, status, data):
        return self.send_body(status, json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8', head=self.command == 'HEAD')

    def bridge(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer ' + TOKEN)

    def session_token(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', '')).get(ACCOUNT_COOKIE)
            return cookie.value if cookie else ''
        except CookieError:
            return ''

    def session_account(self):
        return ACCOUNTS.authenticate(self.session_token())

    def api_account(self):
        header = self.headers.get('Authorization', '')
        return ACCOUNTS.authenticate(header[7:], api=True) if header.startswith('Bearer ') else None

    def set_account_cookie(self, value='', max_age=0):
        self.account_cookie_header = f'{ACCOUNT_COOKIE}={value}; Path=/;{COOKIE_SECURE} HttpOnly; SameSite=Strict; Max-Age={max_age}'

    def account_api(self, path):
        method = self.command
        account = self.session_account()
        if path == '/api/account' and method == 'GET':
            if account:
                return self.reply(200, {'account': account, 'setup': False})
            setup = (bool(SETUP_KEY) or self.owner()) and not ACCOUNTS.initialized()
            return self.reply(200, {'account': None, 'setup': setup, 'requireSetupKey': setup and bool(SETUP_KEY)})
        if method != 'GET' and self.headers.get('Origin') != ORIGIN:
            return self.reply(403, {'error': 'Invalid browser origin.'})
        if path in ('/api/account/setup', '/api/account/login') and method == 'POST':
            body = self.read_json()
            if not isinstance(body, dict):
                raise ValueError('Invalid login request.')
            username, password = body.get('username'), body.get('password')
            if path.endswith('/setup'):
                # The setup key is only accepted until the first account exists.
                if ACCOUNTS.initialized():
                    return self.reply(409, {'error': 'The account already exists. Reload to sign in.'})
                if SETUP_KEY:
                    supplied = body.get('setupKey')
                    if not isinstance(supplied, str) or not hmac.compare_digest(supplied.strip().encode(), SETUP_KEY.encode()):
                        return self.reply(403, {'error': 'Enter the setup key generated for this Ondier installation.'})
                elif not self.owner():
                    return self.reply(403, {'error': 'Open Ondier through its private Tailscale address to create the account.'})
                result = ACCOUNTS.create(username, password)
                if result is None:
                    return self.reply(409, {'error': 'The account already exists. Reload to sign in.'})
            else:
                status, result = ACCOUNTS.login(username, password)
                if status == 429:
                    return self.reply(429, {'error': 'Too many attempts. Try again in a few minutes.'})
                if status == 401:
                    return self.reply(401, {'error': 'Incorrect username or password.'})
            account, token = result
            self.set_account_cookie(token, ACCOUNTS.session_ttl)
            return self.reply(200, {'account': account, 'setup': False})
        if not account:
            return self.reply(401, {'error': 'Sign in to Ondier.'})
        if path == '/api/account/logout' and method == 'POST':
            ACCOUNTS.logout(self.session_token())
            self.set_account_cookie()
            return self.reply(200, {'ok': True})
        if path == '/api/account/keys':
            if method == 'GET':
                return self.reply(200, {'keys': ACCOUNTS.keys()})
            if method == 'POST':
                body = self.read_json()
                if not isinstance(body, dict):
                    raise ValueError('Invalid API key request.')
                return self.reply(201, ACCOUNTS.new_key(body.get('name'), body.get('days')))
        if method == 'DELETE' and re.fullmatch(r'/api/account/keys/[0-9a-f]{32}', path):
            if ACCOUNTS.revoke(path.rsplit('/', 1)[-1]):
                return self.reply(200, {'ok': True})
        raise LookupError

    def owner(self):
        return bool(OWNER) and hmac.compare_digest(
            self.headers.get('Tailscale-User-Login', '').casefold().encode(), OWNER.encode())

    def sync_auth(self):
        try:
            header = self.headers.get('Authorization', '')
            if header.startswith('Basic '):
                return hmac.compare_digest(b64decode(header[6:], validate=True), ('ondier:' + sync_password()).encode())
            cookie = SimpleCookie(self.headers.get('Cookie', '')).get('ondier_sync')
            return cookie is not None and hmac.compare_digest(cookie.value, sync_cookie())
        except (ValueError, CookieError):
            return False

    def read_json(self, limit=262_144):
        length = int(self.headers.get('Content-Length', 0))
        if not 0 < length <= limit or self.headers.get('Transfer-Encoding'):
            raise ValueError('Invalid request size.')
        return json.loads(self.rfile.read(length))

    def serve(self):
        if len(self.path) > 4096:
            return self.reply(414, {'error':'Request is too long.'})
        target = urlsplit(self.path)
        path = target.path
        if path == '/health':
            return self.reply(200, {'service':'Ondier','status':'ok','version':VERSION})
        try:
            query = {k: v[0] for k, v in parse_qs(target.query, max_num_fields=12).items()}
            if path in ('/index.php/login/v2', '/index.php/login/v2/poll', '/login/v2', '/login/v2/poll') or LOGIN.fullmatch(path):
                return self.nextcloud_login(path)
            if self.command in ('GET', 'HEAD'):
                if re.fullmatch(r'/editorial/(?:podcast-\d+|host-[a-z0-9-]+)\.jpg', path):
                    asset = EDITORIAL / path.rsplit('/', 1)[-1]
                    if not asset.is_file():
                        raise LookupError
                    return self.send_body(200, asset.read_bytes(), 'image/jpeg', head=self.command == 'HEAD')
                if path.startswith(podcast_streams.PREFIX):
                    return podcast_streams.send_resource(self, path, head=self.command == 'HEAD')
                if match := LIST_FEED.fullmatch(path):
                    identifier, signature = match.groups()
                    if not hmac.compare_digest(capability('list:' + identifier), signature):
                        raise LookupError
                    return self.send_body(200, list_rss(current(), identifier), 'application/rss+xml; charset=utf-8',
                                          head=self.command == 'HEAD')
                if match := FEED.fullmatch(path):
                    show = self.feed_show(*match.groups())
                    data = feeds.syndicated(show, load_feed(show), feed_url(show), lambda ep: media_url(show, ep))
                    return self.send_body(200, data, 'application/rss+xml; charset=utf-8', head=self.command == 'HEAD')
                if match := MEDIA.fullmatch(path):
                    identifier, key, video = match.groups()
                    show = self.feed_show(identifier, key)
                    known = ARCHIVE.match(show['id'], '', path)
                    status, data = self.channel_stream(feeds.channel_id(show['url']), video, False,
                                                       archived=bool(known and known.get('youtubeAudioID') == video))
                    if status != 200:
                        return self.reply(status, data)
                    return podcast_streams.send_resource(self, data['path'], head=self.command == 'HEAD')
                if path in ('/', '/app.js', '/style.css', '/logo.png', '/hls.js'):
                    file = WEB / (path[1:] or 'index.html')
                    return self.send_body(200, file.read_bytes(), mimetypes.guess_type(file)[0], head=self.command == 'HEAD')
            if path.startswith('/api/2/') or path.startswith(SIMPLE_SYNC):
                return self.gpodder(path, query)
            if path == '/api/client' and self.command == 'GET':
                account = self.api_account()
                return self.reply(200, {'service': 'Ondier', **account}) if account else self.reply(
                    401, {'error': 'A valid Ondier API key is required.'})
            if path.startswith('/podcasts/') or path == '/documentaries/video-stream':
                if not (self.bridge() or self.api_account()):
                    return self.reply(401, {'error':'An access token is required.'})
                return self.native(path, query)
            if path == '/api/account' or path.startswith('/api/account/'):
                return self.account_api(path)
            if self.api_account():
                if path == '/api/sync-settings':
                    return self.reply(403, {'error': 'Open Sync setup in the Ondier web app.'})
            else:
                if not self.session_account():
                    return self.reply(401, {'error': 'Sign in to Ondier.'})
                if self.command in ('POST', 'PUT', 'DELETE') and self.headers.get('Origin') != ORIGIN:
                    return self.reply(403, {'error': 'Invalid browser origin.'})
            return self.web_api(path, query)
        except LookupError:
            return self.reply(404, {'error':'Not found.'})
        except (ValueError, TypeError, XML.ParseError) as error:
            return self.reply(400, {'error':str(error) or 'Invalid request.'})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, HTTPException, sqlite3.Error):
            return self.reply(503, {'error':'The publisher or podcast storage is temporarily unavailable.'})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = serve

    def feed_show(self, identifier, key):
        show = find(identifier) if hmac.compare_digest(capability(identifier), key) else None
        if show is None:
            raise LookupError
        return show

    def channel_stream(self, channel, video, want_video=False, hls=False, archived=False, desktop=False):
        if not channel or not podcast_videos.VIDEO_ID.fullmatch(video):
            return 400, {'error':'Invalid channel or video.'}
        if not archived:
            status, data = youtube.endpoint(channel)
            if status != 200:
                return status, data
            if not any(ep['youtubeAudioID'] == video for ep in data['episodes']):
                return 404, {'error':'The video is not in this channel.'}
        if want_video:
            return podcast_streams.open_youtube_video(video, desktop=desktop)
        return podcast_streams.open_audio_stream(video, channel, hls=hls)

    def native(self, path, query):
        if path in ('/podcasts/subscriptions', '/podcasts/progress') and self.command in ('GET', 'PUT'):
            return self.reply(*podcasts.endpoint(path.rsplit('/', 1)[-1], self.read_json() if self.command == 'PUT' else None))
        if self.command != 'GET':
            raise LookupError
        if path == '/podcasts/episode-search':
            return self.reply(200, pages(current(), query, native=True))
        if path == '/podcasts/feed':
            show = find(query.get('id'))
            if not show:
                raise LookupError
            return self.reply(200, presented(load_feed(show), show['id']))
        if path == '/podcasts/apple-search':
            return self.reply(200, feeds.apple_search(query.get('q', '').strip()))
        if path == '/podcasts/rss':
            url = podcasts.public_url(query.get('url'))
            entry = feeds.rss_entry(url, podcast_id(url))
            return self.reply(200, {'origin':entry['origin'], 'rss':entry['raw']})
        if path == '/podcasts/youtube-search':
            return self.reply(*youtube.search_channels(query.get('q', '')))
        if path == '/podcasts/youtube-channel':
            return self.reply(*youtube.resolve(query.get('url', '')))
        if path == '/podcasts/youtube-feed':
            return self.reply(*youtube.endpoint(query.get('channel', '')))
        if path in ('/podcasts/audio-stream', '/documentaries/video-stream'):
            channel, video = query.get('channel'), query.get('v', '')
            archived = False
            if channel and podcast_videos.VIDEO_ID.fullmatch(video):
                for show in current()['subscriptions']:
                    if feeds.channel_id(show['url']) == channel and ARCHIVE.match(show['id'], '', video + '.m4a'):
                        archived = True
                        break
            return self.reply(*self.channel_stream(channel, video, path == '/documentaries/video-stream',
                                                  query.get('hls') == '1', archived=archived,
                                                  desktop=query.get('profile') == 'mpv'))
        if path == '/podcasts/video-stream':
            return self.reply(*podcast_streams.open_stream(query.get('v', ''), desktop=query.get('profile') == 'mpv',
                                                         index=video_matching.endpoint()))
        if path == '/podcasts/video-links':
            return self.reply(200, video_matching.endpoint())
        raise LookupError

    def web_api(self, path, query):
        if match := re.fullmatch(r'/api/video-matching/([0-9a-f]{64})(?:/([0-9a-f]{64}))?', path):
            return self.video_matching_api(*match.groups())
        if path in ('/api/saved', '/api/queue', '/api/listening'):
            if self.command == 'GET' and path == '/api/listening':
                return self.reply(200, current().get('playbackPreferences', {}))
            if self.command == 'PUT' and path == '/api/queue':
                with podcasts.LOCK:
                    state = podcasts.load()
                    existing = state.get('queue', [])
                    entries = listening_state(state, 'queue')['episodes']
                    next_up = entries[0] if entries else None
                    if next_up:
                        existing.remove(next_up['episode']['id'])
                        podcasts.save(state)
                return self.reply(200, {'episode': next_up})
            if self.command == 'PUT' and path == '/api/listening':
                body = self.read_json()
                if not isinstance(body, dict):
                    raise ValueError('Invalid playback action.')
                with podcasts.LOCK:
                    state = podcasts.load()
                    listening.apply(state, body)
                    podcasts.save(state)
                return self.reply(200, {'ok': True})
            if self.command == 'GET' and path != '/api/listening':
                return self.reply(200, listening_state(current(), path.rsplit('/', 1)[-1], query.get('ids') == '1'))
        if path.startswith('/api/video-playback/'):
            identifier = path.rsplit('/', 1)[-1]
            if not listening.VIDEO.fullmatch(identifier):
                raise LookupError
            if self.command == 'GET':
                return self.reply(200, current().get('videoPlayback', {}).get(identifier, {}))
            if self.command == 'PUT':
                body = self.read_json()
                if not isinstance(body, dict):
                    raise ValueError('Invalid video progress.')
                with podcasts.LOCK:
                    state = podcasts.load()
                    value = listening.update_video(state, identifier, body)
                    podcasts.save(state)
                return self.reply(200, value)
        if path == '/api/presentation' and self.command == 'GET':
            return self.reply(200, PRESENTATION)
        if path == '/api/notifications' and self.command == 'GET':
            state = current()
            start = podcasts.timestamp(float(query.get('since', '0')))
            now = time.time()
            shows = {show['id']: show for show in state['subscriptions'] if show.get('notify')}
            return self.reply(200, {'checkedAt': now, 'episodes': [
                row(shows[key], episode, state)
                for key, episode in ARCHIVE.notifications(shows, max(start, now - 7 * 86400), now)
            ]})
        if path == '/api/lists' or path.startswith('/api/lists/'):
            return self.playlist_api(path)
        if path == '/api/backup' and self.command == 'GET':
            if not self.session_account():
                return self.reply(403, {'error': 'Sign in in the web app to download a backup.'})
            return self.send_backup()
        if path == '/api/opml/preview' and self.command == 'POST':
            payload = self.read_json(2_100_000)
            return self.reply(200, {'feeds': preview_opml(payload.get('xml') if isinstance(payload, dict) else None)})
        if path == '/api/opml/import' and self.command == 'POST':
            payload = self.read_json()
            return self.reply(*start_import(payload.get('urls') if isinstance(payload, dict) else None))
        if path.startswith('/api/opml/jobs/') and self.command == 'GET':
            with IMPORT_LOCK:
                job = IMPORT_JOBS.get(path.rsplit('/', 1)[-1])
                if not job:
                    raise LookupError
                return self.reply(200, {**job, 'failed': list(job['failed'])})
        if self.command == 'GET':
            if path == '/api/chapters':
                show = find(query.get('show'))
                episode = ARCHIVE.episode(show['id'], query.get('episode')) if show else None
                if not episode:
                    raise LookupError
                markers = episode.get('chapters', [])
                if address := episode.get('chapterURL'):
                    try:
                        _, status, _, payload = feeds.request_public(address, max_bytes=2_000_000)
                        if status == 200:
                            markers = json.loads(payload).get('chapters') or markers
                    except (OSError, ValueError, HTTPException, AttributeError):
                        pass
                return self.reply(200, {'chapters': [
                    {'startTime': marker['startTime'], 'title': feeds.clean(marker['title'])[:400]}
                    for marker in markers if isinstance(marker, dict)
                    and type(marker.get('startTime')) in (int, float)
                    and 0 <= marker['startTime'] < 30 * 86400
                    and isinstance(marker.get('title'), str)
                ][:1000]})
            if path == '/api/shows-search':
                term = query.get('q', '').casefold()
                return self.reply(200, {'shows': [
                    {**show, **PRESENTATION.get(show['id'], {}), 'rss': feed_url(show)}
                    for show in current()['subscriptions'] if term and term in ' '.join(
                        [show['title'], show['publisher']] +
                        [person['name'] for person in PRESENTATION.get(show['id'], {}).get('hosts', [])]).casefold()
                ][:60]})
            if path == '/api/episodes':
                return self.reply(200, pages(current(), query))
            if path == '/api/archive/status':
                return self.reply(200, ARCHIVE.counts())
            if path == '/api/library':
                state = current()
                shows = [{**show, **PRESENTATION.get(show['id'], {}), 'rss': feed_url(show), 'minimumEpisodeMinutes': feeds.minimum_minutes(show)}
                         for show in state['subscriptions']]
                return self.reply(200, {'shows': shows, 'progress': state['progress']})
            if path == '/api/progress':
                return self.reply(*podcasts.endpoint('progress'))
            if path == '/api/continue':
                return self.reply(200, {'episodes':continue_episodes()})
            if path == '/library.opml':
                return self.send_body(200, opml(current()['subscriptions']), 'text/x-opml; charset=utf-8')
            if path == '/api/sync-settings':
                return self.reply(200, {'server':ORIGIN,'username':'ondier','password':sync_password()})
            if path == '/api/search':
                if query.get('source') == 'youtube':
                    return self.reply(*youtube.search_channels(query.get('q', '')))
                return self.reply(200, feeds.apple_search(query.get('q', '').strip()))
            if path == '/api/video':
                show = find(query.get('show'))
                if not show:
                    raise LookupError
                episode_id = query.get('episode')
                episode = ARCHIVE.episode(show['id'], episode_id)
                if not episode:
                    episode = next((ep for ep in load_feed(show)['episodes'] if ep['id'] == episode_id), None)
                if not episode:
                    raise LookupError
                links = video_matching.endpoint()
                if clip := query.get('clip'):
                    if not any(clip_id(item) == clip for item in links.get('clips', {}).get(episode_id, [])):
                        raise LookupError
                    return self.reply(*podcast_streams.open_stream(clip, index=links))
                video = available_video(episode, links)
                if not video:
                    raise LookupError
                if episode.get('youtubeAudioID') and video['url'] == episode['enclosure']:
                    return self.reply(*self.channel_stream(feeds.channel_id(show['url']), episode['youtubeAudioID'],
                                                           True, archived=True))
                fallback = links['selections'].get(episode_id, {}).get('fallback')
                for candidate in [video] + ([video_matching.descriptor(fallback)] if fallback else []):
                    result = podcast_streams.open_stream(candidate['id'], index=links)
                    if result[0] == 200:
                        if candidate['label'] == 'France.tv':
                            try:
                                result = podcast_streams.relay_france(result[1])
                            except (OSError, ValueError, HTTPException):
                                result = (503, {'error': 'France.tv could not deliver this replay.'})
                        if result[0] == 200:
                            return self.reply(200, {**result[1], 'video': candidate})
                return self.reply(*result)
        if path == '/api/follow' and self.command == 'POST':
            body = self.read_json()
            show = follow(body['url'], body.get('source') == 'youtube')
            return self.reply(200, {**show, 'rss':feed_url(show)})
        if path == '/api/progress' and self.command == 'PUT':
            return self.reply(*podcasts.endpoint('progress', self.read_json()))
        if path.startswith('/api/shows/'):
            show = find(path.rsplit('/', 1)[-1])
            if show is None:
                raise LookupError
            if self.command == 'GET':
                return self.reply(200, episode_list(show, query.get('refresh') == '1'))
            if self.command == 'DELETE':
                save_subscription(show, 'remove')
                return self.reply(200, {'ok':True})
            if self.command == 'PUT':
                patch = self.read_json()
                for name in ('minimumEpisodeMinutesOverride', 'notify'):
                    if name in patch:
                        show[name] = patch[name]
                save_subscription(show)
                return self.reply(200, {'ok':True})
        raise LookupError

    def video_matching_api(self, show_id, episode_id):
        state = current()
        show = find(show_id, state)
        if show is None:
            raise LookupError
        if episode_id and not ARCHIVE.episode(show_id, episode_id):
            raise LookupError
        if self.command == 'GET':
            if episode_id:
                override = state.get('videoOverrides', {}).get(show_id, {}).get(episode_id)
                return self.reply(200, override or {'mode': 'automatic'})
            config = video_matching.settings(state, show)
            return self.reply(200, {**config, 'profiles': video_matching.PROFILES})
        if self.command != 'PUT':
            raise LookupError
        body = self.read_json()
        updated = (video_matching.episode_override(body) if episode_id else
                   video_matching.validated(body, video_matching.settings(state, show)))
        with podcasts.LOCK:
            state = podcasts.load()
            if find(show_id, state) is None:
                raise LookupError
            if episode_id:
                overrides = state.setdefault('videoOverrides', {})
                episodes = overrides.setdefault(show_id, {})
                if updated is None:
                    episodes.pop(episode_id, None)
                    if not episodes:
                        overrides.pop(show_id)
                else:
                    episodes[episode_id] = updated
            else:
                state.setdefault('videoMatching', {})[show_id] = updated
            podcasts.save(state)
        if not episode_id:
            video_matching.WAKE.set()
        return self.reply(200, updated or {'mode': 'automatic'})

    def playlist_api(self, path):
        if self.command == 'GET' and path == '/api/lists':
            state = current()
            return self.reply(200, {'lists': [dict(value, id=key, rss=list_url(key))
                                             for key, value in state.get('playlists', {}).items()],
                                    'shows': state['subscriptions'], 'categories': ARCHIVE.categories(state['subscriptions'])})
        body = self.read_json() if self.command in ('POST', 'PUT') else {}
        if not isinstance(body, dict):
            raise ValueError('Invalid list request.')
        with podcasts.LOCK:
            state = podcasts.load()
            lists = state.setdefault('playlists', {})
            if path == '/api/lists' and self.command == 'POST':
                if len(lists) >= 100:
                    raise ValueError('Too many lists.')
                identifier = secrets.token_hex(16)
                lists[identifier] = playlist_payload(body)
            else:
                match = re.fullmatch(r'/api/lists/([0-9a-f]{32})(/members)?', path)
                if not match:
                    raise LookupError
                identifier = match[1]
                listing = listed(state, identifier)
                if match[2] and self.command == 'PUT':
                    episode = body.get('episode', '')
                    operation = body.get('operation')
                    if (listing['kind'] != 'collection' or not isinstance(episode, str)
                            or not podcasts.ID.fullmatch(episode) or operation not in ('add', 'remove')):
                        raise ValueError('Invalid collection action.')
                    if operation == 'add':
                        if not any(ARCHIVE.episode(show['id'], episode) for show in state['subscriptions']):
                            raise LookupError
                        if episode not in listing['episodes']:
                            if len(listing['episodes']) >= 5000:
                                raise ValueError('This collection is full.')
                            listing['episodes'].append(episode)
                    elif episode in listing['episodes']:
                        listing['episodes'].remove(episode)
                elif not match[2] and self.command == 'PUT':
                    if body.get('kind') != listing['kind']:
                        raise ValueError('The list type cannot be changed.')
                    lists[identifier] = playlist_payload(body, listing)
                elif not match[2] and self.command == 'DELETE':
                    del lists[identifier]
                else:
                    raise LookupError
            podcasts.save(state)
            listing = lists.get(identifier)
        return self.reply(200, dict(listing, id=identifier, rss=list_url(identifier)) if listing else {'ok': True})

    def send_backup(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(dir=podcasts.ROOT) as temporary:
            output = Path(temporary) / 'ondier.zip'
            backups.create(output, podcasts.ROOT, KEY)
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', 'attachment; filename="Ondier-backup.zip"')
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('Content-Length', str(output.stat().st_size))
            self.end_headers()
            with output.open('rb') as data:
                while block := data.read(128 * 1024):
                    self.wfile.write(block)

    def gpodder(self, path, query):
        if not self.sync_auth():
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Ondier Sync"')
            self.send_header('Content-Length', '0')
            return self.end_headers()
        route = path.removeprefix(SIMPLE_SYNC)
        stamp = int(time.time())
        since = max(0, int(query.get('since', query.get('timestamp', '0'))))
        state = current()
        if self.command == 'POST' and path == '/api/2/auth/ondier/login.json':
            self.send_response(200)
            self.send_header('Set-Cookie', f'ondier_sync={sync_cookie()}; Path=/api/2;{COOKIE_SECURE} HttpOnly; SameSite=Strict')
            self.send_header('Content-Length', '0')
            return self.end_headers()
        if self.command == 'POST' and path.startswith('/api/2/devices/ondier/'):
            return self.reply(200, {})
        if path == '/api/2/devices/ondier.json' and self.command == 'GET':
            return self.reply(200, [{'id':'ondier','caption':'Ondier','type':'other','subscriptions':len(state['subscriptions'])}])
        if (route in ('subscriptions', 'subscription_change/create') or path.startswith('/api/2/subscriptions/ondier/')):
            if self.command == 'GET':
                changes = state.get('subscriptionVersions', {})
                active = {s['id']:s for s in state['subscriptions']}
                adds = [feed_url(s) for identifier,s in active.items() if not since or changes.get(identifier, 0) >= since]
                removed = [feed_url({'id':identifier}) for identifier,t in changes.items() if since and t >= since and identifier not in active]
                return self.reply(200, {'add':adds,'remove':removed, 'timestamp':stamp})
            if self.command == 'POST':
                update = self.read_json()
                rewrites = []
                for url in update.get('add', [])[:100]:
                    show = sync_show(url, current()['subscriptions'])
                    if show is None and not url.startswith(ORIGIN + '/'):
                        show = follow(url, (urlsplit(url).hostname or '') in ('youtube.com','www.youtube.com','m.youtube.com'))
                    if show and url != feed_url(show):
                        rewrites.append([url, feed_url(show)])
                for url in update.get('remove', [])[:100]:
                    if show := sync_show(url, current()['subscriptions']):
                        save_subscription(show, 'remove')
                return self.reply(200, {'timestamp':stamp,'update_urls':rewrites})
        if route in ('episode_action', 'episode_action/create') or path == '/api/2/episodes/ondier.json':
            if self.command == 'GET':
                return self.reply(200, {'actions':sync_actions(since, state),'timestamp':stamp})
            if self.command == 'POST':
                values = self.read_json()
                if isinstance(values, dict):
                    values = values.get('actions', [])
                cache = {}
                for action in values[:1000]:
                    kind = str(action.get('action', '')).casefold()
                    if kind not in ('play','new'):
                        continue
                    show = sync_show(action.get('podcast'), state['subscriptions'])
                    episode = sync_episode(action, show, cache) if show else None
                    if not episode:
                        continue
                    position = int(action.get('position', 0)) if kind == 'play' else 0
                    if position < 0:
                        continue
                    total = int(action.get('total', 0))
                    date = datetime.fromisoformat(action.get('timestamp') or datetime.now(timezone.utc).isoformat())
                    updated = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
                    progress = {'position':max(0, position),'updatedAt':min(time.time(), max(0, updated)),
                                'finished':kind == 'play' and total > 0 and position >= total}
                    podcasts.endpoint('progress', {'id':episode['id'],'progress':progress})
                return self.reply(200, {'timestamp':stamp,'update_urls':[]})
        raise LookupError

    def nextcloud_login(self, path):
        with LOGIN_LOCK:
            now = time.time()
            for old in list(LOGINS):
                if LOGINS[old]['expires'] < now:
                    del LOGINS[old]
            if self.command == 'POST' and path.endswith('/v2'):
                if len(LOGINS) >= 50:
                    return self.reply(429, {'error':'Try again later.'})
                token = secrets.token_urlsafe(24)
                view = secrets.token_urlsafe(24)
                LOGINS[token] = {'view':view, 'expires':now+600, 'accepted':False}
                return self.reply(200, {'poll':{'token':token,'endpoint':ORIGIN+'/index.php/login/v2/poll'},'login':ORIGIN+'/connect/'+view})
            if self.command == 'POST' and path.endswith('/poll'):
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 4096:
                    raise ValueError('Invalid login poll.')
                token = parse_qs(self.rfile.read(size).decode(), max_num_fields=2).get('token', [''])[0]
                if token not in LOGINS or not LOGINS[token]['accepted']:
                    raise LookupError
                del LOGINS[token]
                return self.reply(200, {'server':ORIGIN,'loginName':'ondier','appPassword':sync_password()})
            if match := LOGIN.fullmatch(path):
                flow = next((flow for flow in LOGINS.values() if flow['view'] == match[1]), None)
                if flow is None:
                    raise LookupError
                if not self.session_account():
                    if self.command != 'GET':
                        return self.reply(401, {'error': 'Sign in to Ondier.'})
                    self.send_response(303)
                    self.send_header('Location', '/?connect=' + match[1])
                    self.send_header('Content-Length', '0')
                    return self.end_headers()
                if self.command == 'GET':
                    body = '<h1>Connect to Ondier</h1><form method="post"><button>Authorize podcast sync</button></form>'
                elif self.command == 'POST' and self.headers.get('Origin') == ORIGIN:
                    flow['accepted'] = True
                    body = '<h1>Connected</h1><p>Return to your podcast app.</p>'
                else:
                    return self.reply(403, {'error':'Invalid login request.'})
                return self.send_body(200, ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Ondier</title><link rel="stylesheet" href="/style.css"><main>'+body+'</main></html>').encode(), 'text/html; charset=utf-8')
            raise LookupError


class ShortcutHandler(BaseHTTPRequestHandler):
    """The short HTTP address only sends visitors to the HTTPS web app."""

    def log_message(self, *_):
        pass

    def redirect(self):
        self.send_response(302)
        self.send_header('Location', ORIGIN + '/')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def reject(self):
        self.send_response(405)
        self.send_header('Allow', 'GET, HEAD')
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_GET = do_HEAD = redirect
    do_POST = do_PUT = do_DELETE = do_PATCH = reject


def main():
    if os.environ.get('ONDIER_YOUTUBE_KEY_FILE'):
        os.environ['QUIRE_YOUTUBE_API_KEY'] = secret_file('ONDIER_YOUTUBE_KEY_FILE')
    video_matching.migrate()
    if os.environ.get('ONDIER_WORKERS','1') == '1':
        indexed = threading.Event()
        threading.Thread(target=feeds.refresh_loop, args=(ARCHIVE, indexed), daemon=True, name='ondier-rss').start()
        threading.Thread(target=backups.schedule, args=(podcasts.ROOT, KEY, indexed), daemon=True,
                         name='ondier-backups').start()
        threading.Thread(target=video_matching.run, args=(Path(__file__).parent,), daemon=True, name='ondier-video-index').start()
    shortcut = ThreadingHTTPServer(('0.0.0.0', 8789), ShortcutHandler)
    threading.Thread(target=shortcut.serve_forever, daemon=True, name='ondier-shortcut').start()
    ThreadingHTTPServer(('0.0.0.0', 8788), Handler).serve_forever()


ACCOUNTS = Accounts(podcasts.ROOT)
ARCHIVE = Archive(podcasts.ROOT)

if __name__ == '__main__':
    with backups.runtime_lock(podcasts.ROOT):
        main()
