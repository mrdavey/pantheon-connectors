"""Opt-in Python/Go wire tests. Synthetic loopback only; no actual-agent evidence."""
import hashlib
import http.client
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from urllib.parse import urlsplit

from pantheon_connectors import transfers
from pantheon_connectors.client import APIError, Client
from pantheon_connectors.worker import Blocked


class SyntheticTransferService:
    """Only substitutes native binding/consent; every client operation is real HTTP."""
    def __init__(self, client, binding):
        self.client, self.binding = client, binding

    def member(self, connection, binding):
        if connection != 'synthetic' or binding != self.binding:
            raise AssertionError('unexpected synthetic binding')
        return None, None, self.client

    def require_allowance(self):
        pass


@unittest.skipUnless(os.environ.get('PANTHEON_TRANSFER_HTTP_TEST_URL'),
                     'requires explicit disposable loopback transfer server opt-in')
class TransferHTTP(unittest.TestCase):
    def setUp(self):
        # This opt-in is deliberately independent of the general HTTP test server.
        self.url = os.environ['PANTHEON_TRANSFER_HTTP_TEST_URL']
        parsed = urlsplit(self.url)
        self.assertEqual(parsed.scheme, 'http')
        self.assertEqual(parsed.hostname, '127.0.0.1')
        self.assertIsNotNone(parsed.port)
        self.assertFalse(parsed.username or parsed.password or parsed.query or parsed.fragment)
        self.assertIn(parsed.path, ('', '/'))
        self.address = (parsed.hostname, parsed.port)
        admin_secret = secrets.token_urlsafe(32)
        self.admin = Client(self.url, admin_secret)
        self.admin.request('/v1/chats', dict(
            secret=admin_secret, key=secrets.token_urlsafe(24), title='Transfer wire test',
            purpose='Disposable synthetic local transfer validation', hours=4,
            slots=[dict(id='a', runtime='codex', required=True),
                   dict(id='b', runtime='hermes', required=True)]))
        self.addCleanup(self.admin.command, 'end')
        self.bindings, self.clients = {}, {}
        for member, runtime, surface in [('a', 'codex', 'app-server'),
                                         ('b', 'hermes', 'native-adapter')]:
            invitation, credential = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.admin.command('invite', target=member, secret=invitation, hours=1)
            binding = dict(runtime=runtime, version='synthetic', surface=surface,
                           profile='synthetic-' + member, session=secrets.token_urlsafe(16),
                           return_route='synthetic-' + member)
            Client(self.url, '').request('/v1/redeem', dict(
                invitation=invitation, secret=credential, name=member,
                binding=binding, deliberate=True))
            self.bindings[member], self.clients[member] = binding, Client(self.url, credential)
        for client in self.clients.values():
            for entry in client.request('/v1/pending'):
                client.ack([entry['message']['id']], key=secrets.token_urlsafe(24))
                if entry['message']['kind'] == 'probe':
                    client.command('ready', text=entry['message']['text'])
        self.assertEqual(self.admin.request('/v1/chat')['chat']['state'], 'active')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def tool(self, member, operation, **arguments):
        return transfers.execute(
            SyntheticTransferService(self.clients[member], self.bindings[member]),
            'pantheon_transfer_' + operation, dict(connection='synthetic', **arguments),
            self.bindings[member])

    def test_maximum_binary_roundtrip_durable_save_and_explicit_ack(self):
        content = (bytes(range(256)) * (transfers.MAX_BYTES // 256 + 1))[:transfers.MAX_BYTES]
        source, destination = self.root / 'source.bin', self.root / 'received.bin'
        source.write_bytes(content)
        arguments = dict(path=str(source), recipient='b', name='artifact.bin',
                         reason='Synthetic lead copy review', key=secrets.token_urlsafe(24),
                         base_revision='synthetic-base')
        sent = self.tool('a', 'send', **arguments)['untrusted_transfer_metadata']
        self.assertEqual(sent['state'], 'ready')
        self.assertEqual(sent['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(sent['size'], transfers.MAX_BYTES)
        self.assertEqual(self.tool('a', 'send', **arguments)['untrusted_transfer_metadata'], sent)
        path = '/v1/transfers/' + sent['id']
        saved = self.tool('b', 'fetch', transfer_id=sent['id'], path=str(destination))
        self.assertTrue(saved['durably_saved'])
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.clients['b'].request(path)['state'], 'ready')
        # Crash/retry before ACK retains the bytes and never overwrites the saved copy.
        self.assertEqual(self.clients['b'].transfer_bytes(path + '/content'), content)
        with self.assertRaises(Blocked):
            self.tool('b', 'fetch', transfer_id=sent['id'], path=str(destination))
        for _ in range(2):
            acknowledged = self.tool('b', 'ack', transfer_id=sent['id'], path=str(destination))
            self.assertEqual(acknowledged['untrusted_transfer_metadata']['state'], 'acknowledged')
        with self.assertRaises(APIError) as error:
            self.clients['b'].transfer_bytes(path + '/content')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(source.read_bytes(), content)
        self.assertEqual(destination.read_bytes(), content)

    def test_chunked_oversize_upload_rejected_then_original_reservation_retries(self):
        content = b'synthetic retry payload\x00\xff'
        metadata = self.clients['a'].request('/v1/transfers', dict(
            key=secrets.token_urlsafe(24), recipient='b', name='retry.bin', reason='Synthetic retry',
            size=len(content), sha256=hashlib.sha256(content).hexdigest()))
        path = '/v1/transfers/' + metadata['id'] + '/content'
        # Bypass the Python client's own cap to exercise Go's actual streamed cap.
        connection = http.client.HTTPConnection(*self.address, timeout=20)
        try:
            connection.request('PUT', path,
                body=iter([b'x' * transfers.MAX_BYTES, b'x']), encode_chunked=True,
                headers={'Authorization': 'Bearer ' + self.clients['a'].credential,
                         'Content-Type': 'application/octet-stream'})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
        with self.assertRaises(APIError) as error:
            self.clients['b'].transfer_bytes(path)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.clients['a'].transfer_bytes(path, content)['state'], 'ready')
        self.assertEqual(self.clients['b'].transfer_bytes(path), content)
        with self.assertRaises(Blocked):
            self.clients['a'].transfer_bytes(path, b'x' * (transfers.MAX_BYTES + 1))
