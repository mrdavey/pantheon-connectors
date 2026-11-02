"""Synthetic scope/cleanup regressions; no native delivery qualification."""
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors.client import APIError
from pantheon_connectors.codex_output_watch import run
from pantheon_connectors.receiver_lifecycle import ReceiverScope
from pantheon_connectors.state import save_private
from pantheon_connectors.worker import Blocked


class ReceiverLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'setup.json'
        self.config = dict(binding={'session': 'original'}, trial_expires_at=100,
                           allow_runtime_turns=True)
        save_private(self.path, self.config)
        self.stop = threading.Event()

    def test_stop_and_changed_authority_fence_the_reader(self):
        scope = ReceiverScope(self.path, 'connection', self.stop)
        self.assertTrue(scope.unchanged())
        save_private(self.path, dict(self.config, binding={'session': 'other'}))
        self.assertFalse(scope.unchanged())
        self.path.unlink()
        self.stop.set()
        self.assertFalse(scope.unchanged())

    def test_read_retry_exhausts_and_never_replays_uncertainty(self):
        scope = ReceiverScope(self.path, 'connection', self.stop)
        for expected in (2, 4, 8, 16):
            value, delay = scope.recover_read(APIError(503))
            self.assertEqual(delay, expected)
            self.assertEqual(value['retry_seconds'], expected)
        with self.assertRaises(Blocked): scope.recover_read(APIError(503))
        scope.retry.reset()
        self.assertEqual(scope.recover_read(APIError(503))[1], 2)
        with self.assertRaises(APIError): scope.recover_read(APIError(403))

    def test_expiry_preserves_original_scope_and_reports_uncertainty(self):
        audit = Mock()
        scope = ReceiverScope(self.path, 'connection', self.stop, audit)
        save_private(self.path, dict(self.config, trial_expires_at=200,
                                    binding={'session': 'replacement'}))
        with patch('pantheon_connectors.receiver_lifecycle.time.time', return_value=150), \
             patch('pantheon_connectors.onboarding.Onboarding') as service, \
             patch('pantheon_connectors.lifecycle_status.report_terminal', side_effect=APIError(503)) as report:
            scope.report_expiry()
        service.assert_called_once_with(self.config)
        report.assert_called_once_with(service.return_value, 'connection', self.config['binding'], 'time_ran_out', '100')
        self.assertEqual(audit.record.call_args.kwargs['outcome'], 'terminal_status_pending')

    def test_active_connection_and_invalid_deadlines_do_not_report_expiry(self):
        for end in (0, True, None, float('nan'), 200):
            save_private(self.path, dict(self.config, trial_expires_at=end))
            with patch('pantheon_connectors.receiver_lifecycle.time.time', return_value=150), \
                 patch('pantheon_connectors.lifecycle_status.report_terminal') as report:
                ReceiverScope(self.path, 'connection', self.stop).report_expiry()
            report.assert_not_called()

    def test_failed_owner_client_close_still_runs_expiry_cleanup(self):
        owner = SimpleNamespace(rpc=SimpleNamespace(pending=queue.Queue()),
                                close=Mock(side_effect=RuntimeError('client close failed')))
        def tick(_):
            self.stop.set()
            return {'status': 'idle'}
        notices = SimpleNamespace(allowed=Mock(), tick=tick)
        with patch('pantheon_connectors.codex_output_watch.OutputNotices', return_value=notices), \
             patch('pantheon_connectors.codex_output_watch.Owner', return_value=owner), \
             patch('pantheon_connectors.onboarding.Onboarding'), \
             patch('pantheon_connectors.lifecycle_status.report_terminal') as report:
            with self.assertRaisesRegex(RuntimeError, 'client close failed'):
                run(self.path, 'connection', self.stop, report=Mock())
        owner.close.assert_called_once()
        self.assertEqual(report.call_count, 1)
