import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pantheon_connectors.broker.adapter import ToolSnapshot
from pantheon_connectors.broker.catalog import Catalog, validate
from pantheon_connectors.broker.client import BrokerClient, CONFIG
from pantheon_connectors.broker.transport import strict_json
from pantheon_connectors.client import APIError
from pantheon_connectors.onboarding import TOOLS, Onboarding, dispatch
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked


def catalogue():
    value = json.loads((Path(__file__).parent / 'fixtures/tool-catalog-v1.json').read_text())
    value['revision'] = 'sha256:' + 'a' * 64
    return value


class Transport:
    def __init__(self):
        self.catalog = catalogue()
        self.exchanges = []
        self.calls = []
        self.fail = None
        self.view_version = 1

    def request(self, path, **kwargs):
        if path == '/v1/tool-catalog':
            return copy.deepcopy(self.catalog), '"catalog"'
        if path == '/v1/mcp/token':
            self.exchanges.append(copy.deepcopy(kwargs))
            if self.fail == 'token':
                raise APIError(0)
            return {'connection': 'b' * 32, 'resource': 'https://pantheon.talk/mcp',
                    'expires_at': kwargs['payload']['expires_at']}, ''
        raise AssertionError(path)

    def rpc(self, method, params, token):
        self.calls.append(copy.deepcopy((method, params, token)))
        if self.fail == 'tool':
            raise APIError(0)
        if params['name'] == 'pantheon_chat':
            return {'structuredContent': {'chat': {'epoch': self.view_version, 'config': self.view_version}}}
        return {'structuredContent': {'status': 'ok', 'id': 'message-1'}}


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / ('a' * 32 + '.json')
        self.transport = Transport()
        self.clock = 1_800_000_000
        self.allowed = True
        self.config = {'url': 'https://pantheon.talk', 'credential': 'parent-secret',
                       'binding': {'runtime': 'codex', 'session': 'original', 'profile': 'default'},
                       'mcp': CONFIG, 'trial_expires_at': self.clock + 300, 'redeemed': True,
                       'state_dir': str(self.root)}
        self.catalog = Catalog(self.config['url'], http=self.transport, clock=lambda: self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def allowance(self):
        if not self.allowed:
            raise Blocked('Consent ended')

    def client(self):
        return BrokerClient(self.path, self.config, self.allowance,
                            http=self.transport, catalog=self.catalog, clock=lambda: self.clock)

    def test_catalog_accepts_current_contract(self):
        self.assertEqual(len(validate(catalogue())['tools']), 21)

    def test_ack_ready_uses_scoped_remote_command_without_legacy_fallback(self):
        client = self.client()
        with patch('pantheon_connectors.client.Client.request') as legacy:
            client.command('ack_ready', key='read-probe', text='probe', ids=['probe-id'],
                           objective_version=1, context_version=1)
            legacy.assert_not_called()
        self.assertEqual([call[1]['name'] for call in self.transport.calls], ['pantheon_chat', 'pantheon_ack_ready'])
        call = self.transport.calls[-1][1]
        self.assertEqual(call['name'], 'pantheon_ack_ready')
        self.assertNotIn('op', call['arguments'])
        self.assertEqual(call['arguments']['message_ids'], ['probe-id'])

    def test_unsupported_handback_never_falls_back_to_legacy_http(self):
        client = self.client()
        with patch('pantheon_connectors.client.Client.request') as legacy:
            with self.assertRaises(Blocked):
                client.command('handback', key='invented-receipt', text='delivered',
                               evidence='A final response is not a verified receipt')
            with self.assertRaises(Blocked):
                client.tool('pantheon_command', dict(connection=client.local_connection,
                    op='handback', key='direct', text='delivered'), wire=True)
            legacy.assert_not_called()
        self.assertEqual(self.transport.exchanges, [])
        self.assertEqual(self.transport.calls, [])

    def test_catalog_rejects_local_expansion_reserved_and_external_reference(self):
        for mutate in (
            lambda tool: tool.update(name='pantheon_authorize'),
            lambda tool: tool.update(primitive='execute.shell.v1', execution='local'),
            lambda tool: tool['inputSchema'].update({'$ref': 'https://evil.test/schema'}),
            lambda tool: tool.update(authority='creator'),
        ):
            value = catalogue()
            mutate(value['tools'][0])
            with self.assertRaises(Blocked):
                validate(value)
        value = catalogue()
        local = next(t for t in value['tools'] if t['name'] == 'pantheon_transfer_send')
        local['inputSchema']['properties']['shell'] = {'type': 'string'}
        with self.assertRaises(Blocked):
            validate(value)

    def test_catalog_unknown_contract_omitted(self):
        value = catalogue()
        value['tools'][0]['contract'] = '999'
        self.assertEqual(len(validate(value)['tools']), len(value['tools']) - 1)

    def test_refresh_is_atomic_on_invalid_snapshot_and_revision_reuse(self):
        self.catalog.refresh()
        before = self.catalog.tools()
        self.transport.catalog['tools'][0]['name'] = 'pantheon_authorize'
        with self.assertRaises(Blocked):
            self.catalog.refresh(force=True)
        self.assertEqual(before, self.catalog.tools())
        self.transport.catalog = catalogue()
        self.transport.catalog['tools'][0]['description'] = 'Changed'
        with self.assertRaises(Blocked):
            self.catalog.refresh(force=True)

    def test_metadata_cache_restores_without_credential(self):
        catalog = Catalog(self.config['url'], http=self.transport, cache_dir=self.root)
        catalog.refresh()
        restored = Catalog(self.config['url'], http=self.transport, cache_dir=self.root)
        self.assertEqual(restored.tools(), catalog.tools())
        self.assertEqual(restored.status, 'cached_unverified')
        self.assertNotIn(self.config['credential'], restored.cache.read_text())
        self.assertEqual(restored.cache.stat().st_mode & 0o777, 0o600)

    def test_strict_json(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            with self.assertRaises(Blocked):
                strict_json(raw)

    def test_token_is_resource_scoped_and_does_not_expose_parent(self):
        result = self.client().request('/v1/chat')
        self.assertEqual(result['chat']['epoch'], 1)
        self.assertEqual(len(self.transport.exchanges), 1)
        method, params, token = self.transport.calls[-1]
        self.assertEqual(params['arguments']['connection'], 'b' * 32)
        self.assertNotIn(self.config['credential'], token)
        self.assertEqual(self.path.with_suffix('.mcp-token.json').stat().st_mode & 0o777, 0o600)
        self.client().request('/v1/chat')
        self.assertEqual(len(self.transport.exchanges), 1)

    def test_exchange_uncertainty_preserves_exact_secret_and_deadline(self):
        self.transport.fail = 'token'
        with self.assertRaises(APIError):
            self.client().request('/v1/chat')
        first = self.transport.exchanges[-1]
        self.clock += 1
        self.transport.fail = None
        self.client().request('/v1/chat')
        self.assertEqual(first, self.transport.exchanges[-1])

    def test_refresh_does_not_extend_finite_native_grant(self):
        self.client().request('/v1/chat')
        self.clock += 275
        self.client().request('/v1/chat')
        old, new = [item['payload'] for item in self.transport.exchanges]
        self.assertEqual(old['expires_at'], new['expires_at'])
        self.assertEqual(new['previous'], old['secret'])

    def test_expired_uncommitted_exchange_recovers_only_after_server_proof(self):
        self.config['trial_expires_at'] = 0
        self.transport.fail = 'token'
        with self.assertRaises(APIError):
            self.client().request('/v1/chat')
        original = self.transport.exchanges[-1]['payload']
        self.clock += 301
        self.transport.fail = None
        request = self.transport.request
        def prove(path, **kwargs):
            if path == '/v1/mcp/token' and kwargs['payload'] == original:
                raise APIError(409, 'mcp_exchange_expired_uncommitted')
            return request(path, **kwargs)
        with patch.object(self.transport, 'request', side_effect=prove):
            self.client().request('/v1/chat')
        self.assertNotEqual(original['secret'], self.transport.exchanges[-1]['payload']['secret'])
        self.assertNotIn('pending', load_private(self.path.with_suffix('.mcp-token.json')))

    def test_expired_uncertain_exchange_never_discards_intent_on_generic_conflict(self):
        self.transport.fail = 'token'
        with self.assertRaises(APIError):
            self.client().request('/v1/chat')
        path = self.path.with_suffix('.mcp-token.json')
        before = load_private(path)
        self.clock += 301
        with patch.object(self.transport, 'request', side_effect=APIError(409)):
            with self.assertRaises(APIError):
                self.client().request('/v1/chat')
        self.assertEqual(before, load_private(path))

    def test_snapshot_reads_revision_and_tools_together(self):
        snapshot = ToolSnapshot(self.config['url'], TOOLS, catalog=self.catalog)
        with patch.object(self.catalog, 'tools', side_effect=AssertionError('non-atomic read')):
            self.assertTrue(snapshot.refresh())
        self.assertEqual(snapshot.revision, self.transport.catalog['revision'])
        self.assertEqual(len(snapshot.tools()), 21)

    def test_consent_revocation_blocks_saved_token(self):
        self.client().request('/v1/chat')
        self.allowed = False
        with self.assertRaises(Blocked):
            self.client().request('/v1/chat')
        self.assertEqual(len(self.transport.calls), 1)

    def test_binding_change_cannot_reuse_token(self):
        self.client().request('/v1/chat')
        self.config['binding']['session'] = 'different'
        with self.assertRaises(Blocked):
            self.client().request('/v1/chat')

    def test_changed_command_retry_keeps_preconditions(self):
        client = self.client()
        self.transport.fail = 'tool'
        # Allow the initial read and fail the post only.
        rpc = self.transport.rpc
        def fail_post(method, params, token):
            self.transport.fail = 'tool' if params['name'] == 'pantheon_post' else None
            return rpc(method, params, token)
        self.transport.rpc = fail_post
        with self.assertRaises(APIError):
            client.command('post', text='hello', key='same-key')
        first = self.transport.calls[-1][1]
        self.transport.rpc = rpc
        self.transport.fail = None
        self.transport.view_version = 99
        self.client().command('post', text='hello', key='same-key')
        self.assertEqual(first, self.transport.calls[-1][1])
        self.assertEqual(len([c for c in self.transport.calls if c[1]['name'] == 'pantheon_chat']), 1)
        with self.assertRaises(Blocked):
            client.command('post', text='changed', key='same-key')

    def test_confirmed_retry_rechecks_server(self):
        self.client().command('post', text='hello', key='same')
        calls = len(self.transport.calls)
        self.client().command('post', text='hello', key='same')
        self.assertEqual(len(self.transport.calls), calls + 1)

    def test_unknown_tool_requires_catalogue_and_scoped_selector(self):
        for name, connection in [('pantheon_execute', self.path.stem), ('pantheon_chat', 'c' * 32)]:
            with self.assertRaises(Blocked):
                self.client().tool(name, {'connection': connection})
        self.assertFalse(self.transport.exchanges)

    def test_server_tool_addition_needs_no_adapter_code(self):
        tool = copy.deepcopy(next(t for t in self.transport.catalog['tools'] if t['name'] == 'pantheon_chat'))
        tool.update(name='pantheon_new_server_tool', operation='new.operation')
        self.transport.catalog['tools'].append(tool)
        adapter = ToolSnapshot(self.config['url'], TOOLS, catalog=self.catalog)
        self.assertTrue(adapter.refresh())
        self.assertIn(tool['name'], {t['name'] for t in adapter.tools()})
        self.client().tool(tool['name'], {'connection': self.path.stem})
        self.assertEqual(self.transport.calls[-1][1]['name'], tool['name'])

    def test_codex_advertises_refresh_only_in_broker_package(self):
        from pantheon_connectors.codex_hosted import HostedServer
        snapshot = ToolSnapshot(self.config['url'], TOOLS, catalog=self.catalog)
        with patch('pantheon_connectors.codex_hosted.descriptor', return_value={'broker_contract': 'pantheon.broker.v1'}), \
                patch('pantheon_connectors.broker.adapter.ToolSnapshot', return_value=snapshot):
            server = HostedServer(self.root)
            self.addCleanup(server.close)
            self.assertTrue(server.handle('initialize', {})['capabilities']['tools']['listChanged'])
            names = [tool['name'] for tool in server.handle('tools/list', {})['tools']]
            self.assertEqual(len(names), len(set(names)))
            self.assertIn('pantheon_chat', names)
            self.assertFalse(server.broker_active())
            self.assertEqual(server.children, {})
            self.assertEqual(list(self.root.iterdir()), [])

    def test_hermes_snapshot_preserves_native_identity_and_unique_names(self):
        from test_hermes_registration import Context, RegistrationTests
        snapshot = ToolSnapshot(self.config['url'], TOOLS, catalog=self.catalog)
        context = Context()
        with patch('pantheon_connectors.broker.package.metadata', return_value={'contract': 'pantheon.broker.v1'}), \
                patch('pantheon_connectors.broker.adapter.ToolSnapshot', return_value=snapshot):
            plugin = RegistrationTests().load(context, self.root, broker=True)
            self.addCleanup(plugin.close)
            self.addCleanup(snapshot.close)
            self.assertNotIn('pantheon_chat', context.tools)
            self.assertIsNone(snapshot.revision)
            self.assertIn('message_id', context.tools['pantheon_receipts']['schema']['parameters']['properties'])
            self.assertFalse(plugin.broker_active())
            with patch.object(plugin, 'handle', return_value='fixture') as handle:
                context.tools['pantheon_doctor']['handler']({'connection': 'selected'}, session_id='host-owned')
                handle.assert_called_once_with('pantheon_doctor', {'connection': 'selected'}, session_id='host-owned')
            snapshot.refresh()
            from pantheon_connectors.broker.hermes_live import definitions
            live = [tool['name'] for tool in definitions(snapshot)]
            self.assertIn('pantheon_chat', live)
            self.assertEqual(len(live), len(set(live)))

    def test_member_requires_exact_native_binding_before_broker(self):
        save_private(self.path, self.config)
        service = Onboarding(self.config)
        service.allowance_check = lambda: True
        with self.assertRaises(Blocked):
            service.member(self.path.stem, {**self.config['binding'], 'session': 'wrong'})
        with patch('pantheon_connectors.broker.client.for_origin', return_value=self.catalog):
            _, _, client = service.member(self.path.stem, self.config['binding'])
        self.assertIsInstance(client, BrokerClient)
        _, _, recovery = service.member(self.path.stem, self.config['binding'], allow_expired=True)
        self.assertNotIsInstance(recovery, BrokerClient)

    def test_oversized_command_never_writes_intent_or_sends_credentials(self):
        with self.assertRaises(Blocked):
            self.client().command('post', text='x' * (64 * 1024), key='large')
        self.assertFalse(self.path.with_suffix('.mcp-operations').exists())
        self.assertFalse(self.transport.exchanges)

    def test_dynamic_dispatch_cannot_cross_legacy_connection(self):
        config = {**self.config}
        config.pop('mcp')
        save_private(self.path, config)
        service = Onboarding(config)
        service.allowance_check = lambda: True
        with self.assertRaises(Blocked):
            dispatch(service, 'pantheon_chat', {'connection': self.path.stem}, config['binding'], lambda _: None, lambda: {})


if __name__ == '__main__':
    unittest.main()
