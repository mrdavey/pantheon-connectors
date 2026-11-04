import copy
import json
import os
from pathlib import Path
import tempfile
import time
import tomllib
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors.broker import codex_setup_policy as setup
from pantheon_connectors.broker.permission_contract import require
from pantheon_connectors.runtime_policy import allowed
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked


class SetupPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        env = patch.dict(os.environ, CODEX_HOME=str(self.home)); env.start(); self.addCleanup(env.stop)
        self.policy = setup.Policy(self.home / 'pantheon/setup-policy.json')
        self.answer = {'action': 'accept', 'content': {'collaboration': True,
            'minutes': '0', 'transfers': False, 'sites': False}}
        self.elicit = Mock(side_effect=lambda *_: copy.deepcopy(self.answer))
        self.inspect = Mock(return_value={'reviewed_provider': 'existing', 'reviewed_account': 'metadata'})

    def review(self, thread='one', **kwargs):
        return self.policy.review(thread, self.elicit, self.inspect, **kwargs)

    def config(self, thread='one'):
        return {'binding': {'session': thread}, 'delivery': setup.DEFAULT_DAEMON, 'reviewed_provider': 'existing', 'reviewed_account': 'metadata',
                'allow_runtime_turns': True, 'trial_expires_at': 0, 'runtime_policy': {'mode': 'connection'},
                'connection_runtime_supported': True, 'runtime_started_at': time.time()}

    def grant(self, name, thread='one'):
        path = self.home / 'pantheon' / (name + '.json')
        config = self.policy.attach(path, self.config(thread), thread)
        save_private(path, config)
        return path, config

    def test_legacy_setup_root_repairs_permissions_before_native_review_save(self):
        self.policy.path.parent.mkdir(mode=0o755)
        self.policy.path.parent.chmod(0o755)
        self.review()
        self.assertEqual(self.policy.path.parent.stat().st_mode & 0o777, 0o700)

    def test_server_command_contract_remains_usable_by_qualified_092_package(self):
        # Captured from qualified main cb397dc; updating a command enum would
        # block ALL old preapproved command calls, including ordinary ready.
        fixture = Path(__file__).parent / 'fixtures/command-permission-0.9.2.json'
        old = fixture.read_text()
        current = json.loads((Path(__file__).parent / 'fixtures/tool-catalog-v1.json').read_text())
        command = next(tool for tool in current['tools'] if tool['name'] == 'pantheon_command')
        with patch('pantheon_connectors.broker.permission_contract.Path.read_text', return_value=old):
            for op in ('ready', 'report', 'withdraw', 'definition', 'result', 'confirm'):
                require({'codex_setup_policy': {}}, command, {'op': op})
            # A changed enum still fails closed; the shortcut has a new name.
            changed = copy.deepcopy(command)
            changed['inputSchema']['properties']['op']['enum'].append('ack_ready')
            with self.assertRaises(Blocked):
                require({'codex_setup_policy': {}}, changed, {'op': 'ready'})

    def test_native_review_once_then_fresh_grants_across_conversations(self):
        result = self.review()
        self.assertEqual(result['status'], 'setup_active')
        self.assertFalse(result['runtime_started'])
        self.assertTrue(result['remember'])
        self.assertNotIn('remember', self.elicit.call_args.args[1]['properties'])
        self.assertEqual(self.review('two')['status'], 'setup_active')
        self.elicit.assert_called_once(); self.inspect.assert_called_once()
        first, a = self.grant('first')
        second, b = self.grant('second', 'two')
        self.assertNotEqual(first, second)
        self.assertTrue(allowed(a)); self.assertTrue(allowed(b))
        self.assertEqual(self.policy.recall('two')['provider'], setup.provider(a))
        self.assertEqual(self.policy.path.stat().st_mode & 0o777, 0o600)
        native = tomllib.loads((self.home / 'config.toml').read_text())
        tools = native['plugins']['pantheon@pantheon-hosted']['mcp_servers']['pantheon']['tools']
        self.assertIn('pantheon_connect', tools)
        self.assertNotIn('pantheon_transfer_send', tools)
        self.assertNotIn('*', tools)

    def test_saved_setup_survives_restart_and_chat_closure_without_another_review(self):
        self.review(); path, config = self.grant('first')
        config['allow_runtime_turns'] = False
        save_private(path, config)
        original = path.read_bytes()
        self.policy = setup.Policy(self.policy.path)
        with patch.object(setup.time, 'time', return_value=time.time() + 86400):
            self.assertEqual(self.review('two')['status'], 'setup_active')
            self.assertEqual(self.policy.recall('two')['consent']['runtime_policy'], {'mode': 'connection'})
        self.elicit.assert_called_once(); self.inspect.assert_called_once()
        _, fresh = self.grant('second', 'two')
        self.assertTrue(allowed(fresh)); self.assertFalse(allowed(config))
        self.assertEqual(path.read_bytes(), original)

    def test_explainer_discloses_saved_permissions_before_native_acceptance(self):
        def answer(message, form):
            self.assertIn('without stopping for routine approvals', message)
            self.assertIn('saved for future invitations', message)
            self.assertIn('change or revoke them anytime', message)
            self.assertNotIn('remember', form['required'])
            self.inspect.assert_not_called()
            self.assertFalse(self.policy.path.exists())
            self.assertFalse((self.home / 'config.toml').exists())
            return self.answer
        self.elicit.side_effect = answer
        self.review()

    def test_decline_cancel_and_malformed_response_never_inspect_or_write(self):
        valid = copy.deepcopy(self.answer['content'])
        for response in ({'action': 'decline'}, {'action': 'cancel'}, {'approved': True},
                         {'action': 'accept', 'content': None},
                         {'action': 'accept', 'content': {}},
                         {'action': 'accept', 'content': {'collaboration': True}},
                         *({'action': 'accept', 'content': {**valid, 'remember': value}}
                           for value in (False, True)),
                         *({'action': 'accept', 'content': {**valid, 'minutes': value}}
                           for value in (True, 0, 30, '61', '0.0', '', ['30']))):
            self.answer = response
            try:
                self.assertEqual(self.review()['status'], 'setup_declined')
            except Blocked:
                pass
            self.assertFalse(self.policy.path.exists())
        self.inspect.assert_not_called(); self.assertFalse((self.home / 'config.toml').exists())

    def test_form_uses_codex_supported_scalar_fields_and_string_lifetimes(self):
        # Codex rust-v0.162.1's parse_field rejects Number and MultiSelect;
        # one unsupported field rejects the whole form, returning no content.
        for field in setup.FORM['properties'].values():
            self.assertIn(field['type'], ('boolean', 'string'))
        for value, seconds in [('0', 0), ('15', 900), ('30', 1800), ('45', 2700), ('60', 3600)]:
            answer = copy.deepcopy(self.answer)
            answer['content']['minutes'] = value
            selected = setup.choices(answer, 1000)
            self.assertEqual(selected['expires_at'], 1000 + seconds if seconds else 0)

    def test_revoke_and_change_fence_existing_grants_without_deleting_them(self):
        self.review(); path, config = self.grant('first')
        self.assertTrue(allowed(config))
        self.policy.revoke()
        self.assertFalse(allowed(config)); self.assertEqual(load_private(path), config)
        self.review(); self.assertFalse(allowed(config))
        _, fresh = self.grant('next'); self.assertTrue(allowed(fresh))
        self.review(change=True); self.assertFalse(allowed(fresh))
        self.assertTrue(path.exists())

    def test_fixed_window_applies_to_future_chats_without_renewal(self):
        self.answer['content']['minutes'] = '15'
        self.review()
        fixed = self.policy.recall('one')['consent']['expires_at']
        self.assertAlmostEqual(fixed, time.time() + 900, delta=3)
        self.assertEqual(self.policy.recall('other')['consent']['expires_at'], fixed)
        with patch.object(setup.time, 'time', return_value=fixed + 1):
            self.assertIsNone(self.policy.recall('one'))
            self.assertIsNone(self.policy.recall('other'))

    def test_previously_saved_conversation_only_choice_is_not_silently_widened(self):
        self.review()
        previous = self.policy.read(); previous['remember'] = False
        save_private(self.policy.path, previous)
        original = self.policy.path.read_bytes()
        self.policy = setup.Policy(self.policy.path)
        self.assertIsNotNone(self.policy.recall('one'))
        self.assertIsNone(self.policy.recall('other'))
        self.assertEqual(self.policy.path.read_bytes(), original)

    def test_review_deadline_caps_remembered_setup_and_is_visible_before_acceptance(self):
        deadline = time.time()+600
        self.review(expires_at=deadline)
        message, _ = self.elicit.call_args.args
        self.assertIn(time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(deadline)), message)
        self.assertEqual(self.policy.recall('other')['consent']['expires_at'], deadline)
        self.review(expires_at=deadline+60)
        self.assertEqual(self.policy.recall('other')['consent']['expires_at'], deadline)
        self.elicit.assert_called_once()

    def test_review_deadline_expiring_while_human_answers_grants_nothing(self):
        with patch.object(setup.time, 'time', side_effect=[1000, 1700, 1700]):
            with self.assertRaises(Blocked):
                self.review(expires_at=1600)
        self.inspect.assert_not_called()
        self.assertFalse(self.policy.path.exists())
        self.assertFalse((self.home/'config.toml').exists())

    def test_provider_change_and_concurrency_limit_fail_before_grant(self):
        self.review()
        changed = self.config(); changed['reviewed_account'] = 'other'
        with self.assertRaisesRegex(Blocked, 'metadata changed'):
            self.policy.attach(self.home / 'changed.json', changed, 'one')
        paths = [self.grant(str(index))[0] for index in range(3)]
        with self.assertRaisesRegex(Blocked, 'Three'):
            self.grant('fourth')
        closed = load_private(paths[0]); closed['allow_runtime_turns'] = False; save_private(paths[0], closed)
        self.grant('fourth')
        self.assertFalse(allowed(closed))

    def test_capabilities_and_folder_boundaries(self):
        folder = self.home / 'shared'; folder.mkdir()
        self.answer['content'].update(transfers=True, transfer_folder=str(folder), sites=True, site_folder=str(folder))
        self.review(change=True); _, config = self.grant('first')
        setup.require_tool(config, 'pantheon_transfer_fetch', {'path': str(folder / 'new.txt')})
        setup.require_tool(config, 'pantheon_site_deploy', {'directory': str(folder)})
        for path in (self.home / 'secret', folder / '..' / 'secret'):
            with self.assertRaises(Blocked):
                setup.require_tool(config, 'pantheon_transfer_send', {'path': str(path)})
        link = folder / 'link'; link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(Blocked):
            setup.require_tool(config, 'pantheon_transfer_send', {'path': str(link / 'secret')})
        self.policy.revoke()
        setup.require_tool(config, 'pantheon_command', {'op': 'withdraw'})
        setup.require_tool(config, 'pantheon_finish_wake', {})
        with self.assertRaises(Blocked):
            setup.require_tool(config, 'pantheon_post', {})

    def test_default_features_create_private_folders_only_after_acceptance(self):
        def accept(message, form):
            self.assertNotIn('transfer_folder', form['properties'])
            self.assertNotIn('site_folder', form['properties'])
            self.assertIn(str(self.home / 'pantheon/files'), message)
            self.assertFalse((self.home / 'pantheon/files').exists())
            return {'action': 'accept', 'content': {k: v['default'] for k, v in form['properties'].items()}}
        self.elicit.side_effect = accept
        result = self.review()
        self.assertTrue(result['transfers']); self.assertTrue(result['sites'])
        for name in ('files', 'sites'):
            folder = self.home / 'pantheon' / name
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        _, config = self.grant('defaults')
        setup.require_tool(config, 'pantheon_transfer_fetch', {'path': str(self.home / 'pantheon/files/new')})
        with self.assertRaises(Blocked):
            setup.require_tool(config, 'pantheon_site_deploy', {'directory': str(self.home)})

    def test_cancel_does_not_create_feature_folders_and_symlink_default_is_rejected(self):
        self.elicit.side_effect = None; self.elicit.return_value = {'action': 'cancel'}
        self.review()
        self.assertFalse((self.home / 'pantheon/files').exists())
        (self.home / 'pantheon').mkdir()
        (self.home / 'pantheon/files').symlink_to(self.home, target_is_directory=True)
        self.elicit.return_value = {'action': 'accept', 'content': {'collaboration': True,
            'minutes': '0', 'transfers': True, 'sites': True}}
        with self.assertRaises(Blocked):
            self.review()
        self.assertFalse((self.home / 'config.toml').exists())

    def test_invalid_custom_folder_is_not_replaced_by_a_default(self):
        self.answer['content'].update(transfers=True, transfer_folder=0)
        with self.assertRaises(Blocked):
            self.review(change=True)
        self.assertFalse((self.home / 'pantheon/files').exists())

    def test_change_preserves_saved_opt_out_and_prefills_folders(self):
        self.review()
        self.review(change=True)
        form = self.elicit.call_args.args[1]
        self.assertFalse(form['properties']['transfers']['default'])
        self.assertFalse(form['properties']['sites']['default'])
        self.assertEqual(form['properties']['site_folder']['default'], str(self.home / 'pantheon/sites'))

    def test_unselected_capability_and_existing_native_denial_stay_blocked(self):
        (self.home / 'config.toml').write_text('[plugins."pantheon@pantheon-hosted".mcp_servers.pantheon.tools.pantheon_post]\napproval_mode="reject"\n')
        result = self.review(); _, config = self.grant('one')
        self.assertTrue(result['native_policy']['retained'])
        with self.assertRaises(Blocked):
            setup.require_tool(config, 'pantheon_transfer_send', {'path': str(self.home / 'file')})
        self.assertIn('reject', (self.home / 'config.toml').read_text())

    def test_missing_replaced_or_foreign_policy_fails_closed(self):
        self.review(); _, config = self.grant('one')
        changed = copy.deepcopy(config); changed['codex_setup_policy']['path'] = str(self.home / 'other.json')
        self.assertFalse(allowed(changed))
        self.policy.path.unlink(); self.assertFalse(allowed(config))


