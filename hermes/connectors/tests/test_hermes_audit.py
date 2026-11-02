"""Local metadata audit: privacy, correlation, bounded retention and degraded IO."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pantheon_connectors.hermes_audit import Audit, fingerprint
from pantheon_connectors.state import save_private
import test_hermes_notification_contract as notification
import test_hermes_peer_tools as peers


class AuditFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.setup = self.root/'setup.json'
        save_private(self.setup, {})
        self.audit = Audit(self.setup)

    def test_allowlisted_metadata_never_contains_raw_payloads_or_identity(self):
        secret = 'PRIVATE-TOKEN-AND-PEER-CANARY'
        self.audit.record('tool_finished', operation='pantheon_post', command=secret,
            connection=secret, session=secret, request=secret, key=secret, notice_token=secret,
            message_ids=[secret]*100, outcome=secret, error=ValueError(secret),
            limits={'private':secret,'notice_limit':8})
        text = self.audit.path.read_text(); row=json.loads(text)
        self.assertNotIn(secret,text)
        self.assertEqual(row['connection_sha256'],fingerprint(secret))
        self.assertEqual(row['error_sha256'],fingerprint(secret))
        self.assertEqual(row['message_id_count'],100)
        self.assertEqual(len(row['message_ids_sha256']),32)
        self.assertNotIn('command',row);self.assertNotIn('outcome',row)
        self.assertEqual(self.audit.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.audit.health()['status'],'available')

    def test_rotation_is_bounded_and_reload_appends_with_distinct_instance(self):
        self.audit = Audit(self.setup,max_bytes=1100)
        for _ in range(25): self.audit.record('receiver_started',connection='a'*32)
        files=[self.audit.path,Path(str(self.audit.path)+'.1')]
        self.assertTrue(all(f.stat().st_size <= 1100 for f in files))
        replacement=Audit(self.setup,max_bytes=1100)
        replacement.record('plugin_unloaded')
        self.assertNotEqual(replacement.instance,self.audit.instance)
        rows=[json.loads(line) for f in files for line in f.read_text().splitlines()]
        self.assertEqual(rows[0]['schema'],'pantheon.hermes-audit.v1')
        self.assertTrue(any(row['instance']==replacement.instance for row in rows))
        self.assertGreater(self.audit.health()['rotations'],0)
        self.assertEqual(len(list(self.root.glob('*.audit.jsonl*'))),3) # two segments + lock

    def test_contention_symlink_and_disk_failure_are_visible_and_non_destructive(self):
        target=self.root/'preserve';target.write_text('UNCHANGED')
        self.audit.path.symlink_to(target)
        self.audit.record('receiver_started')
        self.assertEqual(target.read_text(),'UNCHANGED')
        self.assertEqual(self.audit.health()['status'],'degraded')
        self.audit.path.unlink()
        lockfd=Audit.open_private(str(self.audit.path)+'.lock')
        try:
            fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.audit.record('receiver_started')
            self.assertEqual(self.audit.failed,2)
        finally:os.close(lockfd)
        with patch('pantheon_connectors.hermes_audit.os.write',side_effect=OSError('PRIVATE-FAILURE')):
            self.audit.record('receiver_started')
        self.assertEqual(self.audit.failed,3)
        self.audit.record('receiver_started')
        self.assertEqual(self.audit.written,1)
        self.assertEqual(json.loads(self.audit.path.read_text())['sequence'],4)

    def test_missing_setup_creates_no_files(self):
        self.setup.unlink()
        self.audit.record('plugin_unloaded')
        self.assertEqual(list(self.root.iterdir()),[])


class ToolAudit(unittest.TestCase):
    setUp=peers.PeerTools.setUp
    call=peers.PeerTools.call

    def test_post_ack_and_withdraw_have_correlated_ids_without_content(self):
        self.client.command.return_value={'id':'message-123'}
        self.call('pantheon_post',text='PRIVATE-POST-CANARY',key='private-operation-key')
        self.call('pantheon_ack',message_ids=['message-123'],key='ack-key')
        self.client.command.return_value={}
        self.call('pantheon_command',op='withdraw',key='withdraw-key')
        rows=[json.loads(line) for line in self.plugin.audit.path.read_text().splitlines()]
        starts=[r for r in rows if r['event']=='tool_started']
        ends=[r for r in rows if r['event']=='tool_finished']
        self.assertEqual([r['request_sha256'] for r in starts],[r['request_sha256'] for r in ends])
        self.assertIn(fingerprint('message-123'),ends[0]['message_ids_sha256'])
        self.assertIn(fingerprint('message-123'),ends[1]['message_ids_sha256'])
        self.assertEqual(ends[2]['command'],'withdraw')
        raw=self.plugin.audit.path.read_text()
        for canary in ('PRIVATE-POST-CANARY','PRIVATE-LOCAL-CREDENTIAL','private-operation-key','chosen'):
            self.assertNotIn(canary,raw)

    def test_logging_failure_does_not_block_withdraw_and_doctor_exposes_gap(self):
        with patch('pantheon_connectors.hermes_audit.os.write',side_effect=OSError('disk full')):
            self.client.command.return_value={}
            self.assertEqual(self.call('pantheon_command',op='withdraw',key='cleanup'),{})
        doctor=self.plugin.doctor('chosen')
        self.assertEqual(doctor['audit']['status'],'degraded')
        self.assertGreater(doctor['audit']['failed'],0)
        self.client.command.assert_called_once_with('withdraw',key='cleanup')

    def test_error_logs_status_and_fingerprint_without_exception_text(self):
        from pantheon_connectors.client import APIError
        self.client.request.side_effect=APIError(403)
        self.assertEqual(self.call('pantheon_status')['status'],'blocked')
        rows=[json.loads(line) for line in self.plugin.audit.path.read_text().splitlines()]
        error=next(r for r in rows if r['event']=='tool_error')
        self.assertEqual(error['http_status'],403)
        self.assertEqual(error['error_kind'],'APIError')
        self.assertEqual(error['request_sha256'],rows[0]['request_sha256'])


class NoticeAudit(unittest.TestCase):
    setUp=notification.NotificationBoundaries.setUp
    new_plugin=notification.NotificationBoundaries.new_plugin
    receive_notice=notification.NotificationBoundaries.receive_notice

    def test_log_failure_keeps_durable_notice_fence_and_exposes_gap(self):
        # Break only the audit sink, leaving the delivery journal operational.
        target=self.root/'audit-target';target.write_text('PRESERVE')
        self.plugin.audit.path.symlink_to(target)
        self.assertEqual(self.plugin.tick(self.handle,self.binding),'scheduled_not_received')
        self.assertEqual(self.plugin.tick(self.handle,self.binding),'idle_or_notice_unresolved')
        self.ctx.inject_message.assert_called_once()
        self.assertEqual(target.read_text(),'PRESERVE')
        self.assertEqual(self.plugin.doctor('chosen')['audit']['status'],'degraded')

    def test_uncertain_host_send_retains_attempt_and_error_without_false_success(self):
        self.ctx.inject_message.side_effect=RuntimeError('PRIVATE-HOST-FAILURE')
        self.plugin.run(self.handle,self.binding)
        rows=[json.loads(line) for line in self.plugin.audit.path.read_text().splitlines()]
        self.assertTrue(any(r['event']=='notice_attempt' and r['outcome']=='outcome_unknown' for r in rows))
        self.assertFalse(any(r['event']=='notice_result' for r in rows))
        self.assertTrue(any(r['event']=='receiver_state' and 'error_sha256' in r for r in rows))
        self.assertNotIn('PRIVATE-HOST-FAILURE',self.plugin.audit.path.read_text())

    def test_notice_attempt_and_receive_share_token_fingerprint(self):
        self.plugin.tick(self.handle,self.binding)
        self.receive_notice()
        rows=[json.loads(line) for line in self.plugin.audit.path.read_text().splitlines()]
        attempt=next(r for r in rows if r['event']=='notice_attempt')
        accepted=next(r for r in rows if r['event']=='notice_result')
        received=next(r for r in rows if r['event']=='tool_finished')
        self.assertEqual(attempt['notice_sha256'],accepted['notice_sha256'])
        self.assertEqual(attempt['notice_sha256'],received['notice_sha256'])
        self.assertEqual(attempt['message_ids_sha256'],received['message_ids_sha256'])
        self.assertEqual(accepted['outcome'],'scheduled_not_received')
        self.assertNotIn('PRIVATE PEER CANARY',self.plugin.audit.path.read_text())
        self.assertFalse(any(r.get('operation')=='pantheon_ack' for r in rows))
