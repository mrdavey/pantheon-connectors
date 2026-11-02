import io
import json
import queue
import unittest

from pantheon_connectors.mcp_elicitation import Input
from pantheon_connectors.worker import Blocked


class ElicitationTests(unittest.TestCase):
    def transport(self, respond):
        transport = Input.__new__(Input)
        transport.form_supported = True
        transport.request_id = 7
        transport.incoming = queue.Queue()
        from collections import deque
        transport.pending = deque()
        transport.write = lambda request: respond(transport, request)
        return transport

    def test_correlates_actual_client_response_and_defers_other_requests(self):
        def respond(transport, request):
            for value in ({'id': 'wrong', 'result': {'action': 'accept'}},
                          {'id': 8, 'method': 'tools/list'},
                          {'id': request['id'], 'result': {'action': 'decline'}}):
                transport.incoming.put(json.dumps(value))
        transport = self.transport(respond)
        self.assertEqual(transport.elicit('Review', {}), {'action': 'decline'})
        self.assertEqual(json.loads(next(transport))['id'], 8)

    def test_absent_url_only_and_false_capabilities_never_send_a_form(self):
        transport = self.transport(lambda *_: self.fail('must not send'))
        for capabilities in ({}, {'elicitation': {'url': {}}}, {'elicitation': False}):
            transport.initialize({'capabilities': capabilities})
            with self.assertRaisesRegex(Blocked, 'does not advertise'):
                transport.elicit('Review', {})
        for capability in ({}, {'form': {}}):
            transport.initialize({'capabilities': {'elicitation': capability}})
            self.assertTrue(transport.form_supported)

    def test_cancel_eof_error_and_timeout_never_become_approval(self):
        for message in ('', json.dumps({'method': 'notifications/cancelled', 'params': {'requestId': 7}})):
            transport = self.transport(lambda t, _: t.incoming.put(message))
            with self.assertRaises(Blocked):
                transport.elicit('Review', {})
        transport = self.transport(lambda t, request: t.incoming.put(json.dumps({'id': request['id'], 'error': {'code': -1}})))
        with self.assertRaises(Blocked): transport.elicit('Review', {})
        transport = self.transport(lambda *_: None)
        with self.assertRaises(Blocked): transport.elicit('Review', {}, timeout=0.001)

    def test_inert_stream_preserves_lines_and_eof(self):
        transport = Input(io.StringIO('{"method":"ping"}\n'), lambda _: None)
        self.assertEqual(list(transport), ['{"method":"ping"}\n'])
