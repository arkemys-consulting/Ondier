"""Retained episode metadata, full-text search and server-side playlists."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from http.client import HTTPException
from urllib.parse import urlsplit
from xml.etree import ElementTree as XML
import json
import re
import sqlite3
import time
import unicodedata

import feeds
import podcasts

DATABASE = 'episodes.sqlite3'
PLAYLISTS = re.compile(r'[0-9a-f]{32}')
TERM = re.compile(r'[^\W_]+', re.UNICODE)


class Archive:
    def __init__(self, directory):
        self.path = Path(directory) / DATABASE
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY, show_id TEXT NOT NULL, published REAL, duration REAL NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL, body TEXT NOT NULL,
                    transcript_url TEXT, transcript_type TEXT, transcript TEXT NOT NULL DEFAULT '',
                    transcript_checked INTEGER NOT NULL DEFAULT 0,
                    discovered REAL
                );
                CREATE INDEX IF NOT EXISTS episodes_show ON episodes(show_id, published DESC);
                CREATE TABLE IF NOT EXISTS show_metadata (
                    id TEXT PRIMARY KEY, categories TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS episode_search USING fts5(
                    title, description, transcript, content='episodes', tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS search_insert AFTER INSERT ON episodes BEGIN
                    INSERT INTO episode_search(rowid, title, description, transcript)
                    VALUES(new.rowid, new.title, new.description, new.transcript);
                END;
                CREATE TRIGGER IF NOT EXISTS search_update AFTER UPDATE OF title, description, transcript ON episodes
                WHEN new.title != old.title OR new.description != old.description OR new.transcript != old.transcript BEGIN
                    INSERT INTO episode_search(episode_search, rowid, title, description, transcript)
                    VALUES('delete', old.rowid, old.title, old.description, old.transcript);
                    INSERT INTO episode_search(rowid, title, description, transcript)
                    VALUES(new.rowid, new.title, new.description, new.transcript);
                END;
            """)
            if not any(column['name'] == 'discovered' for column in db.execute("PRAGMA table_info(episodes)")):
                db.execute("ALTER TABLE episodes ADD COLUMN discovered REAL")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, show, feed):
        rows = []
        discovered = time.time()
        for ep in feed['episodes']:
            episode = {**ep, 'chapters': ep.get('chapters', [])}
            expected = next(iter(episode.get('transcripts') or []), {})
            rows.append((ep['id'], show['id'], ep.get('date'), ep.get('duration') or 0,
                         ep['title'], ep.get('description') or '', json.dumps(episode, ensure_ascii=False),
                         expected.get('url'), expected.get('type'), discovered))
        with self.db() as db:
            categories = json.dumps(feed.get('categories') or [], ensure_ascii=False)
            db.execute("""INSERT INTO show_metadata VALUES (?, ?) ON CONFLICT(id)
                          DO UPDATE SET categories=excluded.categories WHERE excluded.categories != categories""",
                       (show['id'], categories))
            db.executemany("""
                INSERT INTO episodes(id, show_id, published, duration, title, description, body, transcript_url, transcript_type, discovered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET show_id=excluded.show_id, published=excluded.published,
                    duration=excluded.duration, title=excluded.title, description=excluded.description, body=excluded.body,
                    transcript=CASE WHEN excluded.transcript_url IS transcript_url
                        AND excluded.transcript_type IS transcript_type THEN transcript ELSE '' END,
                    transcript_checked=CASE WHEN excluded.transcript_url IS transcript_url
                        AND excluded.transcript_type IS transcript_type THEN transcript_checked ELSE 0 END,
                    transcript_url=excluded.transcript_url, transcript_type=excluded.transcript_type
                WHERE excluded.body != body OR excluded.show_id != show_id
            """, rows)

    def fill_transcripts(self, active_ids, limit=32):
        now = int(time.time())
        with self.db() as db:
            pending = db.execute("""
                SELECT id, transcript_url, transcript_type FROM episodes
                WHERE show_id IN (SELECT value FROM json_each(?))
                  AND transcript_url IS NOT NULL AND transcript = '' AND transcript_checked < ?
                ORDER BY transcript_checked, published DESC LIMIT ?
            """, (json.dumps(active_ids), now - 12 * 3600, limit)).fetchall()

        def fetch(row):
            try:
                _, _, _, raw = feeds.request_public(row['transcript_url'], max_bytes=2_000_000)
                value = transcript_text(raw, row['transcript_type'])
            except (OSError, ValueError, HTTPException):
                value = ''
            return value, now, row['id'], row['transcript_url'], row['transcript_type']

        with ThreadPoolExecutor(max_workers=3) as executor:
            updates = list(executor.map(fetch, pending))
        if updates:
            with self.db() as db:
                db.executemany("""UPDATE episodes SET transcript=?, transcript_checked=?
                                  WHERE id=? AND transcript_url=? AND transcript_type=?""", updates)

    def counts(self):
        with self.db() as db:
            row = db.execute("""SELECT count(*) AS episodes, count(DISTINCT show_id) AS shows,
                                count(NULLIF(transcript, '')) AS transcripts FROM episodes""").fetchone()
            return dict(row)

    def episode(self, show_id, episode_id):
        with self.db() as db:
            row = db.execute('SELECT body FROM episodes WHERE show_id = ? AND id = ?', (show_id, episode_id)).fetchone()
        return json.loads(row[0]) if row else None

    def locate(self, identifiers):
        with self.db() as db:
            rows = db.execute("""SELECT show_id, body FROM episodes
                                 WHERE id IN (SELECT value FROM json_each(?))""",
                              (json.dumps(list(identifiers)),)).fetchall()
        return [(row['show_id'], json.loads(row['body'])) for row in rows]

    def notifications(self, shows, since, now):
        minimums = {key: feeds.minimum_minutes(show) * 60 for key, show in shows.items()}
        with self.db() as db:
            rows = db.execute("""SELECT show_id, body FROM episodes e JOIN json_each(?) active
                                 ON active.key = e.show_id WHERE discovered > ? AND discovered <= ?
                                 AND published BETWEEN discovered - 604800 AND ?
                                 AND duration >= active.value ORDER BY discovered, e.id""",
                              (json.dumps(minimums), since, now, now)).fetchall()
        return [(row['show_id'], json.loads(row['body'])) for row in rows]

    def match(self, show_id, guid, address):
        with self.db() as db:
            row = db.execute("""SELECT body FROM episodes WHERE show_id = ? AND
                (id = ? OR json_extract(body, '$.guid') = ? OR json_extract(body, '$.enclosure') = ?
                 OR json_extract(body, '$.youtubeAudioID') = ?) LIMIT 1""",
                             (show_id, guid, guid, address, address.rsplit('/', 1)[-1].removesuffix('.m4a')
                              if isinstance(address, str) else None)).fetchone()
        return json.loads(row[0]) if row else None

    def page(self, state, *, mode='search', query='', listing=None, offset=0, limit=30):
        shows = {show['id']: show for show in state['subscriptions']}
        exposed = {identifier: feeds.minimum_minutes(show) * 60 for identifier, show in shows.items()}
        if listing and listing.get('shows'):
            exposed = {identifier: minimum for identifier, minimum in exposed.items() if identifier in listing['shows']}
        params = {'shows': json.dumps(exposed), 'now': time.time(), 'limit': limit + 1, 'offset': offset}
        joins = "JOIN json_each(:shows) AS active ON active.key = e.show_id"
        filters = ["(e.published IS NULL OR e.published <= :now)"]
        if mode != 'history':
            filters.append('e.duration >= active.value')
        columns = "e.show_id, e.body, NULL AS excerpt"
        order = 'e.published DESC, e.id'
        terms = TERM.findall(unicodedata.normalize('NFC', query))[:12]
        if query.strip() and not terms:
            return [], False
        if terms:
            params['fts'] = ' AND '.join(f'"{word}"*' for word in terms)
            joins += ' JOIN episode_search ON episode_search.rowid = e.rowid'
            filters.append('episode_search MATCH :fts')
            columns = "e.show_id, e.body, snippet(episode_search, -1, '', '', '…', 18) AS excerpt"
            if mode == 'search':
                order = 'bm25(episode_search, 10.0, 3.0, 1.0), e.published DESC, e.id'
        if mode == 'history':
            played = {identifier: progress['updatedAt'] for identifier, progress in state['progress'].items()
                      if progress.get('finished') or progress.get('position', 0) > 0}
            joins += ' JOIN json_each(:played) AS history ON history.key = e.id'
            params['played'] = json.dumps(played)
            order = 'history.value DESC, e.id'
        if mode == 'latest':
            filters.append('e.published IS NOT NULL')
        if listing:
            if listing['kind'] == 'collection':
                joins += ' JOIN json_each(:members) AS membership ON membership.value = e.id'
                params['members'] = json.dumps(listing['episodes'])
                order = 'CAST(membership.key AS INTEGER)'
            else:
                params['minimum'] = listing['minimum'] * 60
                filters.append('e.duration >= :minimum')
                if listing['category']:
                    params['category'] = listing['category']
                    filters.append("""EXISTS (SELECT 1 FROM show_metadata, json_each(categories)
                        WHERE show_metadata.id = e.show_id AND value = :category)""")
                if listing['unplayed']:
                    params['finished'] = json.dumps([identifier for identifier, value in state['progress'].items() if value.get('finished')])
                    filters.append('e.id NOT IN (SELECT value FROM json_each(:finished))')
                if listing['sort'] == 'oldest':
                    order = 'e.published IS NULL, e.published ASC, e.id'
                elif listing['sort'] == 'longest':
                    order = 'e.duration DESC, e.published DESC, e.id'
        with self.db() as db:
            rows = db.execute(f"""SELECT {columns} FROM episodes e {joins}
                                   WHERE {' AND '.join(filters)}
                                   ORDER BY {order} LIMIT :limit OFFSET :offset""", params).fetchall()
        return [(shows[row['show_id']], json.loads(row['body']), row['excerpt']) for row in rows[:limit]], len(rows) > limit

    def categories(self, shows):
        with self.db() as db:
            rows = db.execute("""SELECT DISTINCT value FROM show_metadata, json_each(categories)
                                 WHERE show_metadata.id IN (SELECT value FROM json_each(?)) ORDER BY value COLLATE NOCASE""",
                              (json.dumps([s['id'] for s in shows]),)).fetchall()
        return [row[0] for row in rows]


