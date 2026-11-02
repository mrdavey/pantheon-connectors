from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pantheon_connectors.setup_timing import record, summarize, measure


def rows(*pairs):
    return [dict(event=name, monotonic_seconds=stamp) for name, stamp in pairs]


class TimingTests(unittest.TestCase):
    def test_full_flow_includes_discovery_and_separates_approval_inside_install(self):
        result = summarize(rows(('invitation_received',0), ('preflight_started',1),
            ('preflight_finished',3), ('install_started',4), ('approval_requested',5),
            ('approval_resolved',65), ('install_finished',68), ('readiness_started',70),
            ('readiness_finished',80), ('ready',80)))
        self.assertEqual(result['total'], dict(elapsed_seconds=80, approval_wait_seconds=60, work_seconds=20))
        self.assertEqual(result['stages']['install']['work_seconds'],4)
        self.assertTrue(result['invite_to_ready_under_30s'])
        self.assertIsNone(result['stages']['activation'])

    def test_fast_copy_does_not_hide_slow_invite_to_ready(self):
        result = summarize(rows(('invitation_received',0), ('install_started',40), ('install_finished',42),
            ('readiness_started',60), ('readiness_finished',61), ('ready',62)))
        self.assertEqual(result['stages']['install']['elapsed_seconds'],2)
        self.assertFalse(result['invite_to_ready_under_30s'])

    def test_blocked_and_incomplete_flows_never_claim_target_achieved(self):
        for terminal in ('blocked','cancelled'):
            result = summarize(rows(('invitation_received',0), ('install_started',1),
                ('approval_requested',2), (terminal,8)))
            self.assertIsNone(result['invite_to_ready_under_30s'])
            self.assertFalse(result['stages']['install']['complete'])
            self.assertEqual(result['total']['approval_wait_seconds'],6)

    def test_rejects_ambiguous_and_forged_records(self):
        invalid = [rows(('invitation_received',0),('ready',1)),
            rows(('invitation_received',0),('approval_requested',1),('approval_requested',2)),
            rows(('invitation_received',0),('install_finished',1)),
            rows(('invitation_received',2),('install_started',1)),
            rows(('invitation_received',0),('blocked',1),('install_started',2)),
            rows(('invitation_received',float('nan'))),
            [dict(event='invitation_received',monotonic_seconds=0,token='never-store')]]
        for events in invalid:
            with self.subTest(events=events), self.assertRaises(ValueError): summarize(events)

    def test_record_is_private_append_only_and_cannot_reset_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root/'timing.jsonl'
            with patch('pantheon_connectors.setup_timing.time.monotonic',side_effect=[1,2]):
                record(path,'invitation_received')
                record(path,'blocked')
            before = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError): record(path,'invitation_received')
            link=root/'link'; link.symlink_to(path)
            with self.assertRaises(OSError): record(link,'install_started')
            with self.assertRaises(ValueError): record(path,'install_started')
            self.assertEqual(path.read_bytes(),before)

    def test_measure_brackets_real_operation_and_failed_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve()/'timing.jsonl'
            record(path, 'invitation_received')
            with measure(path, 'preflight'):
                self.assertTrue(path.exists())
            with self.assertRaises(RuntimeError):
                with measure(path, 'admission'):
                    raise RuntimeError('synthetic admission refusal')
            import json
            report = summarize([json.loads(line) for line in path.read_text().splitlines()])
            self.assertEqual(report['status'], 'blocked')
            self.assertTrue(report['stages']['preflight']['complete'])
            self.assertFalse(report['stages']['admission']['complete'])
            self.assertIsNone(report['installation_under_30s'])

    def test_partial_observation_cannot_claim_full_invitation_timing(self):
        result = summarize(rows(('setup_observation_started',10), ('install_started',11),
            ('install_finished',12), ('readiness_started',13), ('readiness_finished',14), ('ready',14)))
        self.assertTrue(result['installation_under_30s'])
        self.assertFalse(result['invitation_receipt_measured'])
        self.assertIsNone(result['invite_to_ready_under_30s'])
