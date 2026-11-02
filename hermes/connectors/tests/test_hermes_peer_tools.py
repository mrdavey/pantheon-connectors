"""Synthetic peers through native tool handlers; no actual host/model acceptance."""
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors.client import Client
from pantheon_connectors.environment import get as product_env
from pantheon_connectors.hermes_plugin import NativePlugin, HERMES_COMMIT
from pantheon_connectors.onboarding import Onboarding
from pantheon_connectors.state import Journal, JournalBusy, save_private


class PeerTools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = dict(url='http://127.0.0.1:8080', state_dir=str(self.root/'state'),
                           allow_runtime_turns=True, trial_expires_at=time.time()+300)
        self.setup = self.root/'setup.json'; save_private(self.setup, self.config)
        self.ctx = Mock(profile_name='fixture'); self.ctx.inject_message.return_value = True
        self.client = Mock()
        self.plugin = NativePlugin(self.ctx, self.setup, 'fixture', HERMES_COMMIT,
            service_factory=lambda config: Onboarding(config, client_factory=lambda *_: self.client), incoming_probe=lambda _: True)
        self.plugin.observe(session_id='chosen', platform='desktop')
        self.addCleanup(self.plugin.close)
        self.binding = self.plugin.binding('chosen'); self.connection = 'a'*32
        save_private(self.root/'state'/(self.connection+'.json'), dict(url=self.config['url'],
            binding=self.binding, credential='PRIVATE-LOCAL-CREDENTIAL', redeemed=True))
        self.view = dict(role='agent', member='a', chat=dict(state='active', members={
            'a':dict(binding=self.binding, revoked=False, ready=1),
            'b':dict(binding={}, revoked=False, ready=1)}))
        self.client.request.return_value = self.view

    def call(self, name, session='chosen', **args):
        return json.loads(self.plugin.handle(name, dict(connection=self.connection, **args), session_id=session))

    def test_receive_contending_with_receiver_can_resume_without_new_authority(self):
        before = self.setup.read_bytes()
        journal = Journal(self.root/'state'/(self.connection+'.sqlite'), self.binding)
        try:
            with journal.exclusive():
                result = self.call('pantheon_receive')
            self.assertEqual(result['status'], 'blocked')
            self.assertTrue(result['retry_same_receive'])
            self.client.request.assert_not_called()
            self.client.command.assert_not_called()
            self.ctx.inject_message.assert_not_called()
            self.client.request.return_value = []
            self.assertEqual(self.call('pantheon_receive')['messages'], [])
            self.assertEqual(self.setup.read_bytes(), before)
        finally:
            journal.db.close()

    def test_journal_contention_does_not_authorize_mutation_replay(self):
        with patch('pantheon_connectors.hermes_plugin.dispatch', side_effect=JournalBusy('busy')) as dispatch:
            result = self.call('pantheon_command', op='report', target='working', key='original')
        self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('retry_same_receive', result)
        self.assertNotIn('retry_same_join', result)
        dispatch.assert_called_once()
        self.client.command.assert_not_called()

    def test_inspection_reads_only_and_encodes_untrusted_path_arguments(self):
        result = self.call('pantheon_status')
        self.assertEqual(result['untrusted_member_content'], self.view)
        self.assertFalse(result['receiver']['running'])
        self.call('pantheon_history', cursor='opaque?x=/&y=1')
        self.assertEqual(self.client.request.call_args.args, ('/v1/history?cursor=opaque%3Fx%3D%2F%26y%3D1',))
        self.call('pantheon_receipts', message_id='id/../../commands?x=1')
        self.assertEqual(self.client.request.call_args.args, ('/v1/messages/id%2F..%2F..%2Fcommands%3Fx%3D1/receipts',))
        self.assertTrue(all(len(call.args) == 1 for call in self.client.request.call_args_list))
        self.client.command.assert_not_called(); self.ctx.inject_message.assert_not_called()

    def test_other_session_bad_args_changed_binding_revocation_and_expiry_fail_closed(self):
        for name in ('pantheon_status', 'pantheon_history', 'pantheon_receipts', 'pantheon_resume'):
            with self.subTest(name=name):
                args = dict(message_id='m') if name == 'pantheon_receipts' else {}
                self.assertEqual(self.call(name, session='other', **args)['status'], 'blocked')
                self.assertEqual(self.call(name, extra='peer-instruction', **args)['status'], 'blocked')
        self.client.request.assert_not_called()
        for member in (dict(binding=self.binding, revoked=True),
                       dict(binding=self.binding | dict(session='other'), revoked=False)):
            self.view['chat']['members']['a'] = member
            self.assertEqual(self.call('pantheon_resume')['status'], 'blocked')
        self.assertEqual(self.plugin.active, {})
        save_private(self.setup, self.config | dict(allow_runtime_turns=False, trial_expires_at=0))
        self.client.reset_mock()
        self.assertEqual(self.call('pantheon_resume')['status'], 'blocked')
        self.client.request.assert_not_called()

    def test_host_permission_off_blocks_readiness_but_preserves_reads_and_withdrawal(self):
        self.plugin.incoming_probe = lambda _: False
        result = self.call('pantheon_command', op='ready', text='probe', key='ready')
        self.assertEqual(result['host_incoming_permission'], 'missing_or_unverified')
        self.client.command.assert_not_called()
        self.assertEqual(self.call('pantheon_status')['untrusted_member_content'], self.view)
        self.client.command.return_value = {}
        self.assertEqual(self.call('pantheon_command', op='withdraw', key='cleanup'), {})
        self.client.command.assert_called_once_with('withdraw', key='cleanup')

    def test_resume_runs_once_without_redeeming_and_withdraw_wakes_sleeping_receiver(self):
        ticked = threading.Event()
        def tick(*_): ticked.set(); return 'idle_or_notice_unresolved'
        with patch.object(self.plugin, 'tick', side_effect=tick):
            self.assertEqual(self.call('pantheon_resume')['status'], 'receiver_started')
            self.assertTrue(ticked.wait(1))
            thread = self.plugin.active[self.connection]
            self.assertEqual(self.call('pantheon_resume')['status'], 'receiver_already_present')
            self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['/v1/chat', '/v1/chat'])
            self.client.command.side_effect = TimeoutError('lost withdrawal response')
            self.assertEqual(self.call('pantheon_command', op='withdraw', key='same-key')['status'], 'blocked')
            thread.join(1); self.assertFalse(thread.is_alive())
            self.assertTrue(self.plugin.receiver_state(self.connection)['stop_requested'])
            self.client.command.assert_called_once_with('withdraw', key='same-key')
        self.ctx.inject_message.assert_not_called()

    def test_withdraw_during_pending_read_fences_final_enqueue(self):
        entered, release = threading.Event(), threading.Event()
        envelope = dict(protocol='pyros.v1', binding=self.binding,
                        delivery=dict(id='d', state='pending'))
        def request(route):
            if route == '/v1/pending':
                entered.set(); self.assertTrue(release.wait(2)); return [envelope]
            return self.view
        self.client.request.side_effect = request
        results = []
        thread = threading.Thread(target=lambda: results.append(self.plugin.tick(self.connection, self.binding)))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.client.command.return_value = {}
            self.assertEqual(self.call('pantheon_command', op='withdraw', key='withdraw'), {})
        finally:
            release.set(); thread.join(2)
        self.assertEqual(results, ['receiver_stopped'])
        self.ctx.inject_message.assert_not_called()

    def test_reload_resume_preserves_uncertain_notice_and_budget(self):
        save_private(self.setup, self.config | dict(hermes_notice_limit=16))
        journal = Journal(self.root/'state'/(self.connection+'.sqlite'), self.binding)
        journal.control('hermes-notice', json.dumps(dict(token='opaque', deliveries=['d'], outcome='outcome_unknown')))
        journal.control('notice-count', '8')
        journal.db.close()
        self.assertEqual(self.call('pantheon_resume')['status'], 'receiver_started')
        deadline = time.monotonic()+1
        while self.plugin.outcomes.get(self.connection) != 'idle_or_notice_unresolved' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.plugin.outcomes[self.connection], 'idle_or_notice_unresolved')
        thread = self.plugin.active[self.connection]
        self.plugin.close(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.ctx.inject_message.assert_not_called()
        self.assertTrue(all(c.args[0] == '/v1/chat' for c in self.client.request.call_args_list))
        journal = Journal(self.root/'state'/(self.connection+'.sqlite'), self.binding)
        self.assertEqual(journal.control('notice-count'), '8')
        self.assertEqual(json.loads(journal.control('hermes-notice'))['outcome'], 'outcome_unknown')
        journal.db.close()


@unittest.skipUnless(product_env('HTTP_TEST_URL'), 'requires disposable loopback service')
class PeerExchangeHTTP(unittest.TestCase):
    def test_three_native_bindings_exchange_both_directions_and_fan_out(self):
        self.exchange(1)

    def test_sustained_exchange_survives_reload_and_exceeds_legacy_budget(self):
        self.exchange(12)

    def test_unlimited_real_receive_ack_and_duplicate_post_beyond_old_maximum(self):
        self.exchange(66, unlimited=True)

    def exchange(self, rounds, unlimited=False):
        url = product_env('HTTP_TEST_URL'); secret = secrets.token_urlsafe(32)
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        admin = Client(url, secret)
        admin.request('/v1/chats', dict(secret=secret, key=secrets.token_urlsafe(24),
            title='Synthetic three-peer native tools', purpose='No live agent evidence', hours=4,
            slots=[dict(id=slot, runtime='hermes', required=True) for slot in ('a','b','c')]))
        self.addCleanup(lambda: admin.command('end', key='fixture-end'))
        with tempfile.TemporaryDirectory() as tmp:
            peers = {}
            for slot in ('a','b','c'):
                token = secrets.token_urlsafe(32); admin.command('invite', target=slot, secret=token, hours=1)
                root = Path(tmp).resolve()/slot
                config = dict(url=url, state_dir=str(root/'state'), allow_runtime_turns=True, trial_expires_at=time.time()+1800,
                              trial_started_at=time.time(), hermes_trial_duration_seconds=1800, hermes_notice_limit=32)
                if unlimited:
                    config.pop("hermes_notice_limit")
                    config["notification_policy"] = {"mode":"unlimited"}
                setup = root/'setup.json'; save_private(setup, config)
                ctx = Mock(profile_name=slot); ctx.inject_message.return_value = True
                plugin = NativePlugin(ctx, setup, 'fixture', HERMES_COMMIT, incoming_probe=lambda _: True)
                plugin.observe(session_id=slot, platform='desktop'); self.addCleanup(plugin.close)
                binding = plugin.binding(slot)
                connection = Onboarding(config).join(url+'/join#invite='+token, binding,
                    deliberate=True, preflight=plugin.preflight)['connection']
                peers[slot] = (plugin, ctx, connection)

            def tool(slot, name, **args):
                plugin, _, connection = peers[slot]
                result = json.loads(plugin.handle(name, dict(connection=connection, **args), session_id=slot))
                self.assertNotEqual(result.get('status'), 'blocked', result)
                return result

            def consume(slot, token=None):
                result = tool(slot, 'pantheon_receive', **({'notice_token':token} if token else {}))
                messages = [row['untrusted_member_content']['message'] for row in result['messages']]
                for message in messages:
                    tool(slot, 'pantheon_ack', message_ids=[message['id']], key='ack-'+message['id'])
                    if message['kind'] == 'probe':
                        tool(slot, 'pantheon_command', op='ready', text=message['text'], key='ready-'+message['id'])
                return messages

            for slot in ('a','b','c','a','b','c'): consume(slot)
            status = tool('a', 'pantheon_status')['untrusted_member_content']
            self.assertEqual(status['chat']['state'], 'active')
            self.assertTrue(all(m['ready'] == 1 for m in status['chat']['members'].values()))
            sequence = [('a', ('b','c')), ('b', ('a','c'))] * rounds
            for index, (sender, recipients) in enumerate(sequence):
                if index == 12:
                    old, ctx, connection = peers['c']
                    old.close()
                    replacement = NativePlugin(ctx, old.setup_file, 'fixture', HERMES_COMMIT, incoming_probe=lambda _: True)
                    replacement.observe(session_id='c', platform='desktop')
                    peers['c'] = (replacement, ctx, connection)
                    self.addCleanup(replacement.close)
                    self.assertEqual(tool('c', 'pantheon_status')['limits']['notices_used'], 12)
                text = 'SYNTHETIC AUTHORIZED '+str(index)
                key = 'post-'+str(index)
                sent = tool(sender, 'pantheon_post', text=text, key=key)
                self.assertEqual(tool(sender, 'pantheon_post', text=text, key=key), sent)
                plugin, _, connection = peers[sender]
                self.assertEqual(plugin.tick(connection, plugin.binding(sender)), 'idle_or_notice_unresolved')
                for recipient in recipients:
                    plugin, ctx, connection = peers[recipient]
                    self.assertEqual(plugin.tick(connection, plugin.binding(recipient)), 'scheduled_not_received')
                    notice = ctx.inject_message.call_args.args[0]
                    self.assertNotIn(text, notice)
                    self.assertEqual(ctx.inject_message.call_args.kwargs['session_key'], recipient)
                    history = tool(recipient, 'pantheon_history')['untrusted_member_content']['messages']
                    self.assertIn(sent['id'], [m['id'] for m in history])
                    evidence = tool(sender, 'pantheon_receipts', message_id=sent['id'])['untrusted_member_content'][recipient]
                    self.assertFalse(evidence['received']); self.assertFalse(evidence['ack'])
                    result = tool(recipient, 'pantheon_receive', notice_token=notice.split(' notice_token: ')[1])
                    self.assertIn(sent['id'], [r['untrusted_member_content']['message']['id'] for r in result['messages']])
                    evidence = tool(sender, 'pantheon_receipts', message_id=sent['id'])['untrusted_member_content'][recipient]
                    self.assertTrue(evidence['received']); self.assertFalse(evidence['ack'])
                    tool(recipient, 'pantheon_ack', message_ids=[sent['id']], key='ack-'+sent['id'])
                    self.assertTrue(tool(sender, 'pantheon_receipts', message_id=sent['id'])['untrusted_member_content'][recipient]['ack'])
                    self.assertEqual(plugin.tick(connection, plugin.binding(recipient)), 'idle_or_notice_unresolved')
                history = tool(sender, 'pantheon_history')['untrusted_member_content']['messages']
                self.assertEqual(sum(m['id'] == sent['id'] for m in history), 1)
            self.assertEqual(tool('c', 'pantheon_status')['limits']['notices_used'], 2*rounds)
            self.assertEqual(tool('c', 'pantheon_status')['limits']['notices_remaining'], None if unlimited else 32-2*rounds)
            tool('c', 'pantheon_command', op='withdraw', key='withdraw-c')
            self.assertTrue(tool('a', 'pantheon_status')['untrusted_member_content']['chat']['members']['c']['revoked'])
            plugin, _, connection = peers['c']
            resumed = json.loads(plugin.handle('pantheon_resume', dict(connection=connection), session_id='c'))
            self.assertEqual(resumed['status'], 'blocked')
