import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors import codex_hosted as hosted
from pantheon_connectors.broker.codex_setup_policy import CONTRACT
from pantheon_connectors.runtime_policy import allowed
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked

THREAD = '00000000-0000-4000-8000-000000000111'
OTHER = '00000000-0000-4000-8000-000000000222'
ORIGIN = 'https://pantheon.talk'


class SetupConnectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        env = patch.dict(os.environ, CODEX_HOME=str(self.home)); env.start(); self.addCleanup(env.stop)
        metadata = dict(version=3, delivery='default-daemon-tool-output', service_origin=ORIGIN,
                        package_version='0.8.0', source_revision='b'*40, setup_contract=CONTRACT)
        descriptor = patch.object(hosted, 'descriptor', return_value=metadata); descriptor.start(); self.addCleanup(descriptor.stop)
        self.server = hosted.HostedServer(); self.addCleanup(self.server.close)
        discovery = {'packages': {'codex': {'version': '0.8.0', 'source_revision': 'b'*40}}}
        for name in ('fetch_discovery', 'validate_hosted_admission'):
            mocked = patch.object(hosted, name, return_value=discovery); mocked.start(); self.addCleanup(mocked.stop)
        self.provider = 'existing'
        def prepare(path, url, profile, **kwargs):
            save_private(path, dict(url=url, state_dir=str(path.parent/'state'), codex_home=str(self.home),
                binding=dict(runtime='codex', version='0.162.1', surface='app-server-tool-output', profile=profile, session=kwargs['native_thread']),
                delivery='default-daemon-tool-output', allow_runtime_turns=False, allow_tool_output=False,
                allow_daemon_access=False, trial_expires_at=0, notification_policy={'mode': 'unlimited'},
                reviewed_provider=self.provider, reviewed_account='metadata'))
            return dict(status='prepared_disabled', setup_file=str(path))
        mocked = patch.object(hosted, 'prepare', side_effect=prepare); self.prepare = mocked.start(); self.addCleanup(mocked.stop)
        self.answer = {'action': 'accept', 'content': dict(collaboration=True, minutes='0', transfers=False, sites=False)}
        self.server.elicit = Mock(side_effect=lambda *_: self.answer)
        def join(_child, method, params):
            if params['name'] == 'pantheon_receive':
                return self.server.result(dict(messages=[], has_more=False))
            self.assertEqual(params['name'], 'pantheon_join')
            _, connection = self.connection(params['arguments']['link'])
            return self.server.result(dict(status='joined', connection=connection))
        mocked = patch.object(hosted.Server, 'handle', new=join); mocked.start(); self.addCleanup(mocked.stop)

    def connection(self, link):
        token = link.split('=')[-1]
        connection = hashlib.sha256((ORIGIN+'\0'+token).encode()).hexdigest()[:32]
        return self.server.location(THREAD, connection), connection

    def call(self, name, args, thread=THREAD):
        return self.server.handle('tools/call', dict(name=name, arguments=args, _meta={'threadId': thread}))['structuredContent']

    def connect(self, token='a', thread=THREAD, **extra):
        link = ORIGIN+'/join#invite='+token*43
        return self.call('pantheon_connect', dict(link=link, deliberate=True, **extra), thread)

    def reload(self):
        elicit = self.server.elicit
        self.server.close()
        self.server = hosted.HostedServer(); self.addCleanup(self.server.close)
        self.server.elicit = elicit

    def first_connection(self, **extra):
        self.assertEqual(self.connect(**extra)['status'], 'native_reload_required')
        self.reload()
        return self.connect(**extra)

    def test_review_cannot_start_chat_before_native_reload_even_on_retries(self):
        for _ in range(2):
            result = self.connect()
            self.assertEqual(result['status'], 'native_reload_required')
            self.assertFalse(result['runtime_started'])
            self.assertFalse(result['membership_started'])
            self.assertEqual(result['next_action'], 'end_turn_then_reload_same_conversation')
        self.assertEqual(self.call('pantheon_setup', {'action': 'status'})['status'], 'native_reload_required')
        self.server.elicit.assert_called_once()
        self.assertEqual(self.prepare.call_count, 1)  # Review inspection only.
        self.assertEqual(list(self.server.root.rglob('setup.json')), [])
        self.assertEqual(self.server.children, {})
        self.assertEqual(self.server.setup_policy.read()['claims'], [])

    def test_direct_review_and_changed_setup_each_need_new_native_process(self):
        self.assertEqual(self.call('pantheon_setup', {'action': 'review'})['status'], 'native_reload_required')
        self.reload()
        first = self.connect()
        old = load_private(self.server.location(THREAD, first['connection']))
        self.assertEqual(self.call('pantheon_setup', {'action': 'change'})['status'], 'native_reload_required')
        self.assertFalse(allowed(old))
        self.assertEqual(self.connect('b')['status'], 'native_reload_required')
        self.reload()
        self.assertEqual(self.connect('b')['onboarding_status'], 'connected')
        self.assertEqual(self.connect()['status'], 'runtime_inactive')
        self.assertEqual(self.server.elicit.call_count, 2)

    def test_one_form_cold_and_link_only_returning_each_with_independent_grant(self):
        cold = self.first_connection()
        warm = self.connect('b', OTHER)
        self.assertEqual(cold['onboarding_status'], 'connected')
        self.assertEqual(warm['onboarding_status'], 'connected')
        self.assertNotEqual(cold['connection'], warm['connection'])
        self.server.elicit.assert_called_once()
        for thread, result in ((THREAD, cold), (OTHER, warm)):
            config = load_private(self.server.location(thread, result['connection']))
            self.assertTrue(allowed(config))
            self.assertEqual(config['binding']['session'], thread)
        self.assertEqual(self.prepare.call_count, 3)  # One review, one preparation per chat.

    def test_closed_scope_never_renews_even_after_setup_changes(self):
        first = self.first_connection(); path = self.server.location(THREAD, first['connection'])
        value = load_private(path); value['allow_runtime_turns'] = False; save_private(path, value)
        original = path.read_bytes()
        self.call('pantheon_setup', {'action': 'change'})
        result = self.connect()
        self.assertEqual(result['status'], 'runtime_inactive')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.server.elicit.call_count, 2)

    def test_decline_and_model_approval_arguments_cannot_join_or_inspect(self):
        self.answer = {'action': 'decline'}
        result = self.connect(expires_at=time.time()+600)
        self.assertEqual(result['status'], 'setup_declined'); self.prepare.assert_not_called()
        for name, args in [('pantheon_setup', {'action': 'review', 'approved': True}),
                           ('pantheon_authorize', {'connection': 'a'*32, 'deliberate': True, 'expires_at': time.time()+60})]:
            with self.assertRaises(Blocked): self.call(name, args)

    def test_model_permission_arguments_and_invalid_deadlines_fail_before_review(self):
        for args in ({'consent': {'native_access': True, 'existing_provider_usage': True}},
                     {'approved': True}, *({'expires_at': value} for value in
                       (True, '1234', float('nan'), float('inf'), time.time()-1, time.time()+3700))):
            with self.assertRaises(Blocked): self.connect(**args)
        self.server.elicit.assert_not_called()
        self.prepare.assert_not_called()

    def test_changed_provider_stops_new_chat_without_reasking_or_enabling(self):
        self.first_connection(); self.provider = 'changed'
        with self.assertRaisesRegex(Blocked, 'metadata changed'):
            self.connect('b')
        self.server.elicit.assert_called_once()
        link = ORIGIN+'/join#invite='+'b'*43
        path, _ = self.connection(link)
        self.assertFalse(allowed(load_private(path)))

    def test_explicit_audit_deadline_is_preserved_and_cannot_exceed_setup_window(self):
        self.answer['content']['minutes'] = '30'
        deadline = time.time()+600
        result = self.first_connection(expires_at=deadline)
        config = load_private(self.server.location(THREAD, result['connection']))
        self.assertEqual(config['trial_expires_at'], deadline)
        # A subsequent link alone cannot inherit a later setup-relative deadline.
        warm = self.connect('b')
        self.assertEqual(load_private(self.server.location(THREAD, warm['connection']))['trial_expires_at'], deadline)
        self.connect(expires_at=deadline)
        with self.assertRaisesRegex(Blocked, 'original approved lifetime'):
            self.connect(expires_at=deadline-60)
        with self.assertRaisesRegex(Blocked, 'fixed setup window'):
            self.connect('c', expires_at=time.time()+2400)

    def test_tools_expose_native_review_instead_of_model_authorization(self):
        names = {item['name'] for item in self.server.handle('tools/list', {})['tools']}
        self.assertIn('pantheon_setup', names)
        self.assertNotIn('pantheon_authorize', names)
        self.assertNotIn('pantheon_prepare', names)
        schema = next(item for item in self.server.handle('tools/list', {})['tools']
                      if item['name'] == 'pantheon_connect')['inputSchema']['properties']
        self.assertIn('expires_at', schema)
        self.assertNotIn('consent', schema)