class PermissionContractTests(unittest.TestCase):
    def setUp(self):
        self.tools = json.loads((Path(__file__).parents[1] / 'pantheon_connectors/broker/permissions.json').read_text())
        self.config = {'codex_setup_policy': {'id': 'fixture'}}

    def test_description_and_unused_optional_fields_do_not_change_existing_authority(self):
        descriptor = copy.deepcopy(self.tools['pantheon_post'])
        descriptor['description'] = 'New description'
        descriptor['inputSchema']['properties']['connection']['description'] = 'New explanation'
        descriptor['inputSchema']['properties']['optional_new'] = {'type': 'string'}
        require(self.config, descriptor, {'connection': 'fixture'})
        with self.assertRaises(Blocked):
            require(self.config, descriptor, {'connection': 'fixture', 'optional_new': 'unreviewed'})

    def test_changed_route_authority_mutation_and_command_ops_require_review(self):
        for key, value in [('operation', 'new-operation'), ('authority', 'admin'), ('mutating', False), ('contract', '2')]:
            descriptor = copy.deepcopy(self.tools['pantheon_post']); descriptor[key] = value
            with self.assertRaises(Blocked):
                require(self.config, descriptor, {})
        descriptor = copy.deepcopy(self.tools['pantheon_command'])
        descriptor['inputSchema']['properties']['op']['enum'].append('new-operation')
        with self.assertRaises(Blocked):
            require(self.config, descriptor, {'op': 'new-operation'})
