import io
import json
import unittest
from unittest.mock import patch
from pantheon_connectors.operational import Diagnostics
from pantheon_connectors.worker import Worker, Blocked


class OperationalTests(unittest.TestCase):
    def test_content_and_volume(self):
        stream = io.StringIO()
        now = [0]
        diagnostics = Diagnostics(stream, lambda: now[0])
        diagnostics.emit('SECRET invitation chat file payload email')
        diagnostics.emit('idle')
        self.assertEqual(stream.getvalue(), '')
        for _ in range(100):
            diagnostics.emit('blocked')
        now[0] = 60
        diagnostics.emit('received')
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[-1]['suppressed'], 90)
        self.assertEqual(rows[0]['severity'], 'WARNING')
        self.assertEqual(rows[-1]['severity'], 'INFO')
        self.assertEqual(len(rows[0]['operation_id']), 32)
        self.assertNotIn('SECRET', stream.getvalue())

    def test_logging_failure_does_not_change_delivery(self):
        stream = io.StringIO()
        stream.close()
        Diagnostics(stream).emit('received')

    def test_worker_redacts_exception(self):
        import threading
        from contextlib import nullcontext
        from unittest.mock import Mock
        stream = io.StringIO()
        worker = Worker.__new__(Worker)
        worker.lock = threading.Lock()
        worker.journal = Mock()
        worker.journal.exclusive.return_value = nullcontext()
        worker.diagnostics = Diagnostics(stream)
        with patch.object(worker, '_once', side_effect=Blocked('SECRET TOKEN CHAT')):
            with self.assertRaises(Blocked):
                worker.once()
        self.assertNotIn('SECRET', stream.getvalue())
        self.assertEqual(json.loads(stream.getvalue())['outcome'], 'blocked')
