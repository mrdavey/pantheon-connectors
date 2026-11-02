"""Bounded endurance settings, synthetic clocks and no user runtime."""
import json
import time
import unittest
from unittest.mock import patch
from pantheon_connectors.hermes_limits import settings, trial_allowed, describe
from pantheon_connectors.state import Journal, save_private, load_private
from pantheon_connectors.worker import Blocked
import test_hermes_peer_tools as peers


class Limits(unittest.TestCase):
    def test_defaults_and_strict_finite_bounds(self):
        self.assertEqual(settings({}), (600,8))
        self.assertEqual(settings(dict(hermes_trial_duration_seconds=3600, hermes_notice_limit=128)), (3600,128))
        for key, upper in [('hermes_trial_duration_seconds',3600),('hermes_notice_limit',128)]:
            for value in (None,0,-1,upper+1,True,1.5,'32',float('inf'),float('nan')):
                with self.subTest(key=key,value=value),self.assertRaises(Blocked): settings({key:value})

    def test_hosted_window_has_no_duration_proposal_but_remains_anchored_and_finite(self):
        config=dict(hosted_consent=True,notification_policy={'mode':'unlimited'},
                    allow_runtime_turns=False,trial_started_at=0,trial_expires_at=0)
        self.assertEqual(settings(config),(None,None))
        with patch('time.time',return_value=1000):
            self.assertIsNone(describe(config)['duration_seconds'])
            self.assertFalse(trial_allowed(config))
            approved=config | dict(allow_runtime_turns=True,trial_started_at=1000,trial_expires_at=3700)
            self.assertTrue(trial_allowed(approved))  # human-chosen 45 minutes, no ten-minute cutoff
            for fields in (dict(trial_started_at=0),dict(trial_started_at=1001),
                           dict(trial_expires_at=4601),dict(trial_expires_at=float('inf')),
                           dict(allow_runtime_turns=False),dict(hermes_trial_duration_seconds=600)):
                self.assertFalse(trial_allowed(approved | fields))
            narrowed=approved | dict(hermes_trial_duration_seconds=600,trial_expires_at=1600)
            self.assertTrue(trial_allowed(narrowed))
        with patch('time.time',return_value=3700):
            self.assertFalse(trial_allowed(approved))
            self.assertFalse(trial_allowed(approved | dict(trial_expires_at=4700)))

    def test_anchored_duration_cannot_slide_on_reload(self):
        config=dict(allow_runtime_turns=True,trial_started_at=1000,trial_expires_at=2800,
                    hermes_trial_duration_seconds=1800,hermes_notice_limit=32)
        for now,allowed in [(1000,True),(2000,True),(2799,True),(2800,False),(3000,False)]:
            with patch('time.time',return_value=now):
                self.assertEqual(trial_allowed(dict(config)),allowed)
                self.assertEqual(describe(config,12)['notices_remaining'],20)
        with patch('time.time',return_value=2000):
            for fields in (dict(trial_expires_at=2801),dict(trial_started_at=2001),
                           dict(trial_started_at=True),dict(trial_started_at=float('nan'))):
                self.assertFalse(trial_allowed(config | fields))
            legacy=dict(allow_runtime_turns=True,trial_expires_at=2500)
            self.assertTrue(trial_allowed(legacy))
            self.assertFalse(describe(legacy)['duration_anchored'])
            self.assertFalse(trial_allowed(legacy | dict(hermes_trial_duration_seconds=600)))


class RuntimeLimits(unittest.TestCase):
    setUp = peers.PeerTools.setUp
    call = peers.PeerTools.call

    def test_remaining_budget_and_expired_status_are_read_only(self):
        journal=Journal(self.root/'state'/(self.connection+'.sqlite'),self.binding)
        journal.control('notice-count','8');journal.db.close()
        config=self.config | dict(hermes_notice_limit=12,hermes_trial_duration_seconds=1800,
                                 trial_started_at=time.time()-10,trial_expires_at=time.time()+1700)
        save_private(self.setup,config)
        result=self.call('pantheon_status')
        self.assertEqual(result['limits']['notices_remaining'],4)
        self.assertGreater(result['limits']['remaining_seconds'],1600)
        save_private(self.setup,config | dict(allow_runtime_turns=False,trial_expires_at=0))
        before = self.setup.read_bytes()
        self.client.reset_mock()
        result=self.call('pantheon_status')
        self.assertEqual(result['status'],'runtime_not_authorized')
        self.assertEqual(result['limits']['notices_used'],8)
        self.assertFalse(result['limits']['runtime_authorized'])
        self.client.request.assert_not_called()
        self.assertFalse(load_private(self.setup)['allow_runtime_turns'])
        self.assertEqual(self.setup.read_bytes(), before)


    def test_exhausted_resume_cannot_reset_budget(self):
        journal=Journal(self.root/'state'/(self.connection+'.sqlite'),self.binding)
        journal.control('notice-count','8');journal.db.close()
        for _ in range(2):
            result=self.call('pantheon_resume')
            self.assertEqual(result['status'],'notice_budget_exhausted')
            self.assertEqual(result['limits']['notices_remaining'],0)
        self.assertEqual(self.plugin.active,{})
        self.ctx.inject_message.assert_not_called()


    def test_lowered_budget_during_poll_is_rechecked_before_enqueue(self):
        journal=Journal(self.root/'state'/(self.connection+'.sqlite'),self.binding)
        journal.control('notice-count','7');journal.db.close()
        def request(route):
            self.assertEqual(route,'/v1/pending')
            save_private(self.setup,self.config | dict(hermes_notice_limit=7))
            return [dict(protocol='pyros.v1',binding=self.binding,delivery=dict(id='d',state='pending'))]
        self.client.request.side_effect=request
        self.assertEqual(self.plugin.tick(self.connection,self.binding),'notice_budget_exhausted')
        self.ctx.inject_message.assert_not_called()


    def test_invalid_limit_cannot_enable_runtime_but_withdrawal_remains_available(self):
        save_private(self.setup,self.config | dict(hermes_notice_limit=129))
        self.assertEqual(self.call('pantheon_post',text='never send',key='post')['status'],'blocked')
        self.client.command.assert_not_called()
        self.client.command.return_value={}
        self.assertEqual(self.call('pantheon_command',op='withdraw',key='cleanup'),{})
        self.client.command.assert_called_once_with('withdraw',key='cleanup')
