"""Revocable gPodder and Nextcloud Sync credentials for one Ondier installation."""

from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
import os
import re
import secrets
import sqlite3
import time


class SyncDevices:
    def __init__(self, directory, *, allow_legacy=False):
        self.path = Path(directory) / 'sync-devices.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    digest BLOB UNIQUE NOT NULL, created INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1), legacy INTEGER NOT NULL
                );
            """)
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (int(allow_legacy),))

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            yield connection

    @staticmethod
    def validate_name(name):
        name = name.strip() if isinstance(name, str) else ''
        if not 1 <= len(name) <= 80 or not name.isprintable():
            raise ValueError('Enter a device name of 1 to 80 characters.')
        return name

    def legacy_enabled(self):
        with self.db() as db:
            return bool(db.execute('SELECT legacy FROM settings WHERE id = 1').fetchone()[0])

    def disable_legacy(self):
        with self.db() as db:
            db.execute('UPDATE settings SET legacy = 0 WHERE id = 1')

    def list(self):
        with self.db() as db:
            return [dict(zip(('id', 'name', 'created'), row)) for row in db.execute(
                'SELECT id, name, created FROM devices ORDER BY created DESC, rowid DESC')]

    def issue(self, name):
        name = self.validate_name(name)
        token = 'ods_' + secrets.token_urlsafe(32)
        identifier, now = secrets.token_hex(16), int(time.time())
        with self.db() as db:
            db.execute('INSERT INTO devices VALUES (?, ?, ?, ?)',
                       (identifier, name, sha256(token.encode()).digest(), now))
        return {'id': identifier, 'name': name, 'created': now, 'password': token}

    def authenticate(self, token):
        if not re.fullmatch(r'ods_[A-Za-z0-9_-]{43}', token):
            return False
        with self.db() as db:
            return db.execute('SELECT 1 FROM devices WHERE digest = ?',
                              (sha256(token.encode()).digest(),)).fetchone() is not None

    def revoke(self, identifier):
        with self.db() as db:
            return db.execute('DELETE FROM devices WHERE id = ?', (identifier,)).rowcount == 1
