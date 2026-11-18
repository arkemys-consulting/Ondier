"""Run in the Ondier image with ONDIER_WORKERS=0 and an isolated /data."""
from contextlib import closing
from base64 import b64encode
from datetime import datetime, timezone
from email.utils import format_datetime
from hashlib import sha256
from http.client import HTTPConnection
from pathlib import Path
from xml.etree import ElementTree as XML
from unittest.mock import patch
from urllib.error import HTTPError
import json
import os
import secrets
import threading
import time
import unittest

os.environ['ONDIER_ORIGIN'] = 'https://ondier.test'
os.environ['ONDIER_OWNER'] = 'owner@example.test'
os.environ['ONDIER_ACCESS_KEY'] = secrets.token_hex(32)
os.environ['ONDIER_CONTENTS_TOKEN_FILE'] = '/tmp/bridge'
Path('/tmp/bridge').write_text(secrets.token_hex(32))

import backups
import feeds
import podcasts
import server
import video_matching

assert podcasts.ROOT == Path('/data')
assert not (podcasts.ROOT / 'state.json').exists()
assert not server.ACCOUNTS.initialized()

RSS = """
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>History Podcast</title><itunes:category text="History"/>
    <item>
      <title>Le voyage des cartographes</title><guid>long</guid>
      <pubDate>Thu, 01 Jan 2026 12:00:00 GMT</pubDate>
      <itunes:duration>56:00</itunes:duration>
      <description>Les cartes maritimes au dix-septième siècle.</description>
      <enclosure url="https://example.test/long.mp3" type="audio/mpeg"/>
      <podcast:transcript url="https://example.test/long.vtt" type="text/vtt"/>
    </item>
    <item>
      <title>Cartographes : extrait</title><guid>clip</guid>
      <pubDate>Fri, 02 Jan 2026 12:00:00 GMT</pubDate>
      <itunes:duration>2:00</itunes:duration>
      <enclosure url="https://example.test/clip.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Un nouvel épisode</title><guid>future</guid>
      <pubDate>Mon, 01 Jan 2090 12:00:00 GMT</pubDate>
      <itunes:duration>60:00</itunes:duration>
      <enclosure url="https://example.test/future.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""


class OndierArchiveTest(unittest.TestCase):
    def test_archive_playlists_import_and_backup(self):
        origin = 'https://example.test/feed.xml'
        show = {'id': 'a' * 64, 'url': origin, 'title': 'History Podcast',
                'publisher': '', 'notify': False, 'minimumEpisodeMinutesOverride': 15}
        feed = feeds.parse(RSS.encode(), origin, show['id'])
        self.assertEqual(['History'], feed['categories'])
        episode, short, _ = feed['episodes']
        server.ARCHIVE.record(show, feed)
        # Publisher rolls its catalogue forward. The older episode remains indexed.
        server.ARCHIVE.record(show, {**feed, 'episodes': [short]})
        with server.ARCHIVE.db() as db:
            transcript = "Un navigateur évoque l’astrolabe et les épices."
            db.execute("UPDATE episodes SET transcript = ? WHERE id = ?", (transcript, episode['id']))
        original_state = {'subscriptions': [show], 'progress': {}}
        podcasts.save(original_state)
        self.assertEqual(3, server.ARCHIVE.counts()['episodes'])
        self.assertEqual(1, server.ARCHIVE.counts()['transcripts'])
        self.assertEqual(1, len(server.ARCHIVE.page(original_state, query='cartographes')[0]))
        self.assertFalse(server.ARCHIVE.page(original_state, mode='latest')[1])
        self.assertEqual(episode['id'], server.ARCHIVE.match(show['id'], 'long', '')['id'])
        _, token = server.ACCOUNTS.create('test', secrets.token_urlsafe(18))
        valid = server.ACCOUNTS.new_key('Roan', 0)
        cookie = {'Cookie': f'{server.ACCOUNT_COOKIE}={token}', 'Origin': server.ORIGIN}
        bearer = {'Authorization': 'Bearer ' + valid['key']}
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        running = threading.Thread(target=http.serve_forever, daemon=True)
        running.start()

        def request(path, status=200, method='GET', payload=None, headers=None):
            with closing(HTTPConnection(*http.server_address, timeout=12)) as connection:
                opts = dict(headers or {})
                if payload is not None:
                    opts['Content-Type'] = 'application/json'
                connection.request(method, path, json.dumps(payload) if payload is not None else None, opts)
                response = connection.getresponse()
                result = response.read()
                self.assertEqual(status, response.status, (path, result[:100]))
                return json.loads(result) if 'application/json' in response.getheader('Content-Type', '') else result

        backup = podcasts.ROOT / 'test-backup.zip'
        try:
            request('/api/episodes?view=latest', 401)
            results = request('/podcasts/episode-search?q=epices', headers=bearer)
            self.assertEqual(1, len(results['episodes']))
            self.assertIn('astrolabe', results['episodes'][0]['excerpt'])
            self.assertEqual(1, len(request('/api/episodes?view=latest', headers=cookie)['episodes']))
            self.assertFalse(request('/api/episodes?view=history', headers=cookie)['episodes'])
            smart = request('/api/lists', method='POST', headers=cookie,
                            payload={'name': 'Long History', 'kind': 'smart', 'category': 'History', 'minimum': 45, 'unplayed': True})
            smart_path = f"/api/episodes?view=list&list={smart['id']}"
            self.assertEqual(1, len(request(smart_path, headers=cookie)['episodes']))
            rss_path = smart['rss'].split(server.ORIGIN)[1]
            rss = XML.fromstring(request(rss_path))
            self.assertEqual(episode['id'], rss.findtext('channel/item/guid'))
            request(rss_path.replace('.xml', '0.xml'), 401)
            request('/podcasts/progress', method='PUT', headers=bearer, payload={'id':episode['id'], 'progress':{
                'position': 3360, 'updatedAt': time.time(), 'finished': True}})
            self.assertFalse(request(smart_path, headers=cookie)['episodes'])
            self.assertEqual(episode['id'], request('/api/episodes?view=history', headers=cookie)['episodes'][0]['episode']['id'])
            # One account state is shared by native API keys and browser sessions.
            listening_path = '/api/listening'
            request(listening_path, method='PUT', headers=cookie, payload={'operation':'save','id':episode['id']})
            self.assertEqual(episode['id'], request('/api/saved', headers=bearer)['episodes'][0]['episode']['id'])
            request(listening_path, method='PUT', headers=bearer, payload={'operation':'unsave','id':episode['id']})
            request(listening_path, method='PUT', headers=bearer,
                    payload={'operation':'importSaved','id':episode['id'],'savedAt':time.time()})
            self.assertEqual([], request('/api/saved', headers=cookie)['ids'])
            for key in (episode['id'], short['id']):
                request(listening_path, method='PUT', headers=bearer, payload={'operation':'next','id':key})
            reorder = {'operation':'down','id':short['id'],'mutationID':'f02bb384-4eb9-48d6-9502-ad15419f2668'}
            request(listening_path, method='PUT', headers=cookie, payload=reorder)
            request(listening_path, method='PUT', headers=bearer, payload={'operation':'next','id':short['id']})
            request(listening_path, method='PUT', headers=cookie, payload=reorder)
            self.assertEqual([short['id'], episode['id']], request('/api/queue', headers=bearer)['ids'])
            claimed = request('/api/queue', method='PUT', headers=cookie, payload={})['episode']
            self.assertEqual(short['id'], claimed['episode']['id'])
            self.assertEqual([episode['id']], request('/api/queue', headers=bearer)['ids'])
            request(listening_path, method='PUT', headers=bearer, payload={'operation':'next','id':short['id']})
            request(listening_path, method='PUT', headers=cookie, payload={'operation':'speed','rate':1.75})
            self.assertEqual(1.75, request('/api/listening', headers=bearer)['speed'])
            stamp = time.time()
            request(listening_path, method='PUT', headers=cookie,
                    payload={'operation':'reset','ids':[episode['id'],short['id']],'updatedAt':stamp})
            self.assertFalse(request('/api/progress', headers=bearer)['progress'][episode['id']]['finished'])
            request('/api/progress', method='PUT', headers=bearer, payload={'id':episode['id'], 'progress':{
                'position': 3360, 'updatedAt': stamp + 1, 'finished': True}})
            video = '/api/video-playback/Abcdef12345'
            request(video, 401)
            request(video, method='PUT', headers=bearer, payload={'position':73.5,'audio':'fr','updatedAt':stamp})
            request(video, method='PUT', headers=cookie, payload={'subtitle':'off','updatedAt':stamp + 1})
            request(video, method='PUT', headers=bearer, payload={'position':10,'updatedAt':stamp - 1})
            request(video, method='PUT', headers=bearer, payload={'position':5,'updatedAt':stamp + 2,'import':True})
            self.assertEqual((73.5,'fr','off'), tuple(request(video, headers=cookie)[key] for key in ('position','audio','subtitle')))
            self.assertEqual([], request('/api/chapters?show=' + show['id'] + '&episode=' + short['id'],
                                         headers=bearer)['chapters'])
            sources = '/api/video-matching/' + show['id']
            match_path = sources + '/' + episode['id']
            index_path = '/podcasts/video-links'
            first = 'https://www.youtube.com/watch?v=Abcdef12345'
            second = 'https://www.france.tv/france-2/example/1234567-replay.html'
            channel = 'https://www.youtube.com/channel/UC' + 'a' * 22
            self.assertEqual('standard', request(sources, headers=cookie)['profile'])
            body = dict(youtube=channel, franceCatalogues=['https://www.france.tv/france-2/example/'],
                        providerOrder=['youtube','franceTV'], fallback=True, profile='standard',
                        rules={'dateWindowDays':3, 'maxDurationDifferenceSeconds':90, 'minimumScore':100})
            request(sources, 403, 'PUT', body, {'Cookie': cookie['Cookie']})
            request(sources, 400, 'PUT', {**body, 'franceCatalogues':['https://example.test/program/']}, cookie)
            config = request(sources, method='PUT', payload=body, headers=cookie)
            published = datetime.fromtimestamp(stamp, timezone.utc)
            title = 'Les cartes de la nouvelle expédition'
            new_episode = f'''<item><title>{title}</title><guid>new-video-match</guid>
                <pubDate>{format_datetime(published)}</pubDate><itunes:duration>56:00</itunes:duration>
                <enclosure url="https://example.test/new.mp3" type="audio/mpeg"/></item>'''
            updated_rss = RSS.replace('</channel>', new_episode + '</channel>').encode()
            incoming = sha256(f"{show['id']}\nnew-video-match".encode()).hexdigest()
            fixture = {'origin': origin, 'raw': b64encode(updated_rss).decode()}
            automatic = video_matching.matcher.Video(first, title, published, 3360, stamp + 3600)
            too_short = video_matching.matcher.Video(second, title, published, 3200, stamp + 3600)
            with patch.object(feeds, 'rss_entry', return_value=fixture), \
                 patch.object(video_matching.matcher, 'key_at', return_value='test'), \
                 patch.object(video_matching.matcher, 'youtube_catalogue', return_value=[automatic]), \
                 patch.object(video_matching.matcher, 'france_replays', return_value=[too_short]):
                video_matching.refresh(Path('/app'), {}, {})
            selected = request(index_path, headers=bearer)['selections'][incoming]
            self.assertEqual(first, selected['primary']['url'])
            self.assertIsNone(selected['fallback'])
            public_feed = f'''<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015">
                <entry><yt:videoId>Abcdef12345</yt:videoId><yt:channelId>{config['channel']}</yt:channelId>
                <title>{title}</title><published>{published.isoformat()}</published></entry></feed>'''.encode()
            with patch.object(feeds, 'rss_entry', return_value=fixture), \
                 patch.object(video_matching.matcher, 'key_at', return_value='test'), \
                 patch.object(video_matching.matcher, 'youtube_catalogue',
                              side_effect=HTTPError('', 403, 'Quota', None, None)), \
                 patch.object(video_matching.matcher, 'fetch', return_value=public_feed), \
                 patch.object(video_matching.youtube, 'public_uploads', return_value={
                     'channel_id': config['channel'], 'entries':[{'id':'Abcdef12345', 'duration':3360}]}), \
                 patch.object(video_matching.matcher, 'france_replays', return_value=[too_short]):
                video_matching.matcher.index_path().unlink()
                video_matching.refresh(Path('/app'), {}, {})
            selected = request(index_path, headers=bearer)['selections'][incoming]
            self.assertEqual(first, selected['primary']['url'])
            catalogue = {
                video_matching.provider_key(show, config, provider): {
                    'links': {episode['id']: {'url': url, 'expiresAt': stamp + 3600}},
                } for provider, url in (('youtube', first), ('franceTV', second))
            }
            request(index_path, 401)
            request(sources + '/' + 'a' * 64, 404, 'PUT', {'mode':'hidden'}, cookie)
            request(match_path, 400, 'PUT', {'mode':'manual','url':'https://example.test/'}, cookie)
            with patch.object(video_matching.matcher, 'load_index', return_value=catalogue):
                selected = request(index_path, headers=bearer)['selections'][episode['id']]
                self.assertEqual((first, second), (selected['primary']['url'], selected['fallback']['url']))
                reversed_order = {**body, 'providerOrder':['franceTV','youtube'], 'fallback':False}
                request(sources, method='PUT', payload=reversed_order, headers=cookie)
                selected = request(index_path, headers=bearer)['selections'][episode['id']]
                self.assertEqual(second, selected['primary']['url'])
                self.assertIsNone(selected['fallback'])
                request(match_path, method='PUT', payload={'mode':'hidden'}, headers=cookie)
                hidden = request(index_path, headers=bearer)
                self.assertIn(episode['id'], hidden['hidden'])
                self.assertNotIn(episode['id'], hidden['selections'])
                request(match_path, method='PUT', payload={'mode':'automatic'}, headers=cookie)
                self.assertEqual(second, request(index_path, headers=bearer)['selections'][episode['id']]['primary']['url'])
                manual = 'https://youtu.be/GhiJkl67890'
                request(match_path, method='PUT', payload={'mode':'manual', 'url':manual}, headers=cookie)
                self.assertEqual('https://www.youtube.com/watch?v=GhiJkl67890',
                                 request(index_path, headers=bearer)['selections'][episode['id']]['primary']['url'])
            collection = request('/api/lists', method='POST', headers=cookie,
                                 payload={'name': 'Saved', 'kind': 'collection'})
            member_path = f"/api/lists/{collection['id']}/members"
            for _ in range(2):
                request(member_path, method='PUT', headers=cookie, payload={'episode':episode['id'],'operation':'add'})
            listing = request(f"/api/episodes?view=list&list={collection['id']}", headers=cookie)
            self.assertEqual(1, len(listing['episodes']))
            request(member_path, method='PUT', headers=cookie, payload={'episode':episode['id'],'operation':'remove'})
            rss = request('/library.opml', headers=cookie).decode()
            preview = request('/api/opml/preview', method='POST', headers=cookie, payload={'xml':rss})
            self.assertTrue(preview['feeds'][0]['duplicate'])
            job = request('/api/opml/import', 202, 'POST', {'urls':[origin, origin]}, cookie)
            for _ in range(30):
                result = request('/api/opml/jobs/' + job['id'], headers=cookie)
                if result['done']:
                    break
                time.sleep(.05)
            self.assertEqual((1, 1, 1), (result['done'], result['total'], result['duplicates']))
            request('/api/backup', 403, headers=bearer)
            backup.write_bytes(request('/api/backup', headers=cookie))
        finally:
            http.shutdown()
            http.server_close()
            running.join(timeout=3)

        self.assertEqual(1, backups.restore(backup, podcasts.ROOT, server.KEY, verify=True)['version'])
        wrong_key = secrets.token_hex(32)
        with self.assertRaises(ValueError):
            backups.restore(backup, podcasts.ROOT, wrong_key, verify=True)
        podcasts.save(original_state)
        backups.restore(backup, podcasts.ROOT, server.KEY)
        restored = podcasts.load()
        self.assertIn(smart['id'], restored['playlists'])
        self.assertTrue(restored['progress'][episode['id']]['finished'])
        self.assertEqual([short['id'], episode['id']], restored['queue'])
        self.assertEqual(73.5, restored['videoPlayback']['Abcdef12345']['position'])
        self.assertEqual(3, restored['videoMatching'][show['id']]['rules']['dateWindowDays'])
        self.assertEqual('manual', restored['videoOverrides'][show['id']][episode['id']]['mode'])
        self.assertIsNone(server.ACCOUNTS.authenticate(token))
        self.assertTrue(server.ACCOUNTS.authenticate(valid['key'], api=True))
        self.assertEqual(3, server.ARCHIVE.counts()['episodes'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
