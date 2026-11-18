"""Password login and revocable credentials for Ondier's shared library."""
from contextlib import contextmanager
from hashlib import scrypt, sha256
from pathlib import Path
import hmac
import os
import secrets
import sqlite3
import time


class Accounts:
    session_ttl = 30 * 24 * 3600

    def __init__(self, directory):
        self.path = Path(directory) / 'accounts.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owner (
                    id INTEGER PRIMARY KEY CHECK(id = 1), username TEXT NOT NULL,
                    salt BLOB NOT NULL, password BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (digest BLOB PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest BLOB UNIQUE NOT NULL,
                    created INTEGER NOT NULL, expires INTEGER
                );
                CREATE TABLE IF NOT EXISTS login_limit (
                    id INTEGER PRIMARY KEY CHECK(id = 1), attempts INTEGER NOT NULL, reset INTEGER NOT NULL
                );
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def password_hash(password, salt):
        return scrypt(password.encode(), salt=salt, n=2**16, r=8, p=1, maxmem=128 * 1024**2)

    @staticmethod
    def digest(value):
        return sha256(value.encode()).digest()

    @staticmethod
    def identity(username):
        return {'username': username}

    def owner(self, db):
        return db.execute('SELECT username, salt, password FROM owner WHERE id = 1').fetchone()

    def initialized(self):
        with self.db() as db:
            return self.owner(db) is not None

    def create(self, username, password):
        username = username.strip() if isinstance(username, str) else ''
        if not 1 <= len(username) <= 80 or not username.isprintable():
            raise ValueError('Enter a username of 1 to 80 characters.')
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            raise ValueError('The password must be 8 to 256 characters long.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.owner(db):
                return None
            salt = secrets.token_bytes(16)
            db.execute('INSERT INTO owner VALUES (1, ?, ?, ?)', (username, salt, self.password_hash(password, salt)))
            return self.issue_session(db, username)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 256:
            return 401, None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = self.owner(db)
            if not owner:
                return 401, None
            now = int(time.time())
            limit = db.execute('SELECT attempts, reset FROM login_limit WHERE id = 1').fetchone()
            attempts, reset = limit if limit and limit[1] > now else (0, now + 300)
            if attempts >= 10:
                return 429, None
            name, salt, expected = owner
            matches = hmac.compare_digest(self.password_hash(password, salt), expected)
            if not matches or not hmac.compare_digest(username.strip().casefold().encode(), name.casefold().encode()):
                db.execute('INSERT OR REPLACE INTO login_limit VALUES (1, ?, ?)', (attempts + 1, reset))
                return 401, None
            db.execute('DELETE FROM login_limit')
            return 200, self.issue_session(db, name)

    def issue_session(self, db, username):
        now = int(time.time())
        token = secrets.token_urlsafe(32)
        db.execute('DELETE FROM sessions WHERE expires <= ?', (now,))
        db.execute('INSERT INTO sessions VALUES (?, ?)', (self.digest(token), now + self.session_ttl))
        return self.identity(username), token

    def authenticate(self, token, api=False):
        if not token or len(token) > 100 or (api and not token.startswith('ond_')):
            return None
        with self.db() as db:
            owner = self.owner(db)
            if not owner:
                return None
            table = 'api_keys' if api else 'sessions'
            row = db.execute(f'SELECT 1 FROM {table} WHERE digest = ? AND (expires IS NULL OR expires > ?)',
                             (self.digest(token), int(time.time()))).fetchone()
            return self.identity(owner[0]) if row else None

    def logout(self, token):
        if token:
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE digest = ?', (self.digest(token),))

    def keys(self):
        with self.db() as db:
            return [dict(zip(('id', 'name', 'created', 'expires'), row)) for row in db.execute(
                'SELECT id, name, created, expires FROM api_keys ORDER BY created DESC, rowid DESC')]

    def new_key(self, name, days):
        name = name.strip() if isinstance(name, str) else ''
        if not 1 <= len(name) <= 80 or not name.isprintable():
            raise ValueError('Enter a name of 1 to 80 characters.')
        if type(days) is not int or days not in (0, 30, 90, 365):
            raise ValueError('Choose a supported expiration.')
        now = int(time.time())
        record = {'id': secrets.token_hex(16), 'name': name, 'created': now,
                  'expires': now + days * 86400 if days else None}
        token = 'ond_' + secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('INSERT INTO api_keys VALUES (?, ?, ?, ?, ?)',
                       (record['id'], name, self.digest(token), now, record['expires']))
        return {**record, 'key': token}

    def revoke(self, identifier):
        with self.db() as db:
            return db.execute('DELETE FROM api_keys WHERE id = ?', (identifier,)).rowcount == 1
