import json
import time
import unittest
from unittest.mock import patch
from pantheon_connectors import notification_policy as policy
from pantheon_connectors.state import save_private, load_private, Journal
from pantheon_connectors.worker import Blocked
import test_codex_output as output
import test_hermes_peer_tools as peers
import test_codex_hosted as hosted


class PolicyTests(unittest.TestCase):
    def test_explicit_mode_and_legacy_bounds(self):
        for runtime, field, maximum in [('codex','max_notices',8),('hermes','hermes_notice_limit',128)]:
            for value in (None,{}, {'mode':'unlimited','limit':0}, {'mode':'limited','limit':True}, {'mode':'limited','limit':maximum+1}):
                with self.subTest(runtime=runtime,value=value),self.assertRaises(Blocked):
                    policy.policy({'notification_policy':value},runtime)
            with self.assertRaises(Blocked):policy.policy({'notification_policy':{'mode':'unlimited'},field:maximum},runtime)
            config={'notification_policy':{'mode':'unlimited'}}
            self.assertFalse(policy.exhausted(config,runtime,1000000))
            self.assertIsNone(policy.describe(config,runtime,1000000)['notices_remaining'])
            self.assertTrue(policy.exhausted({field:maximum},runtime,maximum))


class UnlimitedCodex(output.DefaultDaemonOutputTests):
    def setUp(self):
        super().setUp()
        self.config.pop('max_notices')
        self.config['notification_policy']={'mode':'unlimited'}
        self.bridge=self.restart()

    def test_many_completed_wakes_keep_history_and_no_replay(self):
        for i in range(140):
            self.pending=[dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':str(i)})]
            result=self.restart().tick(output.CONNECTION)
            self.lifecycle(result['wake_id'])
            self.pending=[]
            self.lifecycle(result['wake_id'],finish=True)
        self.assertEqual(len(self.calls),140)
        self.assertEqual(len(self.restart().read()['notices']),140)
        self.assertEqual(self.restart().tick(output.CONNECTION)['status'],'idle')

    def test_policy_change_during_preflight_fails_before_send(self):
        self.on_check=lambda:self.config.update(notification_policy={'mode':'limited','limit':8})
        with self.assertRaises(Blocked):self.bridge.tick(output.CONNECTION)
        self.assertEqual(self.calls,[])

    def test_mode_cannot_change_existing_journal(self):
        self.bridge.tick(output.CONNECTION)
        self.config['notification_policy']={'mode':'limited','limit':8}
        with self.assertRaises(Blocked):self.restart().read()
        self.assertEqual(len(self.calls),1)


class LegacyCodex(output.OutputTests):
    def test_quota_still_enforced_and_legacy_journal_compatible(self):
        for i in range(3):
            self.pending=[dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':str(i)})]
            result=self.restart().tick(output.CONNECTION)
            self.lifecycle(result['wake_id']);self.pending=[];self.lifecycle(result['wake_id'],finish=True)
        data=self.bridge.read()
        data['scope'].pop('notification_policy');data['scope']['max_notices']=3
        save_private(self.bridge.path,data)
        self.assertEqual(len(self.restart().read()['notices']),3)
        self.pending=[dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':'new'})]
        with self.assertRaises(Blocked):self.restart().tick(output.CONNECTION)
        self.config.pop('max_notices');self.config['notification_policy']={'mode':'unlimited'}
        with self.assertRaises(Blocked):self.restart().read()
        self.assertEqual(len(self.calls),3)


class UnlimitedHermes(unittest.TestCase):
    setUp=peers.PeerTools.setUp
    call=peers.PeerTools.call

    def config_unlimited(self):
        value=self.config | {'notification_policy':{'mode':'unlimited'}}
        save_private(self.setup,value)
        return value

    def test_stop_expiry_revocation_disable_and_policy_change(self):
        value=self.config_unlimited()
        self.plugin.stop.set()
        self.assertEqual(self.plugin.tick(self.connection,self.binding),'plugin_unloaded')
        self.plugin.stop.clear()
        save_private(self.setup,value | {'trial_expires_at':0})
        self.assertEqual(self.plugin.tick(self.connection,self.binding),'trial_expired')
        save_private(self.setup,value | {'allow_runtime_turns':False})
        self.assertEqual(self.plugin.tick(self.connection,self.binding),'trial_expired')
        save_private(self.setup,value)
        self.client.request.side_effect=Blocked('revoked')
        with self.assertRaises(Blocked):self.plugin.tick(self.connection,self.binding)
        self.ctx.inject_message.assert_not_called()

    def test_journal_policy_and_legacy_counts_cannot_be_reset(self):
        value=self.config_unlimited()
        path=self.root/'state'/(self.connection+'.sqlite')
        journal=Journal(path,self.binding)
        with journal.exclusive():
            self.plugin.check_notice_policy(journal,value,0)
            journal.control('notice-count','140')
            self.plugin.check_notice_policy(journal,value,140)
            with self.assertRaises(Blocked):self.plugin.check_notice_policy(journal,self.config,140)
        journal.db.close()
        limits=self.call('pantheon_status')['limits']
        self.assertEqual(limits['notices_used'],140);self.assertIsNone(limits['notices_remaining'])
        journal=Journal(path,self.binding)
        journal.db.execute("DELETE FROM controls WHERE id='notification-policy'");journal.db.commit()
        with journal.exclusive(),self.assertRaises(Blocked):self.plugin.check_notice_policy(journal,value,140)
        journal.db.close()

    def test_policy_change_during_pending_read_never_injects(self):
        value=self.config_unlimited()
        def request(route):
            save_private(self.setup,self.config)
            return [dict(protocol='pyros.v1',binding=self.binding,delivery=dict(id='d',state='pending'))]
        self.client.request.side_effect=request
        with self.assertRaises(Blocked):self.plugin.tick(self.connection,self.binding)
        self.ctx.inject_message.assert_not_called()


class UnlimitedHosted(hosted.HostedCodexTests):
    def test_normal_production_authorize_needs_no_quota_argument(self):
        self.prepare_scope()
        result=self.call('pantheon_authorize',dict(connection=hosted.CONNECTION,deliberate=True,expires_at=time.time()+600))
        self.assertEqual(result['notification_policy'],{'mode':'unlimited'})
        self.assertFalse(result['receiver_started'])


    def test_unlimited_authorization_is_explicit_nonrenewable_and_inert(self):
        self.prepare_scope()
        args=dict(connection=hosted.CONNECTION,deliberate=True,expires_at=time.time()+600,notification_policy={'mode':'unlimited'})
        result=self.call('pantheon_authorize',args)
        self.assertFalse(result['receiver_started'])
        path=self.server.location(hosted.THREAD,hosted.CONNECTION)
        config=load_private(path)
        self.assertNotIn('max_notices',config)
        self.assertEqual(config['notification_policy'],{'mode':'unlimited'})
        for change in ({'expires_at':args['expires_at']+1},{'notification_policy':{'mode':'limited','limit':8}},{'max_notices':8}):
            with self.assertRaises(Blocked):self.call('pantheon_authorize',args | change)
        config.update(allow_runtime_turns=False,trial_expires_at=0);save_private(path,config)
        self.call('pantheon_authorize',args)
        self.assertFalse(load_private(path)['allow_runtime_turns'])
        self.assertEqual(load_private(path)['trial_expires_at'],0)
