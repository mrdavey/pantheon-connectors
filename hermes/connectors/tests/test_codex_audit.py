import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pantheon_connectors.codex_audit import Audit, failure
from pantheon_connectors.codex_mcp import Server
from pantheon_connectors.worker import Blocked


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'private'
        self.audit = Audit(self.root/'setup.json')

    def rows(self):
        return [json.loads(x) for x in self.audit.path.read_text().splitlines()]

    def test_allowlist_redacts_arguments_errors_and_even_identifier_values(self):
        secret = 'a'*32
        self.audit.emit('tool_requested', tool='pantheon_join', connection=secret,
                        message_ids=[secret,'an_actual_Message-ID_22'], token=secret, text='private message', error='secret traceback',
                        url='https://private.example', status=['bad'], source='outside.py')
        text = self.audit.path.read_text()
        for value in (secret, 'private message', 'secret traceback', 'private.example', 'outside.py'):
            self.assertNotIn(value, text)
        row = self.rows()[0]
        self.assertEqual(row['schema'], 1)
        self.assertEqual(len(row['connection_sha256']), 64)
        self.assertEqual(len(row['message_ids_sha256']), 2)
        self.assertNotIn('an_actual_Message-ID_22', text)
        self.assertEqual(self.audit.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(failure(ValueError(secret)), {'reason':'state_error'})

    def test_concurrent_records_remain_complete_and_rotation_is_bounded(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:self.audit.emit('receiver_status', status='idle'), range(40)))
        self.assertEqual(len(self.rows()), 40)
        self.audit.max_bytes = 1000
        for _ in range(12): self.audit.emit('receiver_status', status='idle')
        current = self.audit.path
        previous = current.with_suffix('.jsonl.1')
        self.assertTrue(previous.exists())
        self.assertLessEqual(current.stat().st_size, 1000)
        self.assertLessEqual(previous.stat().st_size, 1000)
        self.assertTrue(self.rows())

    def test_symlinks_modes_and_hardlinks_fail_closed_without_writing_target(self):
        self.root.mkdir(mode=0o700)
        outside = Path(self.temp.name)/'outside'
        outside.write_text('preserve')
        self.audit.path.symlink_to(outside)
        with self.assertRaises(Blocked):self.audit.emit('tool_requested')
        self.assertEqual(outside.read_text(), 'preserve')
        self.audit.path.unlink()
        self.audit.path.write_text('');self.audit.path.chmod(0o644)
        with self.assertRaises(Blocked):self.audit.emit('tool_requested')
        self.audit.path.chmod(0o600)
        os.link(self.audit.path, self.root/'hardlink')
        with self.assertRaises(Blocked):self.audit.emit('tool_requested')

    def test_audit_failure_prevents_tool_operation_and_inert_startup_writes_nothing(self):
        server = Server(self.root/'setup.json')
        server.handle('initialize', {})
        server.handle('tools/list', {})
        self.assertFalse(self.root.exists())
        with patch.object(server.audit, 'emit', side_effect=Blocked('unavailable')), patch.object(server, '_handle') as operation:
            with self.assertRaises(Blocked):server.handle('tools/call', {'name':'pantheon_join'})
            operation.assert_not_called()
        server.close()

    def test_failed_native_identity_is_correlated_without_raw_error_or_payload(self):
        server = Server(self.root/'setup.json')
        with self.assertRaises(Blocked):
            server.handle('tools/call', dict(name='pantheon_join', arguments={'link':'secret invitation'}))
        server.close()
        rows = self.rows()
        self.assertEqual([x['event'] for x in rows], ['tool_requested','tool_failed','plugin_closed'])
        self.assertEqual(rows[0]['request_id_sha256'], rows[1]['request_id_sha256'])
        self.assertEqual(rows[1]['reason'], 'guard_failed')
        self.assertNotIn('secret invitation', self.audit.path.read_text())

    def test_receive_and_ack_share_hashed_message_correlation(self):
        server = Server(self.root/'setup.json')
        identity = 'an_actual_Message-ID_22'
        value = {'structuredContent': {'messages':[{'untrusted_member_content':{'message':{'id':identity,'text':'PRIVATE BODY'}}}]}}
        with patch.object(server, '_handle', return_value=value):
            server.handle('tools/call', {'name':'pantheon_receive', 'arguments':{}})
        with patch.object(server, '_handle', return_value={'structuredContent':{}}):
            server.handle('tools/call', {'name':'pantheon_ack', 'arguments':{'message_ids':[identity]}})
        server.close()
        rows = self.rows()
        receive = next(x for x in rows if x['event']=='tool_completed' and x['tool']=='pantheon_receive')
        ack = next(x for x in rows if x['event']=='tool_completed' and x['tool']=='pantheon_ack')
        self.assertEqual(receive['message_ids_sha256'], ack['message_ids_sha256'])
        self.assertEqual(ack['ack_count'], 1)
        self.assertNotIn('PRIVATE BODY', self.audit.path.read_text())

class LocalFileDiagnosticTests(unittest.TestCase):
    def test_file_errors_are_distinct_and_paths_are_never_returned(self):
        import errno
        for error,reason in [(FileNotFoundError(errno.ENOENT,'private-token','/private/token'), 'local_file_missing'),(PermissionError(errno.EACCES,'private-token','/private/token'),'local_file_access_denied'),(OSError(errno.ELOOP,'private-token','/private/token'),'local_symlink_refused')]:
            self.assertEqual(failure(error),{'reason':reason})
            self.assertNotIn('private-token',json.dumps(failure(error)))
