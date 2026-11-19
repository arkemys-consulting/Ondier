"""Exercise sync device enrollment and revocation through the HTTP routes."""

from base64 import b64encode
from contextlib import closing
from http.client import HTTPConnection
import json
import os
import secrets
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

os.environ['ONDIER_ORIGIN'] = 'https://ondier.test'
os.environ['ONDIER_OWNER'] = 'owner@example.test'
os.environ['ONDIER_ACCESS_KEY'] = secrets.token_hex(32)

import server
from accounts import Accounts
from sync_devices import SyncDevices


class SyncDeviceTest(unittest.TestCase):
    def test_devices_revoke_basic_cookie_and_nextcloud_independently(self):
        with TemporaryDirectory() as directory:
            self.assertFalse(server.SYNC_DEVICES.legacy_enabled())
            accounts = Accounts(directory)
            _, session = accounts.create('fixture', secrets.token_urlsafe(18))
            key = accounts.new_key('Roan', 0)
            devices = SyncDevices(directory, allow_legacy=accounts.initialized())
            with patch.object(server, 'ACCOUNTS', accounts), patch.object(server, 'SYNC_DEVICES', devices):
                self.exercise(session, key['key'], devices, directory)

    def exercise(self, session, key, devices, directory):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        running = threading.Thread(target=http.serve_forever, daemon=True)
        running.start()
        self.addCleanup(running.join, 5)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        account = {'Cookie': f'{server.ACCOUNT_COOKIE}={session}', 'Origin': server.ORIGIN}
        device_path = '/api/account/sync-devices'
        sync_path = '/api/2/devices/ondier.json'
        login_path = '/api/2/auth/ondier/login.json'

        def basic(password):
            return {'Authorization': 'Basic ' + b64encode(('ondier:' + password).encode()).decode()}

        def request(path, expected, *, method='GET', headers=None, data=None):
            options = dict(headers or {})
            if isinstance(data, dict):
                data = json.dumps(data)
                options['Content-Type'] = 'application/json'
            with closing(HTTPConnection(*http.server_address, timeout=5)) as connection:
                connection.request(method, path, data, options)
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, expected, path)
                if 'application/json' in response.getheader('Content-Type', ''):
                    body = json.loads(body)
                return body, response.getheader('Set-Cookie')

        request(device_path, 401)
        request(device_path, 401, headers={'Authorization': 'Bearer ' + key})
        request(device_path, 403, method='POST', headers={'Cookie': account['Cookie']}, data={'name': 'Phone'})
        legacy = basic(server.sync_password())
        request(sync_path, 200, headers=legacy)
        _, legacy_cookie = request(login_path, 200, method='POST', headers=legacy)
        legacy_session = {'Cookie': legacy_cookie.split(';')[0]}
        first, _ = request(device_path, 201, method='POST', headers=account, data={'name': 'Phone'})
        second, _ = request(device_path, 201, method='POST', headers=account, data={'name': 'Tablet'})
        self.assertNotEqual(first['password'], second['password'])
        request(sync_path, 200, headers=basic(first['password']))
        _, cookie = request(login_path, 200, method='POST', headers=basic(first['password']))
        session = {'Cookie': cookie.split(';')[0]}
        request(sync_path, 200, headers=session)
        listing, _ = request(device_path, 200, headers=account)
        self.assertTrue(listing['legacyEnabled'])
        self.assertEqual({row['name'] for row in listing['devices']}, {'Phone', 'Tablet'})
        self.assertNotIn(first['password'], json.dumps(listing))
        request(device_path + '/' + first['id'], 200, method='DELETE', headers=account)
        request(sync_path, 401, headers=basic(first['password']))
        request(sync_path, 401, headers=session)
        request(sync_path, 200, headers=basic(second['password']))

        flow, _ = request('/index.php/login/v2', 200, method='POST')
        authorize = urlsplit(flow['login']).path
        poll = urlsplit(flow['poll']['endpoint']).path
        form = urlencode({'token': flow['poll']['token']})
        request(poll, 404, method='POST', data=form)
        request(authorize, 200, method='GET', headers=account)
        request(authorize, 200, method='POST', headers=account, data=urlencode({'name': 'Nextcloud reader'}))
        authorized, _ = request(poll, 200, method='POST', data=form)
        request(poll, 404, method='POST', data=form)
        nextcloud = basic(authorized['appPassword'])
        request(sync_path, 200, headers=nextcloud)
        listing, _ = request(device_path, 200, headers=account)
        third = next(row for row in listing['devices'] if row['name'] == 'Nextcloud reader')
        request(device_path + '/' + third['id'], 200, method='DELETE', headers=account)
        request(sync_path, 401, headers=nextcloud)
        request(device_path + '/legacy', 200, method='DELETE', headers=account)
        request(sync_path, 401, headers=legacy)
        request(sync_path, 401, headers=legacy_session)
        request(sync_path, 200, headers=basic(second['password']))
        reopened = SyncDevices(directory, allow_legacy=True)
        self.assertFalse(reopened.legacy_enabled())
        self.assertFalse(reopened.authenticate(first['password']))
        self.assertTrue(reopened.authenticate(second['password']))
        self.assertNotIn(second['password'].encode(), devices.path.read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
