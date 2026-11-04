"""Connect composes real adapter preparation/authorization without native calls."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pantheon_connectors.broker import connect
from pantheon_connectors.state import JournalBusy, load_private, save_private
from pantheon_connectors.worker import Blocked
from pantheon_connectors.client import APIError
import test_codex_hosted as codex_fixture
import test_hermes_hosted_setup as hermes_fixture
from test_codex_hosted import LINK, THREAD, CONNECTION


class ConnectContract(unittest.TestCase):
    def retry_fixture(self, join, inspect=None, receive=None):
        state = dict(previous=True, active=True, connection=CONNECTION,
                     lifetime={'expires_at': time.time()+500}, provider={'model': 'approved'})
        authorize = Mock()
        with patch('pantheon_connectors.broker.connect.time.sleep'):
            value = connect.run(dict(link=LINK, deliberate=True),
                inspect=inspect or (lambda: state), prepare=lambda _: {},
                authorize=authorize, join=join, preview=Mock(), receive=receive)
        authorize.assert_not_called()
        return value

    def test_initial_read_is_returned_without_ack_or_reauthorization(self):
        initial = {'messages': [{'message': {'id': 'read-me'}}], 'has_more': True}
        join, receive = Mock(return_value={'connection': CONNECTION}), Mock(return_value=initial)
        result = self.retry_fixture(join, receive=receive)
        join.assert_called_once_with()
        receive.assert_called_once_with(CONNECTION)
        self.assertEqual(result['initial_input'], initial)
        self.assertEqual(result['next_action'], 'read_returned_input_then_explicit_ack')
        self.assertFalse(result['delivery_verified'])

    def test_initial_read_failure_preserves_join_and_original_grant(self):
        join, receive = Mock(return_value={'connection': CONNECTION}), Mock(side_effect=APIError(503))
        result = self.retry_fixture(join, receive=receive)
        self.assertEqual(result['onboarding_status'], 'connected')
        self.assertEqual(result['initial_input_status'], 'pending')
        self.assertNotIn('initial_input', result)
        self.assertFalse(result['delivery_verified'])
        join.assert_called_once_with()
        receive.assert_called_once_with(CONNECTION)
        for error in (APIError(401), APIError(403), APIError(410), Blocked('changed scope')):
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.retry_fixture(Mock(return_value={'connection': CONNECTION}), receive=Mock(side_effect=error))

    def test_initial_read_rechecks_scope_and_rejects_malformed_results(self):
        state = dict(previous=True, active=True, connection=CONNECTION,
                     lifetime={'expires_at': time.time()+500}, provider={'model': 'approved'})
        for change in ({'active': False}, {'lifetime': {'expires_at': time.time()+600}},
                       {'provider': {'model': 'changed'}}, {'connection': 'different'}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                self.retry_fixture(Mock(return_value={'connection': CONNECTION}),
                    inspect=Mock(side_effect=[state, state, state, {**state, **change}]),
                    receive=Mock(return_value={'messages': []}))
        for initial in (None, [], {'messages': 'invalid'}, {'connection': CONNECTION}):
            with self.subTest(initial=initial), self.assertRaises(Blocked):
                self.retry_fixture(Mock(return_value={'connection': CONNECTION}), receive=Mock(return_value=initial))

    def test_transient_join_resumes_once_and_exposes_handle_if_still_pending(self):
        for failure in (APIError(503), {'status': 'blocked', 'retry_same_join': True}):
            with self.subTest(failure=failure):
                join = Mock(side_effect=[failure, {'connection': CONNECTION}])
                result = self.retry_fixture(join)
                self.assertEqual(result['onboarding_status'], 'connected')
                self.assertFalse(result['delivery_verified'])
                self.assertEqual(join.call_count, 2)
        join = Mock(side_effect=APIError(503))
        result = self.retry_fixture(join)
        self.assertEqual(result['status'], 'connection_pending')
        self.assertEqual(result['connection'], CONNECTION)
        self.assertFalse(result['delivery_verified'])
        self.assertEqual(join.call_count, 2)

    def test_join_recovery_never_retries_authority_tls_or_local_errors(self):
        for error in (APIError(401), APIError(403), APIError(410), APIError(409),
                      APIError(0, 'tls_verification_failed'), Blocked('changed binding')):
            with self.subTest(error=error):
                join = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    self.retry_fixture(join)
                self.assertEqual(join.call_count, 1)

    def test_join_recovery_rechecks_original_grant_before_retry(self):
        state = dict(previous=True, active=True, connection=CONNECTION,
                     lifetime={'expires_at': time.time()+500}, provider={'model': 'approved'})
        for change in ({'active': False}, {'lifetime': {'expires_at': time.time()+600}},
                       {'provider': {'model': 'changed'}}, {'connection': 'different'}):
            with self.subTest(change=change):
                inspect = Mock(side_effect=[state, state, {**state, **change}])
                join = Mock(side_effect=APIError(503))
                if change.get('active') is False:
                    self.assertEqual(self.retry_fixture(join, inspect)['status'], 'runtime_inactive')
                else:
                    with self.assertRaises(Blocked):
                        self.retry_fixture(join, inspect)
                self.assertEqual(join.call_count, 1)

    def test_missing_review_is_read_only_and_inactive_scope_never_reopens(self):
        prepare, authorize, join = Mock(), Mock(), Mock()
        values = dict(link=LINK, deliberate=True)
        callbacks = dict(prepare=prepare, authorize=authorize, join=join, preview=lambda: {'title': 'test'})
        result = connect.run(values, inspect=lambda: {}, **callbacks)
        self.assertEqual(result['status'], 'consent_required')
        result = connect.run(values, inspect=lambda: {'previous': True, 'active': False}, **callbacks)
        self.assertEqual(result['next_action'], 'stop_inactive_grant')
        for callback in (prepare, authorize, join):
            callback.assert_not_called()

    def test_review_cannot_move_conversations_or_expiry_or_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'review.json'
            review = connect.Review(path, {'session': 'one'})
            end = time.time() + 500
            consent = dict(native_access=True, existing_provider_usage=True, runtime_policy={'mode': 'connection'}, remember_until=end)
            review.remember(consent, {'provider': {'model': 'approved'}})
            self.assertEqual(review.recall()['consent']['expires_at'], end)
            with self.assertRaises(Blocked):
                connect.Review(path, {'session': 'two'}).recall()
            with self.assertRaises(Blocked):
                review.remember({**consent, 'remember_until': end+1}, {'provider': {'model': 'approved'}})
            authorize = Mock()
            with self.assertRaises(Blocked):
                connect.run(dict(link=LINK, deliberate=True), inspect=lambda: {'provider': {'model': 'changed'}},
                    prepare=lambda _: {}, authorize=authorize, join=Mock(), preview=Mock(), recall=review.recall)
            authorize.assert_not_called()
            with patch('time.time', return_value=end+1):
                self.assertIsNone(review.recall())
                review.remember({**consent, 'remember_until': end+100}, {'provider': {'model': 'approved'}})
                self.assertEqual(review.recall()['consent']['expires_at'], end+100)

    def test_consent_is_explicit_finite_and_strict(self):
        good = dict(native_access=True, existing_provider_usage=True, expires_at=time.time()+600)
        for change in ({'expires_at': True}, {'expires_at': float('nan')}, {'expires_at': time.time()+7200},
                       {'native_access': False}, {'runtime_policy': {'mode': 'connection'}}, {'unexpected': True}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                connect.validate(dict(link=LINK, deliberate=True, consent={**good, **change}))


class CodexConnect(unittest.TestCase):
    setUp = codex_fixture.HostedCodexTests.setUp
    call = codex_fixture.HostedCodexTests.call

    def test_transient_child_join_preserves_native_authorization(self):
        consent = dict(native_access=True, existing_provider_usage=True, expires_at=time.time()+600)
        with patch('pantheon_connectors.broker.connect.time.sleep'), patch(
                'pantheon_connectors.codex_hosted.Server.handle', side_effect=[
                    APIError(503), {'structuredContent': {'connection': CONNECTION}},
                    {'structuredContent': {'messages': [], 'has_more': False}}]) as child:
            result = self.call('pantheon_connect', dict(link=LINK, deliberate=True, consent=consent))
        self.assertEqual(result['onboarding_status'], 'connected')
        self.assertEqual(child.call_count, 3)
        self.assertEqual(self.prepare.call_count, 1)
        self.assertEqual(load_private(self.server.location(THREAD, CONNECTION))['trial_expires_at'], consent['expires_at'])

    def test_fresh_join_retry_preserves_grant_and_returns_explicit_ack_instruction(self):
        consent = dict(native_access=True, existing_provider_usage=True, expires_at=time.time()+600)
        args = dict(link=LINK, deliberate=True, consent=consent)
        with patch('pantheon_connectors.codex_hosted.Server.handle', side_effect=lambda _, params: {'structuredContent':
                {'messages': [], 'has_more': False} if params['name'] == 'pantheon_receive' else {'connection': CONNECTION}}) as child:
            first = self.call('pantheon_connect', args)
            saved = load_private(self.server.location(THREAD, CONNECTION))
            retry = self.call('pantheon_connect', dict(link=LINK, deliberate=True))
            self.assertEqual(load_private(self.server.location(THREAD, CONNECTION)), saved)
            self.assertEqual(self.prepare.call_count, 1)
            self.assertTrue(retry['consent_reused'])
            self.assertFalse(first['delivery_verified'])
            self.assertEqual(first['next_action'], 'read_returned_input_then_explicit_ack')
            self.assertEqual([call.args[1]['name'] for call in child.call_args_list],
                             ['pantheon_join', 'pantheon_receive'] * 2)
            save_private(self.server.location(THREAD, CONNECTION), {**saved, 'allow_runtime_turns': False})
            self.assertEqual(self.call('pantheon_connect', args)['status'], 'runtime_inactive')
            self.assertEqual(child.call_count, 4)

    def test_native_identity_and_same_connection_are_required(self):
        with self.assertRaises(Blocked):
            self.call('pantheon_connect', dict(link=LINK, deliberate=True), 'guessed')
        with patch('pantheon_connectors.codex_hosted.Onboarding.preview', return_value={'scope': {'title': 'Test'}}):
            result = self.call('pantheon_connect', dict(link=LINK, deliberate=True))
        self.assertEqual(result['status'], 'consent_required')
        self.prepare.assert_not_called()

    def test_explicit_remembered_review_creates_fresh_bounded_chat_grant_only_here(self):
        import hashlib
        end = time.time()+500
        consent = dict(native_access=True, existing_provider_usage=True, runtime_policy={'mode': 'connection'}, remember_until=end)
        def joined(method, params):
            if params['name'] == 'pantheon_receive':
                return {'structuredContent': {'messages': [], 'has_more': False}}
            token = params['arguments']['link'].split('invite=')[1]
            identity = hashlib.sha256(('https://pantheon.talk\0'+token).encode()).hexdigest()[:32]
            return {'structuredContent': {'connection': identity}}
        with patch('pantheon_connectors.codex_hosted.Server.handle', side_effect=joined):
            first = self.call('pantheon_connect', dict(link=LINK, deliberate=True, consent=consent))
            second = self.call('pantheon_connect', dict(link=LINK.replace('a'*43, 'b'*43), deliberate=True))
        self.assertNotEqual(first['connection'], second['connection'])
        self.assertTrue(second['consent_reused'])
        original = load_private(self.server.location(THREAD, first['connection']))
        fresh = load_private(self.server.location(THREAD, second['connection']))
        self.assertEqual(original['runtime_policy'], {'mode': 'connection'})
        self.assertNotIn('runtime_policy', fresh)
        self.assertEqual(fresh['trial_expires_at'], end)
        with patch('pantheon_connectors.codex_hosted.Onboarding.preview', return_value={'scope': {'title': 'test'}}):
            other = self.call('pantheon_connect', dict(link=LINK, deliberate=True), codex_fixture.OTHER)
        self.assertEqual(other['status'], 'consent_required')


class HermesConnect(unittest.TestCase):
    setUp = hermes_fixture.HostedHermesTests.setUp

    def test_initial_journal_contention_retains_connected_handle_and_deadline(self):
        from pantheon_connectors.hermes_plugin import NativePlugin
        from pantheon_connectors.hermes_setup import handle_for
        identity = handle_for(self.link)
        deadline = time.time()+300
        consent = dict(native_access=True, existing_provider_usage=True, expires_at=deadline)
        with patch('pantheon_connectors.onboarding.Onboarding.preview', return_value={'scope': {'expires': '2099-01-01T00:00:00Z'}}), patch.object(NativePlugin, 'require_live_connection'), patch.object(NativePlugin, 'start') as start, patch(
                'pantheon_connectors.hermes_plugin.dispatch', side_effect=[
                    {'connection': identity}, JournalBusy('Another process is consuming this journal'),
                    {'messages': [], 'has_more': False}]) as dispatch:
            result = json.loads(self.plugin.handle('pantheon_connect',
                dict(link=self.link, deliberate=True, consent=consent), session_id='chosen'))
            self.assertEqual(result.get('onboarding_status'), 'connected', result)
            self.assertEqual(result['connection'], identity)
            self.assertEqual(result['initial_input_status'], 'pending')
            self.assertNotIn('initial_input', result)
            self.assertFalse(result['delivery_verified'])
            child = next(iter(self.plugin.prepared.values()))
            saved = load_private(child.setup_file)
            self.assertEqual(saved['trial_expires_at'], deadline)
            resumed = json.loads(self.plugin.handle('pantheon_receive',
                {'connection': identity}, session_id='chosen'))
            self.assertEqual(resumed['messages'], [])
            self.assertEqual(load_private(child.setup_file), saved)
        self.assertEqual([call.args[1] for call in dispatch.call_args_list],
                         ['pantheon_join', 'pantheon_receive', 'pantheon_receive'])
        start.assert_called_once()

    def test_transient_child_join_returns_recovery_evidence_to_connect(self):
        from pantheon_connectors.hermes_plugin import NativePlugin
        from pantheon_connectors.hermes_setup import handle_for
        identity = handle_for(self.link)
        consent = dict(native_access=True, existing_provider_usage=True, runtime_policy={'mode': 'connection'})
        with patch('pantheon_connectors.broker.connect.time.sleep'), patch.object(NativePlugin, 'require_live_connection'), patch.object(NativePlugin, 'start') as start, patch(
                'pantheon_connectors.hermes_plugin.dispatch', side_effect=[APIError(503), {'connection': identity}, {'messages': [], 'has_more': False}]) as dispatch:
            result = json.loads(self.plugin.handle('pantheon_connect',
                dict(link=self.link, deliberate=True, consent=consent), session_id='chosen'))
        self.assertEqual(result.get('onboarding_status'), 'connected', result)
        self.assertEqual(dispatch.call_count, 3)
        start.assert_called_once()
        self.assertEqual(len(self.plugin.prepared), 1)

    def test_completed_scope_stays_off_while_remembered_review_joins_a_fresh_chat(self):
        from pantheon_connectors.hermes_plugin import NativePlugin
        original = NativePlugin._handle
        def handle(plugin, name, args, **kwargs):
            if name == 'pantheon_receive':
                return json.dumps({'messages': [], 'has_more': False})
            if name == 'pantheon_join':
                return json.dumps({'connection': load_private(plugin.setup_file)['prepared_connection']})
            return original(plugin, name, args, **kwargs)
        end = time.time()+500
        consent = dict(native_access=True, existing_provider_usage=True, expires_at=end, remember_until=end)
        with patch.object(NativePlugin, '_handle', handle), patch('pantheon_connectors.onboarding.Onboarding.preview', return_value={'scope': {'expires': '2099-01-01T00:00:00Z'}}):
            first = json.loads(self.plugin.handle('pantheon_connect', dict(link=self.link, deliberate=True, consent=consent), session_id='chosen'))
            self.assertEqual(first.get('onboarding_status'), 'connected', first)
            child = next(iter(self.plugin.prepared.values()))
            saved = load_private(child.setup_file)
            stopped = {**saved, 'allow_runtime_turns': False, 'trial_expires_at': 0}
            save_private(child.setup_file, stopped)
            again = json.loads(self.plugin.handle('pantheon_connect', dict(link=self.link, deliberate=True), session_id='chosen'))
            self.assertEqual(again['status'], 'runtime_inactive')
            second = json.loads(self.plugin.handle('pantheon_connect', dict(link=self.link.replace('Q'*40, 'R'*40), deliberate=True), session_id='chosen'))
            self.assertEqual(second.get('onboarding_status'), 'connected', second)
            self.assertTrue(second['consent_reused'])
            self.assertNotEqual(first['connection'], second['connection'])
            self.assertEqual(load_private(child.setup_file), stopped)
            fresh = next(p for p in self.plugin.prepared.values() if p is not child)
            self.assertEqual(load_private(fresh.setup_file)['trial_expires_at'], end)
            self.assertFalse(self.plugin.stop.is_set())

    def test_fixed_deadline_and_idempotent_connect(self):
        from pantheon_connectors.hermes_plugin import NativePlugin
        from pantheon_connectors import hermes_limits
        original = NativePlugin._handle
        def handle(plugin, name, args, **kwargs):
            if name == 'pantheon_receive':
                return json.dumps({'messages': [], 'has_more': False})
            if name == 'pantheon_join':
                return json.dumps({'connection': load_private(plugin.setup_file)['prepared_connection']})
            return original(plugin, name, args, **kwargs)
        end = time.time()+500
        args = dict(link=self.link, deliberate=True, consent=dict(native_access=True, existing_provider_usage=True, expires_at=end))
        with patch.object(NativePlugin, '_handle', handle), patch('pantheon_connectors.onboarding.Onboarding.preview', return_value={'scope': {'expires': '2099-01-01T00:00:00Z'}}):
            result = json.loads(self.plugin.handle('pantheon_connect', args, session_id='chosen'))
            self.assertEqual(result.get('onboarding_status'), 'connected', result)
            child = next(iter(self.plugin.prepared.values()))
            saved = load_private(child.setup_file)
            self.assertEqual(saved['trial_expires_at'], end)
            self.assertTrue(hermes_limits.finite_grant_matches(saved))
            result = json.loads(self.plugin.handle('pantheon_connect', {'link': self.link, 'deliberate': True}, session_id='chosen'))
            self.assertTrue(result.get('consent_reused'), result)
            self.assertEqual(load_private(child.setup_file), saved)
            self.plugin.observe(session_id='chosen', platform='desktop', model='different')
            result = json.loads(self.plugin.handle('pantheon_connect', args, session_id='chosen'))
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(load_private(child.setup_file), saved)
