"""Synthetic native notification boundaries; never contacts a Hermes host/model."""
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

from pantheon_connectors.client import Client, APIError
from pantheon_connectors.hermes_plugin import HERMES_COMMIT, NativePlugin, NOTICE
from pantheon_connectors.onboarding import Onboarding, dispatch
from pantheon_connectors.state import Journal, save_private, load_private
from pantheon_connectors.worker import Blocked
from pantheon_connectors.environment import get as product_env
from test_conformance import FakeClient, test_brief


class Context:
    profile_name = 'fixture'

    def __init__(self):
        self.inject_message = Mock(return_value=True)


class NotificationBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = dict(url='http://127.0.0.1:8080', state_dir=str(self.root/'state'),
                           allow_runtime_turns=True, trial_expires_at=time.time()+300)
        self.binding = dict(runtime='hermes', version='fixture', surface='native-adapter',
                            profile='fixture', session='chosen', return_route='hermes:chosen')
        self.setup = self.root/'setup.json'
        save_private(self.setup, self.config)
        self.envelope = dict(protocol='pyros.v1', binding=self.binding, chat='fixture', brief=test_brief(),
                            delivery=dict(id='delivery-1', state='pending'),
                            message=dict(id='message-1', kind='message', text='PRIVATE PEER CANARY'))
        self.client = FakeClient(self.envelope)
        self.ctx = Context()
        self.public = Mock()
        self.public.request.side_effect = lambda route, payload=None: (
            dict(protocol='pyros.v1', local_test=True) if route == '/v1/manifest' else
            dict(purpose='Synthetic fixture') if route == '/v1/preview' else dict(id='fixture-member'))
        self.service = Onboarding(self.config, client_factory=lambda _, credential: self.client if credential else self.public)
        self.link = self.config['url']+'/join#invite='+'a'*40
        self.plugin = self.new_plugin()
        self.handle = self.service.join(self.link, self.binding, deliberate=True,
                                        preflight=self.plugin.preflight)['connection']
        self.public.reset_mock()

    def new_plugin(self):
        plugin = NativePlugin(self.ctx, self.setup, 'fixture', HERMES_COMMIT,
                              service_factory=lambda _: self.service, incoming_probe=lambda _: True)
        plugin.observe(session_id='chosen', platform='desktop')
        return plugin

    def assert_notice(self, handle, session):
        self.ctx.inject_message.assert_called_once()
        call = self.ctx.inject_message.call_args
        self.assertTrue(call.args[0].startswith(NOTICE+' Connection: '+handle+' notice_token: '))
        self.assertEqual(call.kwargs, dict(session_key=session))

    def receive_notice(self, token=None):
        token = token or self.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
        return json.loads(self.plugin.handle('pantheon_receive',
            dict(connection=self.handle, notice_token=token), session_id='chosen'))

    def test_multiple_deliveries_coalesce_and_manual_drain_keeps_queued_notice_fenced(self):
        self.plugin.tick(self.handle, self.binding)
        token = self.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
        self.envelope['delivery']['id'] = 'delivery-2'
        self.assertEqual(self.new_plugin().tick(self.handle, self.binding), 'idle_or_notice_unresolved')
        self.ctx.inject_message.assert_called_once()
        # Another explicit receive drained the old input. Its queued notice
        # must not receive newly arrived, unrelated content or generate a reply.
        commands = list(self.client.commands)
        result = self.receive_notice(token)
        self.assertEqual(result['status'], 'stale_notice_noop')
        self.assertEqual(result['messages'], [])
        self.assertEqual(self.client.commands, commands)
        self.assertEqual(self.receive_notice(token)['status'], 'stale_notice_noop')
        self.assertEqual(self.plugin.tick(self.handle, self.binding), 'scheduled_not_received')

    def test_unknown_token_fails_before_network_or_receipt(self):
        with patch.object(self.client, 'request') as request:
            self.assertEqual(self.receive_notice('unknown')['status'], 'blocked')
            request.assert_not_called()
        self.assertEqual(self.client.commands, [])

    def test_receiver_survives_foreground_receive_lock_and_delivers_next_notice(self):
        self.plugin.tick(self.handle, self.binding)
        token = self.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
        entered, release = threading.Event(), threading.Event()
        request = self.client.request
        results, errors = [], []

        def paused_request(route, payload=None):
            if route == '/v1/pending' and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise AssertionError('Fixture receive was not released')
            return request(route, payload)

        def receive():
            try:
                results.append(self.service.receive(self.handle, self.binding))
            except Exception as error:
                errors.append(error)

        stop = threading.Event()
        self.plugin.receiver_stops[self.handle] = stop
        waits = []

        def next_tick(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                self.assertEqual(self.plugin.outcomes[self.handle], 'journal_busy_retry')
                self.assertEqual(self.ctx.inject_message.call_count, 1)
                release.set()
                foreground.join(2)
                self.assertFalse(foreground.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(results[0]['messages']), 1)
                # A late content-free notice must not consume a new peer message.
                self.envelope['delivery'] = dict(id='delivery-2', state='pending')
                self.envelope['message']['id'] = 'message-2'
                self.assertEqual(self.receive_notice(token)['status'], 'stale_notice_noop')
            else:
                stop.set()

        with patch.object(self.client, 'request', side_effect=paused_request):
            foreground = threading.Thread(target=receive)
            foreground.start()
            try:
                self.assertTrue(entered.wait(2))
                with patch.object(stop, 'wait', side_effect=next_tick):
                    self.plugin.run(self.handle, self.binding)
            finally:
                release.set()
                foreground.join(2)
        self.assertEqual(waits, [5, 5])
        self.assertEqual(self.ctx.inject_message.call_count, 2)
        self.assertEqual(self.plugin.outcomes[self.handle], 'scheduled_not_received')
        path, _, _ = self.service.member(self.handle, self.binding)
        journal = Journal(path.with_suffix('.sqlite'), self.binding)
        try:
            self.assertEqual(journal.control('notice-count'), '2')
            self.assertEqual(json.loads(journal.control('hermes-notice'))['deliveries'], ['delivery-2'])
        finally:
            journal.db.close()
        self.assertEqual(load_private(self.setup), self.config)
        self.assertNotIn('ack', [op for op, _ in self.client.commands])
        audit = [json.loads(line) for line in self.plugin.audit.path.read_text().splitlines()]
        self.assertIn('journal_busy_retry', [row.get('outcome') for row in audit])
        self.assertNotIn('blocked_check_setup_binding_and_native_host', [row.get('outcome') for row in audit])

    def test_journal_contention_retry_rechecks_stop_expiry_and_identity(self):
        for action in ('withdraw', 'unload', 'expire', 'identity'):
            with self.subTest(action=action):
                plugin = self.new_plugin()
                save_private(self.setup, self.config)
                stop = threading.Event()
                plugin.receiver_stops[self.handle] = stop
                path, _, _ = self.service.member(self.handle, self.binding)
                journal = Journal(path.with_suffix('.sqlite'), self.binding)

                def after_contention(seconds):
                    self.assertEqual(seconds, 5)
                    self.assertEqual(plugin.outcomes[self.handle], 'journal_busy_retry')
                    if action == 'withdraw': plugin.stop_connection(self.handle)
                    elif action == 'unload': plugin.close()
                    elif action == 'expire': save_private(self.setup, dict(self.config, trial_expires_at=time.time()-1))
                    else: plugin.observe(session_id='chosen', platform='unsupported')

                try:
                    with journal.exclusive(), patch.object(stop, 'wait', side_effect=after_contention) as wait:
                        plugin.run(self.handle, self.binding)
                        wait.assert_called_once_with(5)
                    self.ctx.inject_message.assert_not_called()
                    self.assertIsNone(journal.control('notice-count'))
                    if action == 'identity':
                        self.assertEqual(plugin.outcomes[self.handle], 'blocked_check_setup_binding_and_native_host')
                    if action == 'expire':
                        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
                finally:
                    journal.db.close()
                    plugin.close()

    def test_legacy_uncertain_notice_cannot_be_requeued_after_upgrade(self):
        path, _, _ = self.service.member(self.handle, self.binding)
        journal = Journal(path.with_suffix('.sqlite'), self.binding)
        journal.control('notice-old-delivery', 'outcome_unknown')
        journal.db.close()
        self.assertEqual(self.plugin.tick(self.handle, self.binding), 'legacy_notice_unresolved')
        self.ctx.inject_message.assert_not_called()

    def test_expiry_watch_fences_without_receiver_or_host_mutation(self):
        save_private(self.setup, self.config | dict(trial_expires_at=time.time()+.1, unrelated='preserve'))
        self.plugin.watch_expiry()
        self.plugin.expiry_thread.join(timeout=2)
        self.assertFalse(self.plugin.expiry_thread.is_alive())
        current = load_private(self.setup)
        self.assertFalse(current['allow_runtime_turns'])
        self.assertEqual(current['trial_expires_at'], 0)
        self.assertEqual(current['unrelated'], 'preserve')
        self.assertEqual(current['cleanup']['status'], 'runtime_fenced_host_cleanup_required')
        self.assertFalse(self.plugin.stop.is_set())
        self.ctx.inject_message.assert_not_called()
        self.assertEqual(self.client.commands, [])
        before = self.setup.read_bytes()
        self.plugin.expire(current)
        self.assertEqual(self.setup.read_bytes(), before)

    def test_expiry_watch_does_not_wait_for_blocked_network_receiver(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        original = self.client.request
        def hold(route, payload=None):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('fixture release missing')
            return original(route, payload)
        with patch.object(self.client, 'request', side_effect=hold):
            receiver = threading.Thread(target=lambda: result.append(self.plugin.tick(self.handle, self.binding)))
            try:
                receiver.start()
                self.assertTrue(entered.wait(5))
                # Establish the blocked receiver before expiring its allowance.
                # A sub-second wall-clock deadline races scheduling on CI hosts.
                save_private(self.setup, self.config | dict(trial_expires_at=time.time()-1))
                self.plugin.watch_expiry()
                self.plugin.expiry_thread.join(timeout=5)
                self.assertFalse(self.plugin.expiry_thread.is_alive())
                self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
                self.assertTrue(receiver.is_alive())
            finally:
                release.set()
                receiver.join(timeout=2)
                self.plugin.close()
        self.assertEqual(result, ['trial_expired'])
        self.ctx.inject_message.assert_not_called()

    def test_unload_stops_expiry_watch_without_host_or_setup_change(self):
        before = self.setup.read_bytes()
        self.plugin.watch_expiry()
        self.plugin.close()
        self.plugin.expiry_thread.join(timeout=1)
        self.assertFalse(self.plugin.expiry_thread.is_alive())
        self.assertEqual(self.setup.read_bytes(), before)

    def test_expiry_preserves_concurrent_owner_change(self):
        old = self.config | dict(trial_expires_at=1)
        self.plugin.expire(old)
        self.assertEqual(load_private(self.setup), self.config)

    def test_unknown_notice_outcome_is_durable_and_not_replayed(self):
        self.ctx.inject_message.side_effect = TimeoutError('synthetic lost response')
        with self.assertRaises(Blocked): self.plugin.tick(self.handle, self.binding)
        self.ctx.inject_message.side_effect = None
        self.assertEqual(self.new_plugin().tick(self.handle, self.binding), 'idle_or_notice_unresolved')
        self.assert_notice(self.handle, 'chosen')
        self.assertEqual(self.client.commands, [])
        path, _, _ = self.service.member(self.handle, self.binding)
        journal = Journal(path.with_suffix('.sqlite'), self.binding)
        try:
            self.assertEqual(journal.control('notice-delivery-1'), 'outcome_unknown')
            self.assertEqual(journal.control('notice-count'), '1')
        finally: journal.db.close()

    def test_host_refusal_is_not_receipt_and_not_automatically_retried(self):
        self.ctx.inject_message.return_value = False
        self.assertEqual(self.plugin.tick(self.handle, self.binding), 'native_host_unavailable')
        self.assertEqual(self.new_plugin().tick(self.handle, self.binding), 'idle_or_notice_unresolved')
        self.ctx.inject_message.assert_called_once()
        self.assertEqual(self.client.commands, [])
        self.assertEqual(self.envelope['delivery']['state'], 'pending')

    def test_eight_notice_budget_survives_receiver_restart(self):
        for index in range(8):
            self.envelope['delivery']['id'] = 'delivery-'+str(index)
            self.assertEqual(self.new_plugin().tick(self.handle, self.binding), 'scheduled_not_received')
            self.receive_notice()
            self.envelope['delivery']['state'] = 'pending'
            self.envelope['message']['id'] = 'message-'+str(index+2)
        self.envelope['delivery']['id'] = 'delivery-ninth'
        self.assertEqual(self.new_plugin().tick(self.handle, self.binding), 'notice_budget_exhausted')
        self.assertEqual(self.ctx.inject_message.call_count, 8)
        for call in self.ctx.inject_message.call_args_list:
            self.assertTrue(call.args[0].startswith(NOTICE+' Connection: '+self.handle+' notice_token: '))
            self.assertEqual(call.kwargs, dict(session_key='chosen'))
        self.assertEqual([op for op, _ in self.client.commands], ['claim', 'receipt'] * 8)

    def test_revoked_expired_and_unbounded_runtime_stop_before_pending_or_injection(self):
        for changed in (dict(allow_runtime_turns=False), dict(trial_expires_at=time.time()-1),
                        dict(trial_expires_at=time.time()+7200), dict(trial_expires_at=float('nan'))):
            with self.subTest(changed=changed):
                save_private(self.setup, self.config | changed)
                with patch.object(self.service, 'member') as member:
                    self.assertEqual(self.plugin.tick(self.handle, self.binding), 'trial_expired')
                    member.assert_not_called()
                self.ctx.inject_message.assert_not_called()

    def test_allowance_revoked_during_pending_fetch_stops_before_notice(self):
        original_request = self.client.request
        def revoke_during_fetch(route, payload=None):
            result = original_request(route, payload)
            save_private(self.setup, self.config | dict(allow_runtime_turns=False))
            return result
        with patch.object(self.client, 'request', side_effect=revoke_during_fetch):
            self.assertEqual(self.plugin.tick(self.handle, self.binding), 'trial_expired')
        self.ctx.inject_message.assert_not_called()
        self.assertEqual(self.client.commands, [])

    def test_unload_during_pending_fetch_stops_before_notice_or_durable_intent(self):
        original_request = self.client.request
        def unload_during_fetch(route, payload=None):
            result = original_request(route, payload)
            self.plugin.close()
            return result
        with patch.object(self.client, 'request', side_effect=unload_during_fetch):
            self.assertEqual(self.plugin.tick(self.handle, self.binding), 'plugin_unloaded')
        self.ctx.inject_message.assert_not_called()
        self.assertEqual(self.client.commands, [])
        path, _, _ = self.service.member(self.handle, self.binding)
        journal = Journal(path.with_suffix('.sqlite'), self.binding)
        try:
            self.assertIsNone(journal.control('notice-count'))
        finally: journal.db.close()

    def test_retained_handler_cannot_operate_after_unload(self):
        self.plugin.close()
        with patch.object(self.plugin, 'service_factory') as factory:
            for name, args in [('pantheon_receive', dict(connection=self.handle)),
                               ('pantheon_post', dict(connection=self.handle, text='no', key='no'))]:
                result = json.loads(self.plugin.handle(name, args, session_id='chosen'))
                self.assertEqual(result['status'], 'blocked')
            factory.assert_not_called()
        self.assertEqual(self.plugin.doctor('chosen')['status'], 'blocked')
        with patch('pantheon_connectors.hermes_plugin.threading.Thread') as thread:
            self.plugin.start(self.handle, self.binding)
            thread.assert_not_called()

    def test_later_unsupported_surface_blocks_existing_membership_tools(self):
        self.plugin.observe(session_id='chosen', platform='cli')
        with patch.object(self.service, 'receive') as receive, patch.object(self.service, 'command') as command:
            for name, args in [('pantheon_receive', dict(connection=self.handle)),
                               ('pantheon_post', dict(connection=self.handle, text='no', key='no'))]:
                self.assertEqual(json.loads(self.plugin.handle(name, args, session_id='chosen'))['status'], 'blocked')
            receive.assert_not_called()
            command.assert_not_called()

    def test_reloaded_instance_requires_fresh_observation_for_saved_membership(self):
        self.plugin.close()
        replacement = NativePlugin(self.ctx, self.setup, 'fixture', HERMES_COMMIT,
                                   service_factory=lambda _: self.service, incoming_probe=lambda _: True)
        with patch.object(self.service, 'receive') as receive:
            result = json.loads(replacement.handle('pantheon_receive', dict(connection=self.handle), session_id='chosen'))
            self.assertEqual(result['status'], 'blocked')
            receive.assert_not_called()
        replacement.observe(session_id='other', platform='desktop')
        self.assertEqual(replacement.doctor('chosen')['status'], 'session_observation_pending')
        replacement.observe(session_id='chosen', platform='desktop')
        result = json.loads(replacement.handle('pantheon_receive', dict(connection=self.handle), session_id='chosen'))
        self.assertEqual(len(result['messages']), 1)

    def test_surface_change_during_pending_fetch_blocks_enqueue(self):
        original_request = self.client.request
        def change_during_fetch(route, payload=None):
            result = original_request(route, payload)
            self.plugin.observe(session_id='chosen', platform='cli')
            return result
        with patch.object(self.client, 'request', side_effect=change_during_fetch), self.assertRaises(Blocked):
            self.plugin.tick(self.handle, self.binding)
        self.ctx.inject_message.assert_not_called()

    def test_expiry_during_receive_fetch_prevents_claim_and_receipt(self):
        original = self.client.request
        def expire_on_fetch(route, payload=None):
            result = original(route, payload)
            save_private(self.setup, self.config | dict(trial_expires_at=1))
            return result
        with patch.object(self.client, 'request', side_effect=expire_on_fetch):
            result = json.loads(self.plugin.handle('pantheon_receive',
                dict(connection=self.handle), session_id='chosen'))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.client.commands, [])

    def test_expiry_during_pending_fetch_stops_before_notice(self):
        deadline = time.time()+300
        save_private(self.setup, self.config | dict(trial_expires_at=deadline))
        clock = Mock(return_value=deadline-1)
        original_request = self.client.request
        def expire_during_fetch(route, payload=None):
            result = original_request(route, payload)
            clock.return_value = deadline+1
            return result
        with patch('pantheon_connectors.runtime_policy.time.time', clock), patch.object(
                self.client, 'request', side_effect=expire_during_fetch):
            self.assertEqual(self.plugin.tick(self.handle, self.binding), 'trial_expired')
        self.ctx.inject_message.assert_not_called()
        self.assertEqual(self.client.commands, [])

    def test_wrong_session_profile_or_unobserved_session_never_fetches_or_injects(self):
        for binding in (self.binding | dict(session='other'), self.binding | dict(profile='other')):
            with self.subTest(binding=binding), patch.object(self.service, 'member') as member:
                with self.assertRaises(Blocked): self.plugin.tick(self.handle, binding)
                member.assert_not_called()
        self.plugin.observed.clear()
        with self.assertRaises(Blocked): self.plugin.tick(self.handle, self.binding)
        self.ctx.inject_message.assert_not_called()

    def test_notice_then_native_fetch_never_implicitly_acks_or_posts(self):
        self.assertEqual(self.plugin.tick(self.handle, self.binding), 'scheduled_not_received')
        result = json.loads(self.plugin.handle('pantheon_receive', dict(connection=self.handle), session_id='chosen'))
        self.assertEqual(len(result['messages']), 1)
        self.assertIn('PRIVATE PEER CANARY', json.dumps(result))
        self.assertNotIn('PRIVATE PEER CANARY', self.ctx.inject_message.call_args.args[0])
        self.assertEqual([op for op, _ in self.client.commands], ['claim', 'receipt'])
        json.loads(self.plugin.handle('pantheon_ack', dict(connection=self.handle,
                   message_ids=['message-1'], key='explicit-ack'), session_id='chosen'))
        self.assertEqual(self.client.commands[-1], ('ack', dict(ids=['message-1'], key='explicit-ack')))
        self.assertNotIn('post', [op for op, _ in self.client.commands])

    def test_join_without_runtime_consent_cannot_create_state_or_start_receiver(self):
        config = self.config | dict(state_dir=str(self.root/'never-created'), allow_runtime_turns=False)
        service = Onboarding(config, client_factory=lambda *_: self.public)
        with self.assertRaises(Blocked):
            service.join(self.link, self.binding, deliberate=True, preflight=Mock())
        self.assertFalse(Path(config['state_dir']).exists())
        self.assertNotIn('/v1/redeem', [call.args[0] for call in self.public.request.call_args_list])
        self.assertEqual(self.plugin.active, {})

    def test_saved_recovery_still_requires_current_allowance_and_preflight(self):
        path, _, _ = self.service.member(self.handle, self.binding)
        before = path.read_bytes()
        self.config['allow_runtime_turns'] = False
        preflight = Mock()
        with self.assertRaises(Blocked):
            self.service.join(self.link, self.binding, deliberate=True, preflight=preflight)
        preflight.assert_not_called()
        self.config['allow_runtime_turns'] = True
        preflight.side_effect = Blocked('synthetic host rejection')
        with self.assertRaises(Blocked):
            self.service.join(self.link, self.binding, deliberate=True, preflight=preflight)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('/v1/redeem', [call.args[0] for call in self.public.request.call_args_list])

    def test_different_invitation_requires_preview_even_with_other_saved_membership(self):
        before = sorted(Path(self.config['state_dir']).iterdir())
        def public(route, payload=None):
            if route == '/v1/manifest': return dict(protocol='pyros.v1', local_test=True)
            if route == '/v1/preview': raise APIError(401)
            raise AssertionError('No redemption is permitted after refused preview')
        self.public.request.side_effect = public
        with self.assertRaises(APIError):
            self.service.join(self.config['url']+'/join#invite='+'b'*40, self.binding,
                              deliberate=True, preflight=Mock())
        self.assertEqual(sorted(Path(self.config['state_dir']).iterdir()), before)
        self.assertNotIn('/v1/redeem', [call.args[0] for call in self.public.request.call_args_list])

    def test_cleanup_withdraw_survives_expiry_but_other_operations_do_not(self):
        self.config['trial_expires_at'] = time.time()-1
        for operation, args in (('pantheon_receive', {}), ('pantheon_ack', dict(message_ids=['message-1'], key='ack')),
                                ('pantheon_post', dict(text='synthetic', key='post'))):
            with self.subTest(operation=operation), self.assertRaises(Blocked):
                dispatch(self.service, operation, dict(connection=self.handle, **args),
                         self.binding, self.plugin.preflight, lambda: {})
        self.service.command(self.handle, self.binding, 'withdraw', key='cleanup')
        self.assertEqual(self.client.commands, [('withdraw', dict(key='cleanup'))])


@unittest.skipUnless(product_env('HTTP_TEST_URL'), 'requires disposable loopback Go fixture')
class NativeNotificationHTTP(unittest.TestCase):
    # The explicit test URL belongs to the suite's disposable server. CI uses
    # 8080 inside its isolated runner; the local harness selects an ephemeral port.
    def test_lost_redemption_response_recovers_exact_saved_membership(self):
        url = product_env('HTTP_TEST_URL')
        parsed = urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.hostname), ('http', '127.0.0.1'))
        credential = secrets.token_urlsafe(32)
        admin = Client(url, credential)
        admin.request('/v1/chats', dict(secret=credential, key=secrets.token_urlsafe(24),
            title='Lost native join fixture', purpose='Synthetic recovery; no native host', hours=4,
            slots=[dict(id='hermes', runtime='hermes', required=True)]))
        self.addCleanup(lambda: admin.command('end', key='fixture-end'))
        token = secrets.token_urlsafe(32)
        admin.command('invite', target='hermes', secret=token, hours=1)
        requests = []
        class LostResponse(Client):
            def request(inner, route, payload=None):
                result = super().request(route, payload)
                if route == '/v1/redeem':
                    requests.append(dict(payload))
                    if len(requests) == 1: raise APIError(0)
                return result
        with tempfile.TemporaryDirectory() as temporary:
            config = dict(url=url, state_dir=str(Path(temporary).resolve()/'state'),
                          allow_runtime_turns=True, trial_expires_at=time.time()+300)
            binding = dict(runtime='hermes', version='fixture', surface='native-adapter',
                           profile='fixture', session='chosen')
            link = url+'/join#invite='+token
            service = Onboarding(config, client_factory=LostResponse)
            with self.assertRaises(APIError) as lost:
                service.join(link, binding, deliberate=True, preflight=lambda _: None)
            self.assertEqual(lost.exception.status, 0)
            with self.assertRaises(APIError) as consumed: service.preview(link)
            self.assertEqual(consumed.exception.status, 401)
            # Restart the local object. Preview is now unavailable, but the exact
            # durable redemption request must reconcile the existing membership.
            recovered = Onboarding(config, client_factory=LostResponse)
            result = recovered.join(link, binding, deliberate=True, preflight=lambda _: None)
            self.assertEqual(result['status'], 'joined_awaiting_real_agent_probe')
            self.assertEqual(requests[0], requests[1])
            with self.assertRaises(Blocked):
                recovered.join(link, binding | dict(session='different'), deliberate=True, preflight=lambda _: None)
            self.assertEqual(len(requests), 2)

    def test_targeted_notice_fetch_ack_and_reply_over_real_protocol(self):
        url = product_env('HTTP_TEST_URL')
        parsed = urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.hostname), ('http', '127.0.0.1'))
        credential = secrets.token_urlsafe(32)
        admin = Client(url, credential)
        admin.request('/v1/chats', dict(secret=credential, key=secrets.token_urlsafe(24),
            title='Notification boundary fixture', purpose='Synthetic host; no model or user session', hours=4,
            slots=[dict(id=slot, runtime='hermes', required=True) for slot in ('a', 'b')]))
        self.addCleanup(lambda: admin.command('end', key='fixture-end'))
        with tempfile.TemporaryDirectory() as temporary:
            members = {}
            for slot in ('a', 'b'):
                token = secrets.token_urlsafe(32)
                admin.command('invite', target=slot, secret=token, hours=1)
                root = Path(temporary).resolve()/slot
                config = dict(url=url, state_dir=str(root/'state'), allow_runtime_turns=True,
                              trial_expires_at=time.time()+300)
                setup = root/'setup.json'; save_private(setup, config)
                ctx = Context()
                plugin = NativePlugin(ctx, setup, 'fixture', HERMES_COMMIT, incoming_probe=lambda _: True)
                plugin.observe(session_id=slot, platform='desktop')
                binding = plugin.binding(slot)
                service = Onboarding(config)
                handle = service.join(url+'/join#invite='+token, binding, deliberate=True,
                                      preflight=plugin.preflight)['connection']
                _, _, client = service.member(handle, binding)
                members[slot] = (plugin, ctx, binding, handle, client)

            def tool(slot, name, **args):
                plugin, _, _, handle, _ = members[slot]
                result = json.loads(plugin.handle(name, dict(connection=handle, **args), session_id=slot))
                self.assertNotEqual(result.get('status'), 'blocked', result)
                return result

            def consume(slot):
                client = members[slot][-1]
                pending = client.request('/v1/pending')
                if not pending: return
                tool(slot, 'pantheon_receive')
                for envelope in pending:
                    message = envelope['message']
                    tool(slot, 'pantheon_ack', message_ids=[message['id']], key='ack-'+message['id'])
                    if message['kind'] == 'probe':
                        tool(slot, 'pantheon_command', op='ready', text=message['text'], key='ready-'+message['id'])

            for slot in ('a', 'b', 'a', 'b'): consume(slot)
            self.assertEqual(admin.request('/v1/chat')['chat']['state'], 'active')
            challenge = tool('a', 'pantheon_post', text='TEST SENDER: synthetic challenge canary', key='challenge')
            plugin, ctx, binding, handle, client = members['b']
            self.assertEqual(plugin.tick(handle, binding), 'scheduled_not_received')
            ctx.inject_message.assert_called_once()
            self.assertTrue(ctx.inject_message.call_args.args[0].startswith(NOTICE+' Connection: '+handle+' notice_token: '))
            self.assertEqual(ctx.inject_message.call_args.kwargs, dict(session_key='b'))
            receipts = members['a'][-1].request('/v1/messages/'+challenge['id']+'/receipts')['b']
            self.assertFalse(receipts['received']); self.assertFalse(receipts['ack'])
            token = ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
            fetched = tool('b', 'pantheon_receive', notice_token=token)
            self.assertIn('synthetic challenge canary', json.dumps(fetched))
            # Simulate a lost native tool result; server claim/receipt retries
            # must preserve the same delivery, without another host injection.
            self.assertEqual(tool('b', 'pantheon_receive', notice_token=token)['messages'], fetched['messages'])
            ctx.inject_message.assert_called_once()
            receipts = members['a'][-1].request('/v1/messages/'+challenge['id']+'/receipts')['b']
            self.assertTrue(receipts['received']); self.assertFalse(receipts['ack'])
            tool('b', 'pantheon_ack', message_ids=[challenge['id']], key='explicit-challenge-ack')
            self.assertTrue(members['a'][-1].request('/v1/messages/'+challenge['id']+'/receipts')['b']['ack'])
            reply = tool('b', 'pantheon_post', text='Synthetic permitted reply', key='reply')
            pending = members['a'][-1].request('/v1/pending')
            self.assertIn(reply['id'], [e['message']['id'] for e in pending])
            self.assertEqual(members['a'][1].inject_message.call_count, 0)
            token = ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
            stale = tool('b', 'pantheon_receive', notice_token=token)
            self.assertEqual(stale['status'], 'stale_notice_noop')
            self.assertEqual(stale['messages'], [])
            self.assertEqual(plugin.tick(handle, binding), 'idle_or_notice_unresolved')
            ctx.inject_message.assert_called_once()
