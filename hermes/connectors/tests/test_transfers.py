import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from pantheon_connectors import transfers
from pantheon_connectors.client import Client, APIError
from pantheon_connectors.onboarding import dispatch, TOOLS
from pantheon_connectors.worker import Blocked


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.content = b'private-payload\x00\xff'
        self.metadata = dict(id='transfer-1', size=len(self.content), sha256=hashlib.sha256(self.content).hexdigest(), state='ready')
        self.client = Mock()
        self.client.request.side_effect = lambda path, *args: {"temporary_transfers": {"available": True, "protocol": "pantheon.transfers.v1"}} if path == "/v1/chat" else self.metadata
        self.client.transfer_bytes.return_value = self.content
        self.service = Mock()
        self.service.member.return_value = (None, None, self.client)
        self.binding = dict(runtime='codex')
        self.path = str(self.root / 'received')

    def call(self, operation, **args):
        return dispatch(self.service, 'pantheon_transfer_' + operation,
                        dict(connection='connection', **args), self.binding, lambda _: None, lambda: {})

    def test_fetch_is_private_durable_and_never_acknowledges(self):
        with patch.object(transfers.os, 'fsync', wraps=os.fsync) as sync:
            result = self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertEqual(Path(self.path).read_bytes(), self.content)
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)
        self.assertEqual(sync.call_count, 2)
        self.assertNotIn('private-payload', json.dumps(result))
        self.client.request.assert_any_call('/v1/transfers/transfer-1')
        self.assertEqual(self.client.request.call_count, 2)
        self.assertEqual(list(self.root.iterdir()), [Path(self.path)])
        self.call('ack', transfer_id='transfer-1', path=self.path)
        self.client.request.assert_called_with('/v1/transfers/transfer-1/ack', dict(size=len(self.content), sha256=self.metadata['sha256'], durable=True))

    def test_integrity_failure_leaves_no_file_or_ack(self):
        self.client.transfer_bytes.return_value = b'bad'
        with self.assertRaises(Blocked): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(self.client.request.call_count, 2)

    def test_existing_dirty_file_and_broken_link_never_replaced(self):
        Path(self.path).write_bytes(b'dirty')
        with self.assertRaises(Blocked): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertEqual(Path(self.path).read_bytes(), b'dirty')
        Path(self.path).unlink()
        Path(self.path).symlink_to(self.root / 'missing')
        with self.assertRaises(Blocked): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertTrue(Path(self.path).is_symlink())

    def test_symlink_ancestor_and_source_are_refused(self):
        (self.root / 'link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError): transfers.safe_save(str(self.root / 'link' / 'received'), self.content)
        Path(self.path).write_bytes(self.content)
        (self.root / 'source').symlink_to(self.path)
        with self.assertRaises(OSError): transfers.read_file(str(self.root / 'source'))

    def test_shared_writable_destination_is_refused(self):
        self.root.chmod(0o777)
        try:
            with self.assertRaises(Blocked): transfers.safe_save(self.path, self.content)
        finally:
            self.root.chmod(0o700)
        self.assertFalse(Path(self.path).exists())

    def test_interrupted_download_can_retry_without_implicit_receipt(self):
        self.client.transfer_bytes.side_effect = [APIError(0), self.content]
        with self.assertRaises(APIError): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertFalse(Path(self.path).exists())
        self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertEqual(Path(self.path).read_bytes(), self.content)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list],
                         ['/v1/chat', '/v1/transfers/transfer-1', '/v1/chat', '/v1/transfers/transfer-1'])

    def test_exclusive_publication_preserves_racing_destination(self):
        original = os.link
        def race(src, dst, **kwargs):
            Path(self.path).write_bytes(b'racer')
            return original(src, dst, **kwargs)
        with patch.object(transfers.os, 'link', side_effect=race):
            with self.assertRaises(FileExistsError): transfers.safe_save(self.path, self.content)
        self.assertEqual(Path(self.path).read_bytes(), b'racer')
        self.assertEqual(list(self.root.iterdir()), [Path(self.path)])

    def test_fsync_failure_never_reports_success_or_ack(self):
        with patch.object(transfers.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.client.request.call_count, 2)

    def test_publication_then_directory_fsync_failure_recovers_only_by_explicit_ack(self):
        original = os.fsync
        calls = 0
        def fail_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('directory fsync interrupted after publication')
            return original(fd)
        with patch.object(transfers.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertEqual(Path(self.path).read_bytes(), self.content)
        self.assertEqual(list(self.root.iterdir()), [Path(self.path)])
        self.client.request.assert_any_call('/v1/transfers/transfer-1')
        self.assertEqual(self.client.request.call_count, 2)
        with self.assertRaisesRegex(Blocked, 'explicitly call pantheon_transfer_ack'):
            self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertFalse(any(call.args[0].endswith('/ack') for call in self.client.request.call_args_list))
        self.call('inspect', transfer_id='transfer-1')
        with patch.object(transfers.os, 'fsync', wraps=os.fsync) as sync:
            self.call('ack', transfer_id='transfer-1', path=self.path)
        self.assertEqual(sync.call_count, 2)
        self.client.request.assert_called_with('/v1/transfers/transfer-1/ack',
            dict(size=len(self.content), sha256=self.metadata['sha256'], durable=True))

    def test_lost_fetch_result_preserves_file_until_explicit_ack(self):
        # The local save succeeds but the caller never sees its tool result.
        self.call('fetch', transfer_id='transfer-1', path=self.path)
        with self.assertRaises(Blocked): self.call('fetch', transfer_id='transfer-1', path=self.path)
        self.assertEqual(Path(self.path).read_bytes(), self.content)
        self.assertFalse(any(call.args[0].endswith('/ack') for call in self.client.request.call_args_list))
        self.call('inspect', transfer_id='transfer-1')
        self.call('ack', transfer_id='transfer-1', path=self.path)
        self.assertTrue(self.client.request.call_args.args[0].endswith('/ack'))

    def test_lost_ack_response_retries_identical_receipt_after_reverification(self):
        Path(self.path).write_bytes(self.content)
        acknowledged = dict(self.metadata, state='acknowledged')
        # First ACK committed remotely but its response was lost. Inspection
        # remains metadata-only and the same receipt can be explicitly retried.
        self.client.request.side_effect = [self.metadata, APIError(0), acknowledged, acknowledged]
        with self.assertRaises(APIError): self.call('ack', transfer_id='transfer-1', path=self.path)
        with patch.object(transfers.os, 'fsync', wraps=os.fsync) as sync:
            result = self.call('ack', transfer_id='transfer-1', path=self.path)
        self.assertEqual(sync.call_count, 2)
        receipts = [call for call in self.client.request.call_args_list if call.args[0].endswith('/ack')]
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(result['untrusted_transfer_metadata']['state'], 'acknowledged')
        self.client.transfer_bytes.assert_not_called()

    def test_explicit_ack_refuses_changed_file(self):
        Path(self.path).write_bytes(b'changed')
        with self.assertRaises(Blocked): self.call('ack', transfer_id='transfer-1', path=self.path)
        self.assertEqual(self.client.request.call_count, 1)

    def test_ack_refuses_file_removed_replaced_or_changed_during_fsync(self):
        original_sync = os.fsync
        for mutation in ('unlink', 'replace', 'modify', 'symlink'):
            with self.subTest(mutation=mutation):
                path = self.root / mutation
                path.write_bytes(self.content)
                mutated = False
                def sync(fd):
                    nonlocal mutated
                    if not mutated:
                        mutated = True
                        if mutation == 'modify':
                            path.write_bytes(b'x' * len(self.content))
                        else:
                            path.unlink()
                            if mutation == 'replace':
                                path.write_bytes(self.content)
                            elif mutation == 'symlink':
                                target = self.root / 'target'
                                target.write_bytes(self.content)
                                path.symlink_to(target)
                    return original_sync(fd)
                self.client.request.reset_mock()
                with patch.object(transfers.os, 'fsync', side_effect=sync):
                    with self.assertRaises(Blocked):
                        self.call('ack', transfer_id='transfer-1', path=str(path))
                self.client.request.assert_called_once_with('/v1/transfers/transfer-1')

    def test_upload_retry_preserves_exact_reservation(self):
        Path(self.path).write_bytes(self.content)
        self.client.transfer_bytes.side_effect = [APIError(0), self.metadata]
        args = dict(path=self.path, recipient='peer', name='file.bin', reason='review', key='original', base_revision='base-hash')
        with self.assertRaises(APIError): self.call('send', **args)
        result = self.call('send', **args)
        reservations = [call for call in self.client.request.call_args_list if call.args[0] == '/v1/transfers']
        self.assertEqual(len(reservations), 2)
        self.assertEqual(reservations[0], reservations[1])
        self.assertNotIn('private-payload', json.dumps(result))
        self.client.transfer_bytes.assert_called_with('/v1/transfers/transfer-1/content', self.content)

    def test_bounds_paths_and_runtime(self):
        for content in (b'', b'x' * (transfers.MAX_BYTES + 1)):
            Path(self.path).write_bytes(content)
            with self.assertRaises(Blocked): transfers.read_file(self.path)
        for path in ('relative', '/a/../b', '/a//b'):
            with self.assertRaises(Blocked): transfers.safe_save(path, b'x')
        self.binding['runtime'] = 'claude'
        with self.assertRaises(Blocked): self.call('inspect', transfer_id='transfer-1')
        self.service.member.assert_not_called()

    def test_registration_and_argument_validation(self):
        from pantheon_connectors.claude_mcp import TOOLS as CLAUDE
        self.assertTrue(transfers.NAMES <= {t['name'] for t in TOOLS})
        self.assertFalse(transfers.NAMES & {t['name'] for t in CLAUDE})
        with self.assertRaises(Blocked): self.call('fetch', transfer_id='t', path=self.path, durable=True)
        self.binding['runtime'] = 'hermes'
        self.call('inspect', transfer_id='t')

    def test_raw_download_is_bounded_and_blocks_redirects(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'x' * (transfers.MAX_BYTES + 1)
        opener = Mock()
        opener.open.return_value = response
        with patch('urllib.request.build_opener', return_value=opener) as build:
            with self.assertRaises(Blocked): Client('http://127.0.0.1:1', 'secret').transfer_bytes('/v1/transfers/t/content')
        response.read.assert_called_once_with(transfers.MAX_BYTES + 1)
        self.assertIsNone(build.call_args.args[0]().redirect_request(None, None, None, None, None, None))

    def test_scoped_discovery_blocks_send_and_fetch_before_file_access(self):
        for availability in ({}, {'available': True, 'protocol': 'unknown'}, {'available': False, 'protocol': 'pantheon.transfers.v1'}):
            self.client.request.side_effect = lambda path, *args: {'temporary_transfers': availability}
            for operation, args in [('send',dict(path=self.path, recipient='b', name='file', reason='review', key='same')), ('fetch',dict(transfer_id='transfer-1',path=self.path))]:
                with self.subTest(availability=availability,operation=operation):
                    with patch.object(transfers,'read_file') as read, patch.object(transfers,'safe_save') as save:
                        with self.assertRaisesRegex(Blocked,'unavailable for this chat'): self.call(operation,**args)
                        read.assert_not_called();save.assert_not_called()
        self.client.transfer_bytes.assert_not_called()

    def test_off_receipt_does_not_depend_on_admission_discovery(self):
        Path(self.path).write_bytes(self.content)
        self.client.request.side_effect = lambda path, *args: (_ for _ in ()).throw(AssertionError('ACK must not require admission')) if path == '/v1/chat' else self.metadata
        self.call('inspect',transfer_id='transfer-1')
        self.call('ack',transfer_id='transfer-1',path=self.path)
        self.client.transfer_bytes.assert_not_called()
