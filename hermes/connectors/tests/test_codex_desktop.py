"""Desktop onboarding uses the real local policy/router and queue state machines.

Native queue calls and public discovery are fixtures, not native acceptance evidence.
"""
import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors import codex_delivery as delivery, codex_hosted as hosted, codex_setup, hosted_v3
from pantheon_connectors.broker import codex_setup_policy as policy
from pantheon_connectors.codex_mcp import Server
from pantheon_connectors.codex_queue import Notices
from pantheon_connectors.codex_watch import run
from pantheon_connectors.native_cli import execute
from pantheon_connectors.runtime_policy import allowed
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked
import test_codex_queue as fixtures
from test_hosted_v3 import fixture

THREAD = fixtures.THREAD
CONNECTION = fixtures.CONNECTION


def discovery():
    value = fixture()
    value['packages']['codex']['version'] = '0.9.0'
    value['compatibility']['codex'].update(minimum_version='0.9.0', maximum_exclusive='0.10.0',
                                         host_modes=['app-server-tool-output', 'native-queue'])
    value['guide_url'] += '/' + 'a' * 64
    return value


class DesktopSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.metadata = dict(version=3, delivery=delivery.NATIVE_SETUP, service_origin='https://pantheon.talk',
                             package_version='0.9.0', source_revision='b'*40,
                             broker_contract=hosted_v3.CONTRACT, setup_contract=policy.CONTRACT)
        self.discovery = discovery()
        patches = [patch.dict(os.environ, CODEX_HOME=str(self.home)),
                   patch.object(hosted, 'descriptor', return_value=self.metadata),
                   patch.object(delivery, 'descriptor', return_value=self.metadata),
                   patch.object(codex_setup, 'descriptor', return_value=self.metadata),
                   patch.object(codex_setup, 'cli_compatibility', return_value=('/pinned/codex', '0.162.1')),
                   patch('pantheon_connectors.codex_queue.QueueCLI.available', return_value=True),
                   patch('pantheon_connectors.codex_daemon.discover', side_effect=AssertionError('Desktop must not inspect daemon')),
                   patch.object(hosted_v3, 'fetch', side_effect=lambda *_args, **_kw: copy.deepcopy(self.discovery))]
        for mocked in patches:
            mocked.start(); self.addCleanup(mocked.stop)
        self.server = hosted.HostedServer(); self.addCleanup(self.server.close)
        self.answer = dict(action='accept', content=dict(host='desktop', collaboration=True, minutes='0', transfers=False, sites=False))
        self.elicit = Mock(side_effect=lambda *_: copy.deepcopy(self.answer))
        self.server.elicit = self.elicit
        self.joins = []
        def join(_child, method, params):
            if params['name'] == 'pantheon_receive':
                return self.server.result(dict(messages=[], has_more=False))
            self.assertEqual(params['name'], 'pantheon_join')
            from pantheon_connectors.onboarding import invitation
            import hashlib
            link = params['arguments']['link']
            connection = hashlib.sha256(('https://pantheon.talk\0'+invitation(link, 'https://pantheon.talk')).encode()).hexdigest()[:32]
            self.joins.append((params['_meta']['threadId'], connection))
            return self.server.result(dict(status='joined', connection=connection))
        mocked = patch.object(Server, 'handle', new=join); mocked.start(); self.addCleanup(mocked.stop)

    def call(self, name, args, thread=THREAD):
        return self.server.handle('tools/call', dict(name=name, arguments=args, _meta={'threadId': thread}))['structuredContent']

    def connect(self, token='a', thread=THREAD):
        return self.call('pantheon_connect', dict(link='https://pantheon.talk/join#invite='+token*43, deliberate=True), thread)

    def reload(self):
        self.server.close()
        self.server = hosted.HostedServer(); self.addCleanup(self.server.close)
        self.server.elicit = self.elicit

    def test_native_choice_reload_and_independent_normal_chat_grants(self):
        first = self.connect()
        self.assertEqual(first['status'], 'native_reload_required')
        self.assertEqual(first['delivery'], delivery.GUI_QUEUE)
        self.assertIn('Restart the Codex desktop app', first['notice'])
        self.assertNotIn('codex resume', first['notice'])
        self.assertEqual(self.joins, [])
        self.assertEqual(list(self.server.root.rglob('setup.json')), [])
        message, form = self.elicit.call_args.args
        self.assertIn('user-message bubbles', message)
        self.assertIn('cannot be cancelled', message)
        self.assertIn('host', form['required'])
        self.reload()
        first = self.connect()
        second = self.connect('b', '00000000-0000-4000-8000-000000000002')
        self.assertEqual(first['onboarding_status'], 'connected')
        self.assertEqual(second['onboarding_status'], 'connected')
        self.elicit.assert_called_once()
        self.assertEqual(len(self.joins), 2)
        for thread, connection in self.joins:
            config = load_private(self.server.location(thread, connection))
            self.assertEqual(config['delivery'], delivery.GUI_QUEUE)
            self.assertEqual(config['binding']['surface'], 'native-queue')
            self.assertEqual(config['notification_policy'], {'mode': 'unlimited'})
            self.assertEqual(config['runtime_policy'], {'mode': 'connection'})
            self.assertFalse(config['allow_daemon_access']); self.assertFalse(config['allow_tool_output'])
            self.assertTrue(allowed(config))
        original = self.server.location(THREAD, first['connection']).read_bytes()
        self.connect()
        self.assertEqual(self.server.location(THREAD, first['connection']).read_bytes(), original)
        self.call('pantheon_setup', {'action': 'revoke'})
        self.assertEqual(self.connect()['status'], 'runtime_inactive')
        self.assertEqual(self.server.location(THREAD, first['connection']).read_bytes(), original)
        self.assertFalse(allowed(load_private(self.server.location(THREAD, first['connection']))))

    def test_missing_or_model_selected_host_never_grants(self):
        self.answer['content'].pop('host')
        with self.assertRaises(Blocked): self.connect()
        self.assertFalse(self.server.setup_policy.path.exists())
        with self.assertRaises(Blocked):
            self.call('pantheon_connect', dict(link='https://pantheon.talk/join#invite='+'a'*43, deliberate=True, host='desktop'))
        self.assertEqual(self.joins, [])

    def test_current_cli_only_discovery_rejects_gui_before_authorizing(self):
        self.connect(); self.reload()
        self.discovery['compatibility']['codex']['host_modes'] = ['app-server-tool-output']
        with self.assertRaisesRegex(Blocked, 'host is outside'):
            self.connect()
        self.assertEqual(self.joins, [])
        for path in self.server.root.rglob('setup.json'):
            self.assertFalse(allowed(load_private(path)))

    def test_policy_cannot_cross_delivery_and_legacy_review_does_not_widen(self):
        self.connect(); self.reload(); result = self.connect()
        config = load_private(self.server.location(THREAD, result['connection']))
        config.update(delivery=delivery.DEFAULT_DAEMON)
        self.assertFalse(allowed(config))
        old = self.server.setup_policy.read(); old.pop('delivery')
        save_private(self.server.setup_policy.path, old)
        config['delivery'] = delivery.GUI_QUEUE
        self.assertFalse(allowed(config))


