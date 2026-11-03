import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors.broker.hermes_live import NativeBridge, PREFIX, PROOF, request
from pantheon_connectors.worker import Blocked


class LiveNativeIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.plugin = Mock()
        self.plugin.binding.side_effect = lambda session: {'session': session} if session in ('one', 'two') else (_ for _ in ()).throw(Blocked('No native session'))
        self.plugin.catalogue.tools.return_value = [{'name': 'pantheon_post'}]
        self.plugin.handle.return_value = json.dumps({'status': 'posted'})
        self.bridge = NativeBridge(self.plugin, self.home)
        self.addCleanup(self.bridge.close)

    def proof(self, args=None, session='one'):
        args = {'text': 'synthetic'} if args is None else args
        result = self.bridge.attest(tool_name=PREFIX+'pantheon_post', args=args,
                                   session_id=session, tool_call_id='native-call')
        return {**args, **result['args']}

    def test_native_hook_pins_exact_session_and_replay_is_blocked(self):
        args = self.proof({'text': 'synthetic', PROOF: 'model-invented'}, 'two')
        result = request(self.home, self.bridge.identity, 'call', name='pantheon_post', args=args)
        self.assertEqual(result['status'], 'posted')
        self.plugin.handle.assert_called_once_with('pantheon_post', {'text': 'synthetic'}, session_id='two')
        result = request(self.home, self.bridge.identity, 'call', name='pantheon_post', args=args)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.plugin.handle.call_count, 1)

    def test_changed_args_tool_and_expiry_consume_proof_without_dispatch(self):
        for change in ('args', 'name', 'expiry'):
            with self.subTest(change=change):
                args = self.proof()
                values = dict(secret=self.bridge.secret, method='call', name='pantheon_post', args=args)
                if change == 'args':
                    values['args'] = {**args, 'text': 'changed'}
                elif change == 'name':
                    values['name'] = 'pantheon_join'
                else:
                    for record in self.bridge.pending.values():
                        record['end'] = 0
                with self.assertRaises(Blocked):
                    self.bridge.dispatch(values)
                self.assertEqual(self.bridge.pending, {})
        self.plugin.handle.assert_not_called()

    def test_no_hook_identity_no_host_permission_no_guessed_session(self):
        result = self.bridge.attest(tool_name=PREFIX+'pantheon_post', args={}, session_id='guessed', tool_call_id='native')
        self.assertEqual(result['action'], 'block')
        result = request(self.home, self.bridge.identity, 'call', name='pantheon_post', args={'session_id': 'one'})
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(self.bridge.attest(tool_name='unrelated', args={}))
        self.plugin.handle.assert_not_called()
        self.plugin.catalogue.refresh.assert_not_called()

    def test_catalogue_refreshes_only_after_native_identity_check(self):
        self.proof()
        self.plugin.catalogue.refresh.assert_called_once_with()
        self.plugin.catalogue.tools.return_value = []
        result = self.bridge.attest(tool_name=PREFIX+'pantheon_post', args={}, session_id='one', tool_call_id='native')
        self.assertEqual(result['action'], 'block')
        self.plugin.handle.assert_not_called()

    def test_unload_fences_outstanding_proof_and_removes_private_endpoint(self):
        args = self.proof()
        self.bridge.close()
        with self.assertRaises(Blocked):
            self.bridge.dispatch(dict(secret=self.bridge.secret, method='call', name='pantheon_post', args=args))
        self.assertFalse(self.bridge.record.exists())
        self.assertFalse(self.bridge.temporary.exists())
        self.plugin.handle.assert_not_called()

    def test_foreign_profile_cannot_route_valid_proof(self):
        with tempfile.TemporaryDirectory() as other, self.assertRaises(FileNotFoundError):
            request(Path(other).resolve(), self.bridge.identity, 'call', name='pantheon_post', args=self.proof())

    def test_native_callback_exception_is_not_replayed(self):
        self.plugin.handle.side_effect = RuntimeError('uncertain mutation')
        args = self.proof()
        first = request(self.home, self.bridge.identity, 'call', name='pantheon_post', args=args)
        second = request(self.home, self.bridge.identity, 'call', name='pantheon_post', args=args)
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(second['status'], 'blocked')
        self.plugin.handle.assert_called_once()


class PortableLoadedVersion(unittest.TestCase):
    def test_doctor_distinguishes_loaded_hook_and_portable_process(self):
        import io
        from pantheon_connectors.broker.hermes_live import serve
        from pantheon_connectors.version import HERMES_BROKER_VERSION
        source = io.StringIO(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'pantheon_doctor','arguments':{}}})+'\n')
        output = io.StringIO()
        with patch('pantheon_connectors.broker.hermes_live.request', return_value={'loaded_connector_version':'old-hook'}), patch('pantheon_connectors.broker.adapter.ToolSnapshot.watch'), patch('pantheon_connectors.broker.adapter.ToolSnapshot.close'):
            serve(Path('/unused-native-home'), source, output)
        result = json.loads(output.getvalue())['result']['structuredContent']
        self.assertEqual(result['loaded_connector_version'], 'old-hook')
        self.assertEqual(result['loaded_portable_version'], HERMES_BROKER_VERSION)
