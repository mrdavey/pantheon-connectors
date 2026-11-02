import json
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors import hermes_incoming
from pantheon_connectors.hermes_plugin import NativePlugin
from pantheon_connectors.state import load_private
import test_hermes_hosted_setup as fixture


class NativePermissionProbe(unittest.TestCase):
    def test_only_native_json_true_grants_permission_in_the_exact_profile(self):
        for result, expected in [(subprocess.CompletedProcess([], 0, 'true\n', ''), True),
                                 (subprocess.CompletedProcess([], 0, 'true\n', 'Warning: using default'), False),
                                 (subprocess.CompletedProcess([], 0, '"true"\n', ''), False),
                                 (subprocess.CompletedProcess([], 0, 'false\n', ''), False),
                                 (subprocess.CompletedProcess([], 1, '', 'Config key not set'), False),
                                 (subprocess.CompletedProcess([], 0, 'invalid', ''), False)]:
            with self.subTest(result=result), patch.object(hermes_incoming.subprocess, 'run', return_value=result) as run:
                self.assertIs(hermes_incoming.allowed('exact-profile'), expected)
                self.assertEqual(run.call_args.args[0], ['hermes', '--profile', 'exact-profile', 'config',
                    'get', 'plugins.entries.pantheon.allow_gateway_injection', '--json'])
                self.assertEqual(run.call_args.kwargs['timeout'], 15)
                self.assertNotIn('shell', run.call_args.kwargs)

    def test_unavailable_host_fails_closed_without_writing_any_setting(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('hermes', 15)):
            with patch.object(hermes_incoming.subprocess, 'run', side_effect=error):
                self.assertFalse(hermes_incoming.allowed('default'))
        result = hermes_incoming.PermissionRequired('observed profile').result()
        self.assertIn("--profile 'observed profile'", result['native_commands']['inspect'])
        self.assertIn('explicit_approval', next(k for k in result['native_commands'] if k != 'inspect'))


class IncomingConnect(unittest.TestCase):
    setUp = fixture.HostedHermesTests.setUp
    prepare = fixture.HostedHermesTests.prepare
    authorize = fixture.HostedHermesTests.authorize

    def test_direct_authorization_cannot_write_a_grant_without_host_permission(self):
        prepared = self.prepare()
        child = next(iter(self.plugin.prepared.values()))
        before = load_private(child.setup_file)
        child.incoming_probe = lambda _: False
        value = self.authorize(prepared['connection'])
        self.assertEqual(value['host_incoming_permission'], 'missing_or_unverified')
        self.assertEqual(load_private(child.setup_file), before)

    def test_missing_permission_blocks_before_prepare_grant_or_redemption_then_same_deadline_retries(self):
        self.plugin.incoming_probe = Mock(return_value=False)
        deadline = time.time()+300
        args = dict(link=self.link, deliberate=True, consent=dict(native_access=True,
                    existing_provider_usage=True, expires_at=deadline))
        with patch('pantheon_connectors.hermes_setup.prepare') as prepare:
            value = json.loads(self.plugin.handle('pantheon_connect', args, session_id='chosen'))
            self.assertEqual(value['next_action'], 'verify_reviewed_host_permission_then_retry_same_scope')
            prepare.assert_not_called()
        self.assertFalse(self.plugin.prepared)
        self.assertEqual(list(self.root.rglob('setup.json')), [])
        original = NativePlugin._handle
        def joined(plugin, name, args, **kwargs):
            if name == 'pantheon_receive':
                return json.dumps({'messages': [], 'has_more': False})
            if name == 'pantheon_join':
                return json.dumps({'connection': load_private(plugin.setup_file)['prepared_connection']})
            return original(plugin, name, args, **kwargs)
        self.plugin.incoming_probe.return_value = True
        with patch.object(NativePlugin, '_handle', joined), patch('pantheon_connectors.onboarding.Onboarding.preview',
                return_value={'scope': {'expires': '2099-01-01T00:00:00Z'}}):
            value = json.loads(self.plugin.handle('pantheon_connect', args, session_id='chosen'))
        self.assertEqual(value['onboarding_status'], 'connected')
        self.assertEqual(value['default_folders'], {'transfers': str(self.plugin.prepare_root / 'features/files'),
            'sites': str(self.plugin.prepare_root / 'features/sites')})
        self.assertFalse((self.plugin.prepare_root / 'features').exists())
        child = next(iter(self.plugin.prepared.values()))
        self.assertEqual(load_private(child.setup_file)['trial_expires_at'], deadline)
        self.plugin.incoming_probe.return_value = False
        self.assertEqual(child.doctor('chosen')['host_incoming_permission'], 'missing_or_unverified')
        with self.assertRaises(hermes_incoming.PermissionRequired):
            child.start(value['connection'], child.binding('chosen'))
        self.assertFalse(child.active)

    def test_review_only_does_not_inspect_host_permission(self):
        self.plugin.incoming_probe = Mock(side_effect=AssertionError('No approval yet'))
        with patch('pantheon_connectors.onboarding.Onboarding.preview', return_value={'scope': {'title': 'Synthetic'}}):
            value = json.loads(self.plugin.handle('pantheon_connect', dict(link=self.link, deliberate=True), session_id='chosen'))
        self.assertEqual(value['status'], 'consent_required')
        self.assertEqual(value['default_folders']['transfers'], str(self.plugin.prepare_root / 'features/files'))
        self.assertFalse((self.plugin.prepare_root / 'features').exists())
        self.plugin.incoming_probe.assert_not_called()
