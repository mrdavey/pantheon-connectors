"""Registration contract without a host process, user profile or model call."""
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from pantheon_connectors.hermes_plugin import register, SKILL_PATH, IDENTITY_HOOKS
from pantheon_connectors.onboarding import HERMES_COMMIT, TOOLS
from pantheon_connectors.hermes_tools import TOOLS as HERMES_TOOLS
from pantheon_connectors.hermes_setup import TOOLS as PREPARE_TOOLS
from pantheon_connectors.broker.connect import TOOL as CONNECT_TOOL
from pantheon_connectors.worker import Blocked


class Context:
    profile_name = 'fixture'

    def __init__(self):
        self.skills, self.hooks, self.tools, self.cleanup = {}, {}, {}, []

    def get_config(self, name, default): return default
    def register_skill(self, name, path, description):
        if not Path(path).is_file(): raise FileNotFoundError(path)
        self.skills[name] = (path, description)
    def register_hook(self, name, handler): self.hooks[name] = handler
    def register_tool(self, **tool): self.tools[tool['name']] = tool
    def on_unload(self, callback): self.cleanup.append(callback)
    def inject_message(self, *args, **kwargs): raise AssertionError('No injection in registration tests')


class RegistrationTests(unittest.TestCase):
    def test_broker_registration_needs_no_network_or_remote_catalogue(self):
        with tempfile.TemporaryDirectory() as temp, patch('pantheon_connectors.broker.package.metadata', return_value={'contract': 'pantheon.broker.v1'}), patch(
                'pantheon_connectors.broker.adapter.ToolSnapshot.refresh', side_effect=RuntimeError('Plugin Doctor forbids network')) as refresh:
            ctx = Context()
            plugin = self.load(ctx, Path(temp).resolve(), broker=True)
            try:
                refresh.assert_not_called()
                self.assertIn('pantheon_connect', ctx.tools)
                self.assertIn('pre_tool_call', ctx.hooks)
                self.assertEqual(plugin.active, {})
                self.assertIsNone(plugin.catalogue.revision)
            finally:
                for close in reversed(ctx.cleanup): close()

    def test_doctor_reports_loaded_build_independently_of_current_disk_metadata(self):
        from pantheon_connectors.hermes_plugin import NativePlugin, LOADED_CONNECTOR_VERSION
        with tempfile.TemporaryDirectory() as temp:
            plugin = NativePlugin(Context(), Path(temp)/'setup.json', 'host-version', 'host-commit')
            try:
                with patch('pantheon_connectors.broker.package.metadata', return_value={'package_version':'999.0.0'}):
                    self.assertEqual(plugin.doctor('native')['loaded_connector_version'], LOADED_CONNECTOR_VERSION)
            finally:
                plugin.close()

    def test_registration_rejects_missing_public_capability_without_side_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            ctx = Context()
            ctx.inject_message = lambda message: None  # Cannot target the exact native session.
            with self.assertRaisesRegex(Blocked, 'inject_message'):
                self.load(ctx, Path(temp))
            self.assertEqual(ctx.skills, {})
            self.assertEqual(ctx.hooks, {})
            self.assertEqual(ctx.tools, {})

    def test_non_git_host_uses_public_version_without_requiring_a_checkout(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            constants = SimpleNamespace(__file__=str(home/'hermes_constants.py'), get_hermes_home=lambda: home)
            with patch('pantheon_connectors.broker.package.metadata', return_value=None), patch.dict('sys.modules', hermes_constants=constants, hermes_cli=SimpleNamespace(__version__='99.1.0')), patch('pantheon_connectors.hermes_plugin.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'git')):
                plugin = register(Context())
            try:
                self.assertEqual(plugin.version, 'hermes:99.1.0')
                plugin.observe(session_id='native', platform='desktop')
                plugin.preflight(plugin.binding('native'))
            finally:
                plugin.close()

    def test_updated_host_commit_is_evidence_not_an_allowlist(self):
        from pantheon_connectors.hermes_plugin import NativePlugin
        with tempfile.TemporaryDirectory() as temp:
            ctx = Context()
            for commit in ('1' * 40, '2' * 40):
                plugin = NativePlugin(ctx, Path(temp)/'setup.json', 'git:'+commit, commit)
                try:
                    plugin.observe(session_id='native', platform='desktop')
                    plugin.preflight(plugin.binding('native'))
                    with self.assertRaisesRegex(Blocked, 'identity changed'):
                        plugin.preflight(dict(plugin.binding('native'), version='different'))
                finally:
                    plugin.close()

    def test_declared_hooks_match_native_registration(self):
        manifest = (Path(__file__).resolve().parents[2]/'plugin.yaml').read_text()
        section = manifest.split('provides_hooks:\n',1)[1].split('config_schema:',1)[0]
        declared = {line.strip()[2:] for line in section.splitlines() if line.strip().startswith('- ')}
        from pantheon_connectors.broker.package import metadata
        expected = set(IDENTITY_HOOKS) | ({'pre_tool_call'} if metadata() else set())
        self.assertEqual(declared, expected)

    def test_declared_tools_match_registered_tools_including_sites_and_transfers(self):
        manifest = (Path(__file__).resolve().parents[2]/'plugin.yaml').read_text()
        declared = {line.strip()[2:] for line in manifest.split('provides_tools:\n',1)[1].split('provides_hooks:',1)[0].splitlines() if line.strip().startswith('- ')}
        with tempfile.TemporaryDirectory() as temp:
            ctx = Context()
            from pantheon_connectors.broker.package import metadata
            broker = metadata() is not None
            plugin = self.load(ctx, Path(temp).resolve(), broker=broker)
            self.addCleanup(plugin.close)
            from pantheon_connectors.broker.catalog import LOCAL
            expected = [tool for tool in TOOLS if not broker or tool['name'] in LOCAL]
            self.assertEqual(set(ctx.tools), {t['name'] for t in [CONNECT_TOOL, *expected, *HERMES_TOOLS, *PREPARE_TOOLS]})
            self.assertEqual(declared, set(ctx.tools))
            for name in ('pantheon_site_deploy', 'pantheon_site_status', 'pantheon_transfer_send', 'pantheon_transfer_inspect', 'pantheon_transfer_fetch', 'pantheon_transfer_ack'):
                if broker and name not in LOCAL: continue
                self.assertEqual(ctx.tools[name]['toolset'], 'pantheon')
                with patch.object(plugin, 'handle', return_value='fixture') as handler:
                    ctx.tools[name]['handler']({'connection':'fixture'}, session_id='native')
                    handler.assert_called_once_with(name, {'connection':'fixture'}, session_id='native')

    def load(self, context, home, *, broker=False):
        constants = SimpleNamespace(__file__=str(home/'hermes_constants.py'), get_hermes_home=lambda: home)
        with (nullcontext() if broker else patch('pantheon_connectors.broker.package.metadata', return_value=None)), patch.dict('sys.modules', hermes_constants=constants), patch(
                'pantheon_connectors.hermes_plugin.subprocess.run',
                side_effect=[SimpleNamespace(stdout=HERMES_COMMIT), SimpleNamespace(stdout='')]):
            return register(context)

    def test_skill_and_native_tools_load_without_receiver_or_setup_write(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            ctx = Context()
            with patch('pantheon_connectors.hermes_plugin.threading.Thread') as thread:
                plugin = self.load(ctx, home)
                thread.assert_not_called()
            self.assertEqual(set(ctx.skills), {'collaborate'})
            self.assertEqual(ctx.skills['collaborate'][0], SKILL_PATH)
            self.assertNotIn('bounded trial',ctx.skills['collaborate'][1])
            self.assertIn('Disclose the prepared 60-minute default',ctx.tools['pantheon_join']['schema']['description'])
            self.assertEqual(set(ctx.tools), {t['name'] for t in [CONNECT_TOOL, *TOOLS, *HERMES_TOOLS, *PREPARE_TOOLS]})
            self.assertEqual(set(ctx.hooks), set(IDENTITY_HOOKS))
            self.assertEqual(plugin.active, {})
            self.assertEqual(list(home.iterdir()), [])
            with patch.object(plugin, 'handle', return_value='fixture') as handle:
                ctx.tools['pantheon_receive']['handler']({'connection':'fixture'}, session_id='native-from-dispatcher')
                handle.assert_called_once_with('pantheon_receive', {'connection':'fixture'}, session_id='native-from-dispatcher')
            ctx.cleanup[0]()
            self.assertTrue(plugin.stop.is_set())

    def test_mid_turn_load_binds_on_next_api_request_without_setup_or_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            ctx = Context()
            plugin = self.load(ctx, Path(temp))
            doctor = ctx.tools['pantheon_doctor']['handler']
            # The turn-start hook happened before register; tools now exist.
            pending = json.loads(doctor({}, session_id='chosen'))
            self.assertEqual(pending['status'], 'session_observation_pending')
            self.assertFalse(pending['native_binding_observed'])
            # A normal tool loop's next host request supplies identity, not args.
            ctx.hooks['pre_api_request'](session_id='chosen', platform='desktop', request={'ignored':'PRIVATE'})
            result = json.loads(doctor({}, session_id='chosen'))
            self.assertEqual(result['status'], 'setup_required')
            self.assertTrue(result['native_binding_observed'])
            self.assertEqual(plugin.observed, {'chosen':'desktop'})
            self.assertEqual(list(Path(temp).iterdir()), [])
            self.assertEqual(plugin.active, {})

    def test_whole_turn_runtime_can_observe_at_turn_end_and_next_turn(self):
        for hook in ('post_llm_call', 'pre_llm_call'):
            with self.subTest(hook=hook), tempfile.TemporaryDirectory() as temp:
                ctx = Context()
                plugin = self.load(ctx, Path(temp))
                ctx.hooks[hook](session_id='chosen', platform='desktop')
                plugin.preflight(plugin.binding('chosen'))

    def test_tool_args_other_session_and_non_native_surface_cannot_establish_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            ctx = Context()
            plugin = self.load(ctx, Path(temp))
            doctor = ctx.tools['pantheon_doctor']['handler']
            result = json.loads(doctor({'session_id':'chosen', 'platform':'desktop'}))
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(plugin.observed, {})
            ctx.hooks['pre_api_request'](session_id='other', platform='desktop')
            self.assertEqual(json.loads(doctor({}, session_id='chosen'))['status'], 'session_observation_pending')
            for platform in ('cli', '', 'telegram'):
                ctx.hooks['pre_api_request'](session_id='chosen', platform=platform)
                self.assertEqual(json.loads(doctor({}, session_id='chosen'))['status'], 'blocked')
            plugin.close()
            ctx.hooks['pre_api_request'](session_id='chosen', platform='desktop')
            self.assertEqual(plugin.observed, {})

    def test_reload_does_not_reuse_old_observations_or_retained_handlers(self):
        with tempfile.TemporaryDirectory() as temp:
            first_ctx = Context()
            first = self.load(first_ctx, Path(temp))
            first_ctx.hooks['pre_api_request'](session_id='chosen', platform='desktop')
            first.close()
            next_ctx = Context()
            replacement = self.load(next_ctx, Path(temp))
            self.assertEqual(first.doctor('chosen')['status'], 'blocked')
            self.assertEqual(replacement.doctor('chosen')['status'], 'session_observation_pending')
            next_ctx.hooks['pre_api_request'](session_id='other', platform='desktop')
            self.assertEqual(replacement.doctor('chosen')['status'], 'session_observation_pending')
            next_ctx.hooks['pre_api_request'](session_id='chosen', platform='desktop')
            replacement.preflight(replacement.binding('chosen'))

    def test_missing_skill_fails_before_partial_native_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            ctx = Context()
            with patch('pantheon_connectors.hermes_plugin.SKILL_PATH', Path(temp)/'missing.md'):
                with self.assertRaises(FileNotFoundError): self.load(ctx, Path(temp))
            self.assertEqual(ctx.tools, {})
            self.assertEqual(ctx.hooks, {})
            self.assertEqual(ctx.cleanup, [])