def transcript_text(raw, kind):
    value = raw.decode('utf-8-sig', errors='replace')
    if kind == 'application/json':
        data = json.loads(value)
        segments = data.get('segments', []) if isinstance(data, dict) else []
        value = ' '.join(entry['body'] for entry in segments
                         if isinstance(entry, dict) and isinstance(entry.get('body'), str))
    elif kind in ('text/vtt', 'application/x-subrip'):
        lines = value.replace('\r\n', '\n').split('\n\n')
        value = ' '.join(block.split('\n', 1)[1] for block in
                         (part[part.index('-->'):] for part in lines if '-->' in part) if '\n' in block)
    return feeds.clean(value)[:240_000]


def parse_opml(raw, max_size=1_000_000):
    if len(raw) > max_size or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw.replace(b'\0', b''), re.I):
        raise ValueError('Use an OPML file under 1 MB without XML entities.')
    root = XML.fromstring(raw)
    if feeds.localname(root.tag) != 'opml':
        raise ValueError('Choose an OPML file.')
    rows = {}
    for node in root.iter():
        if feeds.localname(node.tag) != 'outline' or not node.get('xmlUrl'):
            continue
        address = podcasts.public_url(node.get('xmlUrl').strip())
        rows.setdefault(address, {'url': address, 'title': (node.get('title') or node.get('text') or urlsplit(address).hostname)[:400]})
        if len(rows) > 500:
            raise ValueError('An OPML import can contain up to 500 different feeds.')
    return list(rows.values())


def playlist_payload(body, existing=None):
    if not isinstance(body, dict):
        raise ValueError('Invalid list.')
    name = body.get('name', '')
    kind = body.get('kind', '')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or kind not in ('smart', 'collection'):
        raise ValueError('Enter a name and choose a list type.')
    category = body.get('category', '')
    query = body.get('query', '')
    minimum = body.get('minimum', 0)
    shows = body.get('shows', [])
    sort = body.get('sort', 'newest')
    if (not isinstance(category, str) or len(category) > 150 or not isinstance(query, str) or len(query) > 200
            or type(minimum) is not int or not 0 <= minimum <= 1440 or not isinstance(shows, list) or len(shows) > 500
            or not all(isinstance(s, str) and podcasts.ID.fullmatch(s) for s in shows)
            or sort not in ('newest', 'oldest', 'longest') or type(body.get('unplayed', False)) is not bool):
        raise ValueError('Invalid playlist filters.')
    return {'name': name.strip(), 'kind': kind, 'query': query.strip(), 'category': category,
            'minimum': minimum, 'unplayed': body.get('unplayed', False), 'sort': sort, 'shows': list(dict.fromkeys(shows)),
            'episodes': (existing or {}).get('episodes', [])}