class DesktopQueueTests(unittest.TestCase):
    restart = fixtures.QueueTests.restart

    def setUp(self):
        fixtures.QueueTests.setUp(self)
        self.config.update(delivery=delivery.GUI_QUEUE, allow_ui_fallback=False, allow_daemon_access=False,
            allow_tool_output=False, trial_expires_at=0, notification_policy={'mode': 'unlimited'},
            runtime_policy={'mode': 'connection'}, connection_runtime_supported=True, runtime_started_at=time.time(),
            hosted_consent=True, invitation_connection=CONNECTION,
            hosted_runtime_grant={'connection': CONNECTION, 'runtime_policy': {'mode': 'connection'}})
        self.config.pop('max_notices')
        self.chat_state = 'active'
        self.service.member = lambda connection, binding: (None, None, Mock(request=self.request))
        self.router = self.restart()
        self.setup = Path(self.temp.name).resolve()/'setup.json'; save_private(self.setup, self.config)

    def request(self, path):
        if path == '/v1/chat':
            return {'role': 'agent', 'member': 'agent', 'chat': {'state': self.chat_state,
                'members': {'agent': {'revoked': False, 'binding': self.config['binding']}}}}
        self.assertEqual(path, '/v1/pending')
        return self.pending

    def test_unlimited_normal_chat_drains_and_restarts_without_duplicate_notices(self):
        for i in range(10):
            self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id': f'm{i}'})]
            self.assertEqual(self.restart().tick(CONNECTION), 'queue_accepted_not_received')
            wake = list(self.router.read()['notices'])[-1]
            self.assertEqual(self.restart().tick(CONNECTION), 'idle_or_notice_unresolved')
            self.assertEqual(self.router.wake(CONNECTION, wake)['status'], 'draining')
            self.assertEqual(self.router.wake(CONNECTION, wake, finish=True)['status'], 'drain_required')
            self.pending = []
            self.assertEqual(self.router.wake(CONNECTION, wake, finish=True)['status'], 'completed')
        self.assertEqual(len(self.calls), 10)
        self.assertTrue(all(text.startswith('Automated Pantheon notification—not a human request.') for text in self.calls))
        self.assertNotIn('PRIVATE PEER TEXT', ''.join(self.calls))

    def test_revocation_during_preflight_and_missing_queue_never_fallback(self):
        self.available = False
        with self.assertRaisesRegex(Blocked, 'no fallback'): self.router.tick(CONNECTION)
        self.assertEqual(self.router.read()['notices'], {})
        self.available = True
        self.queue.available = lambda: self.config.update(allow_runtime_turns=False) or True
        with self.assertRaises(Blocked): self.router.tick(CONNECTION)
        self.assertEqual(self.calls, [])

    def test_closure_during_queue_preflight_suppresses_new_notice(self):
        def close():
            self.chat_state = 'closed'
            return True
        self.queue.available = close
        self.assertEqual(self.router.tick(CONNECTION), 'connection_closed')
        self.assertEqual(self.calls, [])
        self.assertIn(CONNECTION, self.router.read()['withdrawn_connections'])

    def test_expired_finish_is_local_and_does_not_claim_ack_or_cancel(self):
        self.router.tick(CONNECTION)
        wake = next(iter(self.router.read()['notices']))
        self.config['allow_runtime_turns'] = False; save_private(self.setup, self.config)
        with patch.dict(os.environ, CODEX_HOME='/chosen/home'), patch('pantheon_connectors.codex_queue.Onboarding') as service:
            value = execute(self.config, 'pantheon_finish_wake', {'connection': CONNECTION, 'wake_id': wake}, native_thread=THREAD, setup_file=self.setup)
            self.assertEqual(value['status'], 'connection_fenced')
            self.assertFalse(value['native_queue_cancelled']); self.assertFalse(value['acknowledgement_inferred'])
            service.return_value.member.assert_not_called()
        self.assertEqual(self.router.read()['notices'][wake]['state'], 'queue_accepted')
        with patch.dict(os.environ, CODEX_HOME='/chosen/home'), self.assertRaises(Blocked):
            execute(self.config, 'pantheon_begin_wake', {'connection': CONNECTION, 'wake_id': wake}, native_thread=THREAD)

    def test_room_closure_fences_private_grant_without_cancelling_accepted_entry(self):
        self.router.tick(CONNECTION)
        wake = next(iter(self.router.read()['notices']))
        self.chat_state = 'closed'
        with patch('pantheon_connectors.codex_watch.Notices', return_value=self.router):
            run(self.setup, CONNECTION, threading.Event(), report=Mock())
        self.assertFalse(allowed(load_private(self.setup)))
        journal = self.router.read()
        self.assertIn(CONNECTION, journal['withdrawn_connections'])
        self.assertEqual(journal['notices'][wake]['state'], 'queue_accepted')
        self.assertEqual(len(self.calls), 1)

    def test_active_wake_drains_terminal_input_before_closure_fences(self):
        self.router.tick(CONNECTION)
        wake = next(iter(self.router.read()['notices']))
        self.router.wake(CONNECTION, wake)
        self.chat_state = 'closed'
        self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'],
                             message={'id': 'terminal'})]
        self.assertEqual(self.router.tick(CONNECTION), 'idle_or_notice_unresolved')
        self.assertNotIn(CONNECTION, self.router.read().get('withdrawn_connections', {}))
        self.assertEqual(self.router.wake(CONNECTION, wake, finish=True)['status'], 'drain_required')
        self.pending = []  # Explicit agent ACK; tick/finish never consumes input.
        self.assertEqual(self.restart().wake(CONNECTION, wake, finish=True)['status'], 'completed')
        self.assertEqual(self.router.tick(CONNECTION), 'connection_closed')
        self.assertEqual(len(self.calls), 1)

    def test_terminal_drain_deadline_survives_restart_and_native_calls(self):
        self.router.tick(CONNECTION)
        wake = next(iter(self.router.read()['notices']))
        self.router.wake(CONNECTION, wake)
        self.chat_state = 'closed'
        observed = time.time()
        with patch('pantheon_connectors.codex_queue.time.time', return_value=observed):
            self.assertEqual(self.router.tick(CONNECTION), 'idle_or_notice_unresolved')
        with patch('pantheon_connectors.codex_queue.time.time', return_value=observed + 59):
            self.assertEqual(self.restart().tick(CONNECTION), 'idle_or_notice_unresolved')
        self.assertEqual(self.router.read()['closed_observed_at'][CONNECTION], observed)
        with patch('pantheon_connectors.codex_queue.time.time', return_value=observed + 60):
            with self.assertRaisesRegex(Blocked, 'terminal drain ended'):
                self.restart().wake(CONNECTION, wake, finish=True)
        self.assertIn(CONNECTION, self.router.read()['withdrawn_connections'])
        self.assertEqual(self.router.read()['notices'][wake]['state'], 'draining')
        with patch('pantheon_connectors.codex_watch.Notices', return_value=self.restart()):
            run(self.setup, CONNECTION, threading.Event(), report=Mock())
        self.assertFalse(allowed(load_private(self.setup)))
        self.assertEqual(len(self.calls), 1)

    def test_terminal_drain_never_extends_original_authorization(self):
        self.router.tick(CONNECTION)
        wake = next(iter(self.router.read()['notices']))
        self.router.wake(CONNECTION, wake)
        self.chat_state = 'closed'
        self.assertEqual(self.router.tick(CONNECTION), 'idle_or_notice_unresolved')
        self.config['allow_runtime_turns'] = False
        with self.assertRaisesRegex(Blocked, 'authorization'):
            self.router.wake(CONNECTION, wake, finish=True)
        self.assertEqual(self.router.read()['notices'][wake]['state'], 'draining')
        self.assertEqual(len(self.calls), 1)

    def test_receiver_reload_owns_one_worker_and_stops_on_eof(self):
        entered = threading.Event()
        def receiver(_path, _connection, stop, **_kwargs):
            entered.set(); stop.wait(2)
        server = Server(self.setup); self.addCleanup(server.close)
        with patch('pantheon_connectors.codex_watch.run', side_effect=receiver) as queue, \
                patch('pantheon_connectors.codex_mcp.run', side_effect=AssertionError('daemon receiver')):
            server.start(CONNECTION); self.assertTrue(entered.wait(1))
            server.start(CONNECTION); self.assertEqual(queue.call_count, 1)
            server.close(); self.assertFalse(server.workers[CONNECTION][1].is_alive())
        self.assertTrue(allowed(load_private(self.setup)))  # EOF is not remote withdrawal.

    def test_expiry_stops_delivery_and_reports_terminal_state_without_renewal(self):
        self.config.pop('runtime_policy')
        self.config['trial_expires_at'] = time.time()-1
        save_private(self.setup, self.config)
        original = self.setup.read_bytes()
        with patch('pantheon_connectors.lifecycle_status.report_terminal') as report:
            with self.assertRaises(Blocked): run(self.setup, CONNECTION, threading.Event())
            self.assertEqual(report.call_args.args[3], 'time_ran_out')
            self.assertEqual(report.call_args.args[4], str(self.config['trial_expires_at']))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.setup.read_bytes(), original)

    def test_scope_change_and_uncertain_send_remain_fenced(self):
        self.router.unchanged = lambda: False
        with self.assertRaises(Blocked): self.router.tick(CONNECTION)
        self.assertEqual(self.calls, [])
        self.router.unchanged = lambda: True
        self.failure = TimeoutError('native outcome unknown')
        with self.assertRaisesRegex(Blocked, 'uncertain'): self.router.tick(CONNECTION)
        self.failure = None
        with self.assertRaisesRegex(Blocked, 'Unresolved'): self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls), 1)

