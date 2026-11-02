"""Explicit room-lifetime grants with synthetic server/native fixtures only."""
import copy
import json
import unittest
from unittest.mock import patch
from pantheon_connectors import hermes_limits
from pantheon_connectors.state import Journal, load_private, save_private
from pantheon_connectors.worker import Blocked
import test_hermes_peer_tools as peers


class ConnectionRuntime(unittest.TestCase):
    setUp = peers.PeerTools.setUp
    call = peers.PeerTools.call

    def approved(self):
        self.config.update(url='https://pantheon.talk',hosted_consent=True,
            prepared_binding=self.binding,prepared_connection=self.connection,
            connection_runtime_supported=True,runtime_policy={'mode':'connection'},
            runtime_started_at=1000,trial_started_at=0,trial_expires_at=0,
            notification_policy={'mode':'unlimited'})
        self.config['hermes_runtime_grant']=hermes_limits.connection_grant(self.config)
        save_private(self.setup,self.config)
        member=load_private(self.root/'state'/(self.connection+'.json'))
        member.update(url=self.config['url'],runtime_policy=self.config['runtime_policy'],
            runtime_started_at=1000,connection_runtime_supported=True)
        save_private(self.root/'state'/(self.connection+'.json'),member)
        self.envelope=dict(protocol='pyros.v1',binding=self.binding,
            delivery=dict(id='delivery',state='pending'),message=dict(id='message'))
        def request(route):
            if route=='/v1/chat':return copy.deepcopy(self.view)
            if route=='/v1/pending':return [copy.deepcopy(self.envelope)]
            raise AssertionError(route)
        self.client.request.side_effect=request

    def test_receiver_can_run_beyond_one_hour_but_disconnect_does_not_reactivate(self):
        self.approved()
        with patch('time.time',return_value=10000):
            self.assertEqual(self.plugin.tick(self.connection,self.binding),'scheduled_not_received')
            self.assertEqual(self.client.request.call_args_list[0].args,('/v1/chat',))
            self.assertEqual(self.client.request.call_args_list[-1].args,('/v1/chat',))
            self.client.command.return_value={}
            self.assertEqual(self.call('pantheon_command',op='withdraw',key='disconnect'),{})
            self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
            self.client.reset_mock()
            self.assertEqual(self.call('pantheon_resume')['status'],'blocked')
            self.client.request.assert_not_called()
        self.assertEqual(self.ctx.inject_message.call_count,1)
        self.assertEqual(load_private(self.setup)['runtime_started_at'],1000)

    def test_closed_expired_and_revoked_rooms_stop_work_before_any_notification(self):
        for change in ('closed','expired','revoked','binding'):
            with self.subTest(change=change):
                self.approved();self.client.reset_mock();self.ctx.inject_message.reset_mock()
                self.view['chat']['state']='active'
                self.view['chat']['members']['a']=dict(binding=self.binding,revoked=False)
                if change in ('closed','expired'):
                    self.view['chat'].update(state='closed',reason='timed_out' if change=='expired' else 'ended')
                elif change=='revoked':self.view['chat']['members']['a']['revoked']=True
                else:self.view['chat']['members']['a']['binding']=self.binding | dict(session='other')
                with self.assertRaises(Blocked):self.plugin.tick(self.connection,self.binding)
                self.ctx.inject_message.assert_not_called()
                self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
                self.assertEqual(self.client.request.call_count,1)

    def test_room_closure_during_pending_read_is_rechecked_before_enqueue(self):
        self.approved()
        def request(route):
            if route=='/v1/chat':return copy.deepcopy(self.view)
            if route=='/v1/pending':
                self.view['chat']['state']='closed'
                return [self.envelope]
            raise AssertionError(route)
        self.client.request.side_effect=request
        with self.assertRaises(Blocked):self.plugin.tick(self.connection,self.binding)
        self.ctx.inject_message.assert_not_called()
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])

    def test_foreground_work_and_changed_membership_scope_fail_closed(self):
        self.approved();self.view['chat']['state']='closed'
        self.assertEqual(self.call('pantheon_post',text='never send',key='post')['status'],'blocked')
        self.client.command.assert_not_called()
        self.approved();self.view['chat']['state']='active';self.client.reset_mock()
        member_path=self.root/'state'/(self.connection+'.json')
        saved=load_private(member_path);saved.pop('runtime_policy');save_private(member_path,saved)
        with self.assertRaises(Blocked):self.plugin.tick(self.connection,self.binding)
        self.client.request.assert_not_called();self.ctx.inject_message.assert_not_called()

    def test_journal_pins_original_runtime_start_and_does_not_reset_counts(self):
        self.approved()
        self.assertEqual(self.plugin.tick(self.connection,self.binding),'scheduled_not_received')
        updated=load_private(self.setup);updated['runtime_started_at']=1001
        updated['hermes_runtime_grant']=hermes_limits.connection_grant(updated)
        save_private(self.setup,updated)
        member_path=self.root/'state'/(self.connection+'.json')
        member=load_private(member_path);member['runtime_started_at']=1001;save_private(member_path,member)
        with self.assertRaises(Blocked):self.plugin.tick(self.connection,self.binding)
        self.assertEqual(self.ctx.inject_message.call_count,1)
        journal=Journal(self.root/'state'/(self.connection+'.sqlite'),self.binding)
        try:
            self.assertEqual(journal.control('notice-count'),'1')
            self.assertEqual(json.loads(journal.control('hermes-runtime-grant'))['runtime_started_at'],1000)
        finally:journal.db.close()

    def test_connection_policy_cannot_be_added_to_legacy_finite_config(self):
        save_private(self.setup,self.config | dict(runtime_policy={'mode':'connection'},
            runtime_started_at=1000,connection_runtime_supported=True,trial_expires_at=0))
        self.assertFalse(hermes_limits.trial_allowed(load_private(self.setup)))
        self.assertEqual(self.call('pantheon_post',text='never send',key='post')['status'],'blocked')
        self.client.command.assert_not_called();self.ctx.inject_message.assert_not_called()
