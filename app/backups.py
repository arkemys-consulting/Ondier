"""Signed Ondier snapshots. Restore with the serving container stopped."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
import argparse
import fcntl
import hmac
import json
import os
import shutil
import sqlite3
import time

import podcasts
from accounts import Accounts
from archive import Archive, DATABASE
from sync_devices import SyncDevices

FILES = ('state.json', 'accounts.sqlite3', DATABASE)
SQLITE_FILES = FILES[1:]
BACKUP_NAMES = 'ondier-????-??-??.zip'


@contextmanager
def runtime_lock(root):
    with (root / '.ondier-runtime.lock').open('ab') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OSError('Stop Ondier before restoring data.') from error
        yield


def digest(file):
    with file.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def snapshot_db(source, destination, *, strip_sessions=False):
    uri = source.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=12)) as old, closing(sqlite3.connect(destination)) as copy:
        old.backup(copy)
        if strip_sessions:
            with copy:
                copy.execute('DELETE FROM sessions')
                copy.execute('DELETE FROM login_limit')
            copy.execute('VACUUM')
    destination.chmod(0o600)


def create(output, root, key):
    root, output = Path(root), Path(output)
    with TemporaryDirectory(dir=root) as temp:
        temp = Path(temp)
        with podcasts.LOCK:
            state = podcasts.load()
            (temp / FILES[0]).write_text(json.dumps(state, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            for name in SQLITE_FILES:
                snapshot_db(root / name, temp / name, strip_sessions=name == 'accounts.sqlite3')
        manifest = json.dumps({'version': 1, 'created': datetime.now(timezone.utc).isoformat(),
                               'files': {name: digest(temp / name) for name in FILES}},
                              sort_keys=True, separators=(',', ':')).encode()
        with ZipFile(temp / 'archive.zip', 'w', compression=ZIP_DEFLATED, compresslevel=4) as archive:
            archive.writestr('manifest.json', manifest)
            archive.writestr('signature', hmac.new(key.encode(), manifest, sha256).hexdigest())
            for name in FILES:
                archive.write(temp / name, name)
        (temp / 'archive.zip').chmod(0o600)
        (temp / 'archive.zip').replace(output)


def extract(archive, temp, key):
    try:
        with ZipFile(archive) as bundle:
            if sorted(bundle.namelist()) != sorted((*FILES, 'manifest.json', 'signature')):
                raise ValueError('Unexpected backup contents.')
            if bundle.getinfo('manifest.json').file_size > 4096 or bundle.getinfo('signature').file_size != 64:
                raise ValueError('Invalid backup manifest.')
            manifest = bundle.read('manifest.json')
            signature = hmac.new(key.encode(), manifest, sha256).hexdigest().encode()
            if not hmac.compare_digest(bundle.read('signature'), signature):
                raise ValueError('This backup was not signed by this Ondier instance.')
            index = json.loads(manifest)
            if index['version'] != 1 or set(index['files']) != set(FILES):
                raise ValueError('Unsupported backup version.')
            for name in FILES:
                path = temp / name
                with bundle.open(name) as source, path.open('xb') as destination:
                    shutil.copyfileobj(source, destination, 128 * 1024)
                path.chmod(0o600)
                if digest(path) != index['files'][name]:
                    raise ValueError('Backup checksum mismatch.')
    except (BadZipFile, KeyError) as error:
        raise ValueError('Invalid Ondier backup.') from error
    state = json.loads((temp / FILES[0]).read_text())
    if not isinstance(state, dict) or not isinstance(state.get('subscriptions'), list) or not isinstance(state.get('progress'), dict):
        raise ValueError('Invalid Ondier state.')
    for name, required in [('accounts.sqlite3', {'owner','sessions','api_keys','login_limit'}),
                           (DATABASE, {'episodes','episode_search','show_metadata'})]:
        with closing(sqlite3.connect((temp / name).as_uri() + '?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required.issubset(tables) or db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Invalid Ondier database.')
    return index


def restore(source, root, key, *, verify=False):
    root = Path(root)
    with TemporaryDirectory(dir=root) as scratch:
        temp = Path(scratch)
        index = extract(source, temp, key)
        if verify:
            return index
        Accounts(root)
        Archive(root)
        # Sync grants are local to the installation. A restore must never revive a revoked one.
        SyncDevices(root)
        folder = root / 'backups'
        folder.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        rollback = folder / f'before-restore-{stamp}.zip'
        create(rollback, root, key)
        try:
            install(temp, root)
        except OSError:
            with TemporaryDirectory(dir=root) as recovery:
                previous = Path(recovery)
                extract(rollback, previous, key)
                install(previous, root)
            raise
        return index


def install(staged, root):
    for name in SQLITE_FILES:
        with closing(sqlite3.connect(root / name, timeout=10)) as db:
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                raise OSError('Stop Ondier before restoring data.')
        for suffix in ('-wal', '-shm'):
            (root / (name + suffix)).unlink(missing_ok=True)
    for name in FILES:
        (staged / name).replace(root / name)


def schedule(root, key, indexed):
    indexed.wait(timeout=600)
    folder = root / 'backups'
    while True:
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            name = f'ondier-{datetime.now(timezone.utc).date().isoformat()}.zip'
            if not (folder / name).exists():
                create(folder / name, root, key)
                for old in sorted(folder.glob(BACKUP_NAMES), reverse=True)[7:]:
                    old.unlink()
        except (OSError, sqlite3.Error) as error:
            print(f'Ondier backup failed: {type(error).__name__}', flush=True)
        time.sleep(3600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify', 'restore'])
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    key = os.environ['ONDIER_ACCESS_KEY']
    if args.action == 'verify':
        result = restore(args.backup, podcasts.ROOT, key, verify=True)
    else:
        with runtime_lock(podcasts.ROOT):
            result = restore(args.backup, podcasts.ROOT, key)
    print(f'Backup {result["created"]} ' + ('verified.' if args.action == 'verify' else 'restored.'))


if __name__ == '__main__':
    main()
