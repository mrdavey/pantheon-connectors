import pathlib
import tempfile
import unittest
from pantheon_connectors.lifecycle_status import report_terminal
from pantheon_connectors.worker import Blocked
from pantheon_connectors.onboarding import TOOLS, validate_arguments


class LifecycleStatusTests(unittest.TestCase):
    def test_lost_response_replays_exact_request_without_renewing_allowance(self):
        binding = dict(runtime='hermes', version='fixture', profile='default', session='fixed', surface='native-adapter')
        with tempfile.TemporaryDirectory() as root:
            class Client:
                url = 'http://127.0.0.1:8080'
                credential = 'fixture'
                def __init__(self): self.requests = []; self.fail = True
                def command(self, op, *, before_send, **fields):
                    payload = dict(op=op, epoch=1, config=3, **fields)
                    before_send(payload)
                    return self.request('/v1/commands', payload)
                def request(self, route, payload):
                    self.requests.append((route, payload))
                    if self.fail:
                        self.fail = False
                        raise TimeoutError('response lost after acceptance')
                    return {'id': 'same-event'}
            client = Client()
            class Service:
                def member(self, connection, observed, *, allow_expired=False):
                    self_check.assertEqual(observed, binding)
                    self_check.assertTrue(allow_expired)
                    return pathlib.Path(root) / 'connection.json', {}, client
            self_check = self
            service = Service()
            with self.assertRaises(TimeoutError):
                report_terminal(service, 'connection', binding, 'time_ran_out', 'original-deadline')
            report_terminal(service, 'connection', binding, 'time_ran_out', 'original-deadline')
            self.assertEqual(client.requests[0], client.requests[1])
            report_terminal(service, 'connection', binding, 'time_ran_out', 'original-deadline')
            self.assertEqual(len(client.requests), 2)
            with self.assertRaises(Blocked):
                report_terminal(service, 'connection', binding, 'working', 'original-deadline')

    def test_native_tool_can_report_ending_with_target(self):
        validate_arguments('pantheon_command', dict(connection='connection', op='report', target='completed', key='fixed'))
        tool = next(t for t in TOOLS if t['name'] == 'pantheon_command')
        self.assertIn('target', tool['inputSchema']['properties'])

    def test_hermes_receiver_reports_expiry_after_local_fence(self):
        import threading
        import time
        from unittest.mock import Mock, patch
        from pantheon_connectors.hermes_plugin import NativePlugin
        from pantheon_connectors.state import save_private
        with tempfile.TemporaryDirectory() as root:
            setup = pathlib.Path(root) / 'setup.json'
            end = time.time() - 1
            config = dict(allow_runtime_turns=False, trial_expires_at=0,
                          cleanup=dict(runtime_deadline=end))
            save_private(setup, config)
            plugin = NativePlugin.__new__(NativePlugin)
            plugin.stop = threading.Event()
            plugin.receiver_stops = {'connection': threading.Event()}
            plugin.outcomes = {}
            plugin.lock = threading.RLock()
            plugin.active = {'connection': object()}
            plugin.audit = Mock()
            plugin.setup_file = setup
            plugin.service_factory = Mock(return_value='same-membership-service')
            plugin.tick = Mock(return_value='trial_expired')
            binding = {'session': 'original-session'}
            with patch('pantheon_connectors.lifecycle_status.report_terminal') as publish:
                plugin.run('connection', binding)
            publish.assert_called_once_with('same-membership-service', 'connection', binding,
                                            'time_ran_out', str(end))
            self.assertNotIn('connection', plugin.active)

    def test_expired_allowance_allows_only_terminal_native_metadata(self):
        from unittest.mock import Mock
        from pantheon_connectors.onboarding import Onboarding
        service = Onboarding.__new__(Onboarding)
        service.config = {}
        client = Mock()
        service.member = Mock(return_value=(None, {}, client))
        service.require_allowance = Mock(side_effect=Blocked('expired'))
        service.command('connection', {'session': 'fixed'}, 'report', target='completed', key='ending')
        service.member.assert_called_once_with('connection', {'session': 'fixed'}, allow_expired=True)
        service.require_allowance.assert_not_called()
        self.assertNotIn('before_send', client.command.call_args.kwargs)
        service.member.reset_mock()
        service.command('connection', {'session': 'fixed'}, 'report', target='working', key='working')
        service.member.assert_called_once_with('connection', {'session': 'fixed'}, allow_expired=False)
        callback = client.command.call_args.kwargs['before_send']
        with self.assertRaises(Blocked): callback({})
