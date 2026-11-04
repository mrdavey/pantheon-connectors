"""Closed-room receipt races; no real host, provider, or cloud acceptance."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from pantheon_connectors.state import Journal, load_private, save_private
from pantheon_connectors.worker import Blocked
import test_hermes_connection_runtime as runtime


class TerminalCleanup(unittest.TestCase):
    setUp = runtime.ConnectionRuntime.setUp
    call = runtime.ConnectionRuntime.call
    approved = runtime.ConnectionRuntime.approved
    def closed(self):
        self.approved()
        self.client.url = self.config['url']
        self.client.credential = 'PRIVATE-LOCAL-CREDENTIAL'
        self.view.update(result_agreed=True, result_final=True)
        self.view['chat'].update(state='closed', reason='completed',
            result={'text': 'The agreed picnic checklist.'}, epoch=1, config=1)
        with self.assertRaises(Blocked):
            self.plugin.tick(self.connection, self.binding)
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])

    def received(self, message='review', reported=True):
        journal = Journal(self.root/'state'/(self.connection+'.sqlite'), self.binding)
        try:
            journal.pin_scope(self.config['url'], hashlib.sha256(b'PRIVATE-LOCAL-CREDENTIAL').hexdigest())
            journal.put('delivery-'+message, 'received', 'native-tool:'+message,
                        message=message, reported=reported)
        finally:
            journal.db.close()

    def test_closed_receiver_does_not_prevent_explicit_ack_of_fetched_batch(self):
        self.closed()
        self.received()
        before = self.setup.read_bytes()
        self.client.command.return_value = {}
        result = self.call('pantheon_ack', message_ids=['review'], key='same-ack')
        self.assertEqual(result, {})
        self.client.command.assert_called_once()
        self.assertEqual(self.client.command.call_args.args, ('ack',))
        self.assertEqual(self.client.command.call_args.kwargs['ids'], ['review'])
        self.assertEqual(self.setup.read_bytes(), before)
        self.ctx.inject_message.assert_not_called()

    def test_status_returns_retained_final_result_without_history_or_runtime(self):
        self.closed()
        result = self.call('pantheon_status')
        self.assertEqual(result['status'], 'terminal_cleanup')
        self.assertFalse(result['runtime_enabled'])
        self.assertTrue(result['untrusted_member_content']['result_agreed'])
        self.assertEqual(result['untrusted_member_content']['chat']['result']['text'],
                         'The agreed picnic checklist.')
        self.assertNotIn('members', result['untrusted_member_content']['chat'])
        self.client.command.assert_not_called()
        self.ctx.inject_message.assert_not_called()

    def test_ack_rechecks_closure_if_it_races_foreground_live_check(self):
        self.approved()
        self.received()
        self.client.url = self.config['url']; self.client.credential = 'PRIVATE-LOCAL-CREDENTIAL'
        self.view['chat']['state'] = 'closed'
        self.client.command.return_value = {}
        self.assertEqual(self.call('pantheon_ack', message_ids=['review'], key='race'), {})
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
        self.ctx.inject_message.assert_not_called()

    def test_terminal_path_rejects_unreceived_or_unreported_ids_and_other_operations(self):
        self.closed()
        self.received('unreported', reported=False)
        for ids in ([], ['never-fetched'], ['unreported'], ['x']*21, ['same', 'same']):
            self.assertEqual(self.call('pantheon_ack', message_ids=ids, key='bad')['status'], 'blocked')
        for name, args in (
            ('pantheon_post', {'text': 'new work', 'key': 'post'}),
            ('pantheon_receive', {}), ('pantheon_resume', {}),
            ('pantheon_history', {}),
        ):
            self.assertEqual(self.call(name, **args)['status'], 'blocked')
        self.client.command.assert_not_called()
        self.ctx.inject_message.assert_not_called()

    def test_disabled_or_expired_active_scope_cannot_gain_terminal_access(self):
        self.approved()
        save_private(self.setup, self.config | {'allow_runtime_turns': False})
        self.client.reset_mock()
        self.assertEqual(self.call('pantheon_status')['status'], 'runtime_not_authorized')
        self.assertEqual(self.call('pantheon_ack', message_ids=['review'], key='no')['status'], 'blocked')
        self.client.request.assert_not_called()

    def test_terminal_recovery_keeps_native_and_server_authority_checks(self):
        self.closed(); self.received()
        original = copy.deepcopy(self.view)
        for change in ('active', 'revoked', 'binding', 'role'):
            self.view = copy.deepcopy(original)
            if change == 'active': self.view['chat']['state'] = 'active'
            elif change == 'role': self.view['role'] = 'admin'
            elif change == 'revoked': self.view['chat']['members']['a']['revoked'] = True
            else: self.view['chat']['members']['a']['binding'] = self.binding | {'session': 'other'}
            for name, args in (('pantheon_status', {}), ('pantheon_ack', {'message_ids': ['review'], 'key': 'no'})):
                self.assertEqual(self.call(name, **args)['status'], 'blocked')
        self.view = original
        self.assertEqual(self.call('pantheon_status', session='other')['status'], 'blocked')
        with patch.object(self.plugin, 'preflight', side_effect=Blocked('native access denied')):
            self.assertEqual(self.call('pantheon_status')['status'], 'blocked')
        self.client.command.assert_not_called()

    def test_uncertain_ack_retries_exact_saved_payload_without_renewal(self):
        self.closed(); self.received()
        payload = {'op': 'ack', 'ids': ['review'], 'key': 'uncertain', 'epoch': 1, 'config': 1}
        def lost(op, **kwargs):
            kwargs['before_send'](payload)
            raise TimeoutError('reply lost')
        self.client.command.side_effect = lost
        self.assertEqual(self.call('pantheon_ack', message_ids=['review'], key='uncertain')['status'], 'blocked')
        requests = []
        def request(route, body=None):
            requests.append((route, body))
            return copy.deepcopy(self.view) if route == '/v1/chat' else {}
        self.client.request.side_effect = request
        self.assertEqual(self.call('pantheon_ack', message_ids=['review'], key='uncertain'), {})
        self.assertIn(('/v1/commands', payload), requests)
        self.assertEqual(self.client.command.call_count, 1)
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
        self.ctx.inject_message.assert_not_called()

    def test_unloaded_instance_cannot_use_terminal_recovery(self):
        self.closed(); self.received()
        self.plugin.stop.set()
        self.client.reset_mock()
        self.assertEqual(self.call('pantheon_status')['status'], 'blocked')
        self.assertEqual(self.call('pantheon_ack', message_ids=['review'], key='no')['status'], 'blocked')
        self.client.request.assert_not_called()
        self.client.command.assert_not_called()

    def test_ack_started_before_closure_retains_broker_wire_and_rechecks_scope(self):
        self.closed(); self.received()
        key='started-before-closure'
        directory=self.root/'state'/(self.connection+'.mcp-operations')
        directory.mkdir(mode=0o700)
        operation=directory/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        arguments=dict(connection=self.connection,message_ids=['review'],key=key)
        saved={'intent':{'scope':{'origin':self.client.url,'binding':self.binding,
                                 'parent':hashlib.sha256(self.client.credential.encode()).hexdigest()},
                         'tool':'pantheon_ack','contract':'1','arguments':arguments},
               'wire':dict(arguments,epoch=9,config=7),'status':'pending'}
        save_private(operation,saved)
        self.client.request.side_effect=lambda route,body=None: copy.deepcopy(self.view) if route=='/v1/chat' else {}
        self.assertEqual(self.call('pantheon_ack',message_ids=['review'],key=key),{})
        self.client.request.assert_any_call('/v1/commands',dict(op='ack',ids=['review'],key=key,epoch=9,config=7))
        self.client.command.assert_not_called()
        self.assertEqual(load_private(operation),saved)

    def test_terminal_ack_never_reuses_another_broker_operation_key(self):
        self.closed(); self.received()
        directory=self.root/'state'/(self.connection+'.mcp-operations')
        directory.mkdir(mode=0o700)
        operation=directory/(hashlib.sha256(b'collision').hexdigest()+'.json')
        save_private(operation,{'intent':{'tool':'pantheon_post'},'wire':{},'status':'pending'})
        self.assertEqual(self.call('pantheon_ack',message_ids=['review'],key='collision')['status'],'blocked')
        self.client.command.assert_not_called()
