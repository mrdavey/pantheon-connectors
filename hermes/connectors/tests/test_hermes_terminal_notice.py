"""Synthetic terminal-notice boundaries; not actual-agent handback evidence."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from pantheon_connectors.hermes_plugin import TerminalScopeEnded
from pantheon_connectors.state import Journal, load_private, save_private
from pantheon_connectors.worker import Blocked
import test_hermes_connection_runtime as runtime


class TerminalNotice(unittest.TestCase):
    setUp = runtime.ConnectionRuntime.setUp
    approved = runtime.ConnectionRuntime.approved
    call = runtime.ConnectionRuntime.call

    def close(self, finite=False):
        self.approved()
        if finite:
            now = time.time()
            for key in ('runtime_policy', 'runtime_started_at', 'hermes_runtime_grant'):
                self.config.pop(key, None)
            self.config.update(hermes_finite_runtime_supported=True,
                               hermes_trial_duration_seconds=600,
                               trial_started_at=now - 590, trial_expires_at=now + 10)
            self.config['hermes_finite_runtime_grant'] = dict(
                binding=self.binding, connection=self.connection, url=self.config['url'],
                duration_seconds=600, trial_started_at=now - 590, trial_expires_at=now + 10,
                timezone='UTC', notification_policy=self.config['notification_policy'])
            save_private(self.setup, self.config)
            member_path = self.root/'state'/(self.connection+'.json')
            member = load_private(member_path)
            member.pop('runtime_policy'); member.pop('runtime_started_at')
            member['trial_expires_at'] = now + 10
            save_private(member_path, member)
        self.client.url = self.config['url']
        self.client.credential = 'PRIVATE-LOCAL-CREDENTIAL'
        self.view['chat'].update(state='closed', reason='completed', result={'text': 'PRIVATE RESULT'})
        with self.assertRaises(TerminalScopeEnded):
            self.plugin.tick(self.connection, self.binding)
        self.client.reset_mock()

    def notify(self):
        return self.plugin.terminal_notice(self.connection, self.binding)

    def test_idle_closure_schedules_one_content_free_notice_after_fencing_work(self):
        self.close()
        before = self.setup.read_bytes()
        self.assertEqual(self.notify(), 'terminal_notice_scheduled_not_received')
        text = self.ctx.inject_message.call_args.args[0]
        self.assertIn('pantheon_status', text)
        self.assertIn(self.connection, text)
        self.assertNotIn('PRIVATE RESULT', text)
        self.assertEqual(self.ctx.inject_message.call_args.kwargs, {'session_key': 'chosen'})
        self.assertEqual(self.setup.read_bytes(), before)
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
        self.client.command.assert_not_called()
        self.assertTrue(all(c.args == ('/v1/chat',) for c in self.client.request.call_args_list))
        self.assertEqual(self.notify(), 'terminal_notice_already_attempted')
        self.ctx.inject_message.assert_called_once()
        self.assertEqual(self.call('pantheon_status')['status'], 'terminal_cleanup')
        self.assertEqual(self.call('pantheon_post', text='new work', key='no')['status'], 'blocked')

    def test_receiver_handles_closure_and_stops_after_the_terminal_notice(self):
        self.approved()
        self.view['chat']['state'] = 'closed'
        self.plugin.run(self.connection, self.binding)
        self.assertEqual(self.plugin.outcomes[self.connection], 'terminal_notice_scheduled_not_received')
        self.ctx.inject_message.assert_called_once()
        self.assertEqual(self.plugin.active, {})

    def test_foreground_closure_and_expiry_watcher_keep_the_original_terminal_bound(self):
        self.close(finite=True)
        before = load_private(self.setup)
        self.plugin.expire(before)
        self.assertEqual(load_private(self.setup), before)
        self.assertLessEqual(before['hermes_terminal_notice_until'],
                             before['hermes_finite_runtime_grant']['trial_expires_at'])
        self.assertEqual(self.plugin.tick(self.connection, self.binding), 'terminal_scope_ended')
        self.assertEqual(self.notify(), 'terminal_notice_scheduled_not_received')

    def test_no_notice_after_original_deadline_or_delayed_resume(self):
        for finite in (False, True):
            with self.subTest(finite=finite):
                self.close(finite=finite)
                until = load_private(self.setup)['hermes_terminal_notice_until']
                with patch('time.time', return_value=until):
                    self.assertEqual(self.notify(), 'terminal_notice_unavailable')
                self.ctx.inject_message.assert_not_called()
                self.client.request.assert_not_called()

    def test_no_replay_after_uncertain_enqueue(self):
        self.close()
        self.ctx.inject_message.side_effect = TimeoutError('uncertain host outcome')
        with self.assertRaises(Blocked):
            self.notify()
        self.ctx.inject_message.side_effect = None
        self.assertEqual(self.notify(), 'terminal_notice_already_attempted')
        self.ctx.inject_message.assert_called_once()
        journal = Journal(self.root/'state'/(self.connection+'.sqlite'), self.binding)
        try:
            self.assertEqual(json.loads(journal.control('hermes-terminal-notice'))['outcome'], 'outcome_unknown')
            self.assertEqual(journal.control('notice-count'), '1')
        finally:
            journal.db.close()

    def test_rejected_host_enqueue_is_not_retried(self):
        self.close()
        self.ctx.inject_message.return_value = False
        self.assertEqual(self.notify(), 'terminal_notice_native_host_unavailable')
        self.assertEqual(self.notify(), 'terminal_notice_already_attempted')
        self.ctx.inject_message.assert_called_once()

    def test_closure_racing_pending_read_uses_terminal_notice_not_work_notice(self):
        self.approved()
        def request(route):
            if route == '/v1/chat': return copy.deepcopy(self.view)
            self.assertEqual(route, '/v1/pending')
            self.view['chat']['state'] = 'closed'
            return [self.envelope]
        self.client.request.side_effect = request
        self.plugin.run(self.connection, self.binding)
        self.ctx.inject_message.assert_called_once()
        self.assertTrue(self.ctx.inject_message.call_args.args[0].startswith('Pantheon has closed'))
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])

    def test_revocation_changed_binding_and_reopened_room_cannot_send_final_notice(self):
        self.close()
        original = copy.deepcopy(self.view)
        for change in ('revoked', 'binding', 'active', 'role'):
            with self.subTest(change=change):
                self.view = copy.deepcopy(original)
                if change == 'revoked': self.view['chat']['members']['a']['revoked'] = True
                elif change == 'binding': self.view['chat']['members']['a']['binding'] = {}
                elif change == 'active': self.view['chat']['state'] = 'active'
                else: self.view['role'] = 'admin'
                with self.assertRaises(Blocked): self.notify()
                self.ctx.inject_message.assert_not_called()

    def test_unload_stop_provider_change_and_old_terminal_scopes_do_not_inject(self):
        self.close()
        self.plugin.receiver_stops[self.connection] = threading.Event()
        self.plugin.receiver_stops[self.connection].set()
        self.assertEqual(self.notify(), 'receiver_stopped')
        self.plugin.receiver_stops[self.connection].clear()
        with patch.object(self.plugin, 'check_usage', side_effect=Blocked('changed provider')):
            with self.assertRaises(Blocked): self.notify()
        saved = load_private(self.setup); saved.pop('hermes_terminal_notice_until')
        save_private(self.setup, saved)
        self.assertEqual(self.notify(), 'terminal_notice_unavailable')
        self.plugin.stop.set()
        self.assertEqual(self.notify(), 'receiver_stopped')
        self.ctx.inject_message.assert_not_called()

    def test_final_notice_respects_original_notice_cap(self):
        self.approved()
        self.config['notification_policy'] = {'mode': 'limited', 'limit': 1}
        from pantheon_connectors import hermes_limits
        self.config['hermes_runtime_grant'] = hermes_limits.connection_grant(self.config)
        save_private(self.setup, self.config)
        self.view['chat']['state'] = 'closed'
        with self.assertRaises(TerminalScopeEnded): self.plugin.tick(self.connection, self.binding)
        journal = Journal(self.root/'state'/(self.connection+'.sqlite'), self.binding)
        try:
            self.plugin.check_runtime_grant(journal, self.config)
            self.plugin.check_notice_policy(journal, self.config, 0)
            journal.control('notice-count', '1')
        finally: journal.db.close()
        self.assertEqual(self.notify(), 'notice_budget_exhausted')
        self.ctx.inject_message.assert_not_called()

    def test_owner_stop_or_expiry_during_authority_read_prevents_enqueue(self):
        self.close()
        before = load_private(self.setup)
        def request(route):
            save_private(self.setup, before | {'hermes_terminal_notice_until': 0})
            return copy.deepcopy(self.view)
        self.client.request.side_effect = request
        self.assertEqual(self.notify(), 'terminal_notice_unavailable')
        self.ctx.inject_message.assert_not_called()