@unittest.skipUnless(fixtures.product_env('HTTP_TEST_URL'), 'isolated HTTP fixture required')
class DesktopHTTPTests(unittest.TestCase):
    def test_normal_connection_receipts_readiness_drain_and_closed_receiver(self):
        import secrets
        from pantheon_connectors.client import Client
        from pantheon_connectors.onboarding import Onboarding
        url = fixtures.product_env('HTTP_TEST_URL')
        secret = secrets.token_urlsafe(32)
        admin = Client(url, secret)
        admin.request('/v1/chats', dict(secret=secret, key=secrets.token_urlsafe(24),
            title='Synthetic desktop connection', purpose='Local regression; no native model', hours=4,
            slots=[dict(id='codex', runtime='codex', required=True)]))
        self.addCleanup(lambda: admin.command('end', key='desktop-fixture-end'))
        invite = secrets.token_urlsafe(32)
        admin.command('invite', target='codex', secret=invite, hours=1)
        with tempfile.TemporaryDirectory() as temporary:
            setup = Path(temporary).resolve()/'setup.json'
            config = dict(url=url, state_dir=str(setup.parent/'state'), delivery=delivery.GUI_QUEUE,
                binding=dict(runtime='codex', surface='native-queue', profile='default', version='fixture', session=THREAD),
                codex_home=str(setup.parent), codex_executable='/fixture/codex', allow_runtime_turns=True,
                allow_daemon_access=False, allow_tool_output=False, allow_ui_fallback=False,
                trial_expires_at=0, runtime_policy={'mode': 'connection'}, connection_runtime_supported=True,
                runtime_started_at=time.time(), notification_policy={'mode': 'unlimited'})
            service = Onboarding(config)
            connection = service.join(url+'/join#invite='+invite, config['binding'], deliberate=True, preflight=lambda _: None)['connection']
            config.update(hosted_consent=True, invitation_connection=connection,
                          hosted_runtime_grant={'connection': connection, 'runtime_policy': {'mode': 'connection'}})
            save_private(setup, config)
            queue = Mock(); queue.available.return_value=True; queue.send.return_value={}
            notices = Notices(config, service=service, queue=queue)
            self.assertEqual(notices.tick(connection), 'queue_accepted_not_received')
            wake = next(iter(notices.read()['notices']))
            notices.wake(connection, wake)
            _, _, member = service.member(connection, config['binding'])
            unread = member.request('/v1/pending')
            self.assertTrue(all(e['delivery']['state']=='pending' for e in unread))
            service.receive(connection, config['binding'])
            unread = member.request('/v1/pending')
            self.assertTrue(all(e['delivery']['state']=='received' for e in unread))
            probe = next(e['message'] for e in unread if e['message']['kind']=='probe')
            service.command(connection, config['binding'], 'ack', ids=[e['message']['id'] for e in unread], key='desktop-probe-ack')
            service.command(connection, config['binding'], 'ready', text=probe['text'], key='desktop-ready')
            self.assertEqual(notices.wake(connection, wake, finish=True)['status'], 'drain_required')
            service.receive(connection, config['binding'])
            unread = member.request('/v1/pending')
            service.command(connection, config['binding'], 'ack', ids=[e['message']['id'] for e in unread], key='desktop-start-ack')
            admin.command('end', key='desktop-fixture-end')
            # The real server can close while this accepted wake is running.
            # Its terminal input must remain readable/ACKable before fencing.
            self.assertEqual(notices.tick(connection), 'idle_or_notice_unresolved')
            self.assertTrue(allowed(load_private(setup)))
            service.receive(connection, config['binding'])
            unread = member.request('/v1/pending')
            self.assertTrue(any(e['message']['kind'] == 'terminal' for e in unread))
            self.assertEqual(notices.wake(connection, wake, finish=True)['status'], 'drain_required')
            service.command(connection, config['binding'], 'ack',
                            ids=[e['message']['id'] for e in unread], key='desktop-terminal-ack')
            self.assertEqual(notices.wake(connection, wake, finish=True)['status'], 'completed')
            with patch('pantheon_connectors.codex_watch.Notices', return_value=notices):
                run(setup, connection, threading.Event(), report=Mock())
            self.assertFalse(allowed(load_private(setup)))
            queue.send.assert_called_once()
