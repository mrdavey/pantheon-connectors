"""Default duration is a proposal, not consent; all grants here are temporary fixtures."""
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
from pantheon_connectors import hermes_limits, hermes_setup
from pantheon_connectors.onboarding import Onboarding
from pantheon_connectors.hermes_plugin import NativePlugin
from pantheon_connectors.state import load_private, save_private
import test_hermes_hosted_setup as hosted


class DurationDefault(unittest.TestCase):
    prepare = hosted.HostedHermesTests.prepare

    def setUp(self):
        hosted.HostedHermesTests.setUp(self)
        self.now=datetime(2026,10,15,22,tzinfo=timezone.utc).timestamp()
        self.d["expires_at"]=self.now+7200
        self.invitation_expiry=self.now+7200
        self.public=Mock()
        def request(route,payload=None):
            if route=='/v1/manifest':return {'protocol':'pyros.v1','local_test':False}
            if route=='/v1/preview':
                return {'expires':datetime.fromtimestamp(self.invitation_expiry,timezone.utc).isoformat()}
            raise AssertionError(route)
        self.public.request.side_effect=request
        self.plugin.service_factory=lambda config:Onboarding(config,client_factory=lambda *_:self.public)

    def authorize(self, prepared, **extra):
        args=dict(connection=prepared['connection'],deliberate=True,link=self.link,timezone='America/Los_Angeles')
        args.update(extra)
        with patch('time.time',return_value=self.now):
            return json.loads(self.plugin.handle('pantheon_authorize',args,session_id='chosen'))

    def test_omitted_duration_defaults_to_hour_without_consent_or_preparation_timer(self):
        with patch('time.time',return_value=self.now-5000):prepared=self.prepare()
        setup=Path(prepared['setup_file']);disabled=load_private(setup)
        self.assertEqual(prepared['proposed_duration_seconds'],3600)
        self.assertIn('do not ask a separate duration-selection question',prepared['duration_notice'])
        self.assertEqual(disabled['trial_started_at'],0);self.assertEqual(disabled['trial_expires_at'],0)
        self.assertFalse(disabled['allow_runtime_turns']);self.assertNotIn('hermes_trial_duration_seconds',disabled)
        self.assertEqual(self.authorize(prepared,deliberate=False)['status'],'blocked')
        self.public.request.assert_not_called();self.assertEqual(load_private(setup),disabled)
        approved=self.authorize(prepared)
        self.assertEqual(approved['status'],'runtime_authorized')
        config=load_private(setup)
        self.assertEqual(config['trial_started_at'],self.now)
        self.assertEqual(config['trial_expires_at'],self.now+3600)
        self.assertEqual(config['hermes_trial_duration_seconds'],3600)
        self.assertEqual(approved['runtime']['end_time'],'4:00:00 PM PDT on 15 October 2026')
        self.assertIn('No automatic renewal',approved['runtime']['notice'])
        self.assertEqual(list(Path(config['state_dir']).iterdir()),[])
        self.assertTrue(all(child.active=={} for child in self.plugin.prepared.values()))
        self.assertEqual([call.args[0] for call in self.public.request.call_args_list],['/v1/manifest','/v1/preview'])

    def test_explicit_shorter_preparation_and_activation_durations_are_respected(self):
        prepared=self.prepare(duration_seconds=900)
        self.assertEqual(prepared['proposed_duration_seconds'],900)
        approved=self.authorize(prepared)
        self.assertEqual(approved['runtime']['expires_at'],self.now+900)
        self.assertEqual(approved['runtime']['end_time'],'3:15:00 PM PDT on 15 October 2026')
        # Preparation retries cannot replace an already-approved duration.
        self.assertEqual(self.prepare(duration_seconds=3600)['proposed_duration_seconds'],900)
        self.assertEqual(self.authorize(prepared,duration_seconds=3600)['status'],'blocked')
        other=self.prepare(self.link.replace('Q'*40,'r'*40))
        shortened=self.authorize(other,link=self.link.replace('Q'*40,'r'*40),duration_seconds=1200)
        self.assertEqual(shortened['runtime']['expires_at'],self.now+1200)

    def test_invitation_and_room_expiry_are_earlier_bounds_and_retry_never_extends(self):
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        self.invitation_expiry=self.now+300
        approved=self.authorize(prepared);saved=setup.read_bytes()
        self.assertEqual(approved['runtime']['expires_at'],self.invitation_expiry)
        config=load_private(setup)
        room=datetime.fromtimestamp(self.now+120,timezone.utc).isoformat()
        self.assertEqual(hermes_setup.finite_disclosure(config,room)['expires_at'],self.now+120)
        self.public.reset_mock();self.now+=100
        self.assertEqual(self.authorize(prepared)['runtime'],approved['runtime'])
        self.public.request.assert_not_called();self.assertEqual(setup.read_bytes(),saved)
        self.now+=300
        self.assertEqual(self.authorize(prepared)['status'],'runtime_not_authorized')
        self.assertEqual(setup.read_bytes(),saved)
        child=self.plugin.prepared[setup]
        with patch('time.time',return_value=self.now):child.expire(load_private(setup))
        fenced=setup.read_bytes()
        self.assertEqual(self.authorize(prepared)['status'],'runtime_not_authorized')
        self.assertEqual(setup.read_bytes(),fenced)
        self.assertFalse(load_private(setup)['allow_runtime_turns'])

    def test_expired_invitation_and_invalid_durations_do_not_activate(self):
        for value in (0,3601,True,1.5,'60'):
            self.assertEqual(self.prepare(duration_seconds=value)['status'],'blocked')
        prepared=self.prepare();setup=Path(prepared['setup_file']);before=setup.read_bytes()
        for value in (0,3601,True,1.5,'60'):
            self.assertEqual(self.authorize(prepared,duration_seconds=value)['status'],'blocked')
        self.invitation_expiry=self.now-1
        self.assertEqual(self.authorize(prepared)['status'],'blocked')
        self.assertEqual(setup.read_bytes(),before)

    def test_existing_finite_and_connection_lifetime_approvals_are_unchanged(self):
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        legacy=load_private(setup) | dict(trial_started_at=1000,trial_expires_at=0,hermes_trial_duration_seconds=600)
        save_private(setup,legacy);self.assertEqual(self.authorize(prepared)['status'],'blocked')
        self.assertEqual(load_private(setup),legacy)
        other_link=self.link.replace('Q'*40,'u'*40);other=self.prepare(other_link);other_setup=Path(other['setup_file'])
        args=dict(connection=other['connection'],deliberate=True,runtime_policy={'mode':'connection'})
        result=json.loads(self.plugin.handle('pantheon_authorize',args,session_id='chosen'))
        self.assertEqual(result['status'],'runtime_authorized');before=other_setup.read_bytes()
        self.assertEqual(self.authorize(other,link=other_link)['status'],'blocked')
        self.assertEqual(other_setup.read_bytes(),before)

    def test_native_join_reports_earlier_room_end_and_closure_stops_new_finite_work(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);self.authorize(prepared)
        room=dict(state='active',expiry=datetime.fromtimestamp(self.now+120,timezone.utc).isoformat(),
            members={'a':dict(binding=self.binding,revoked=False)})
        def request(route,payload=None):
            if route=='/v1/manifest':return {'protocol':'pyros.v1','local_test':False}
            if route=='/v1/preview':return {'expires':datetime.fromtimestamp(self.invitation_expiry,timezone.utc).isoformat()}
            if route=='/v1/onboarding':return self.d
            if route=='/v1/redeem':return {'id':'a'}
            if route=='/v1/chat':return {'role':'agent','member':'a','chat':room}
            raise AssertionError(route)
        self.public.request.side_effect=request
        with patch('time.time',return_value=self.now),patch.object(NativePlugin,'start',return_value=True):
            result=json.loads(self.plugin.handle('pantheon_join',dict(link=self.link,deliberate=True),session_id='chosen'))
        self.assertEqual(result['status'],'joined_awaiting_real_agent_probe',result)
        self.assertEqual(result['runtime']['expires_at'],self.now+120)
        self.assertEqual(result['runtime']['end_time'],'3:02:00 PM PDT on 15 October 2026')
        self.assertEqual(load_private(setup)['trial_expires_at'],self.now+3600)
        self.assertEqual(load_private(setup)['hermes_room_expires_at'],self.now+120)
        room['expiry']=datetime.fromtimestamp(self.now+1800,timezone.utc).isoformat()
        self.assertEqual(self.authorize(prepared)['runtime']['expires_at'],self.now+120)
        with patch('time.time',return_value=self.now+121):
            self.assertFalse(hermes_limits.trial_allowed(load_private(setup)))
        room['state']='closed';self.public.reset_mock()
        with patch('time.time',return_value=self.now):
            result=json.loads(self.plugin.handle('pantheon_post',dict(connection=prepared['connection'],
                text='must not send',key='post'),session_id='chosen'))
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(load_private(setup)['allow_runtime_turns'])
        self.assertEqual([call.args[0] for call in self.public.request.call_args_list],['/v1/chat'])

    def test_default_utc_is_labeled_and_timezone_validation_stays_display_only(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);before=setup.read_bytes()
        self.assertEqual(self.authorize(prepared,timezone='Unknown/Zone')['status'],'blocked')
        self.assertEqual(setup.read_bytes(),before)
        with patch('time.time',return_value=self.now):
            result=hermes_setup.authorize(setup,self.binding,dict(connection=prepared['connection'],
                deliberate=True,link=self.link),preview=lambda _: {'scope':{'expires':
                    datetime.fromtimestamp(self.invitation_expiry,timezone.utc).isoformat()}})
        self.assertEqual(result['runtime']['end_time'],'11:00:00 PM UTC on 15 October 2026')

    def test_explicit_notice_cap_is_native_immutable_and_not_a_new_window(self):
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        policy={'mode':'limited','limit':8}
        approved=self.authorize(prepared,notification_policy=policy)
        self.assertEqual(approved['status'],'runtime_authorized')
        config=load_private(setup);saved=setup.read_bytes()
        self.assertEqual(config['notification_policy'],policy)
        self.assertEqual(config['hermes_finite_runtime_grant']['notification_policy'],policy)
        with patch('time.time',return_value=self.now):
            self.assertTrue(hermes_limits.trial_allowed(config))
            self.assertEqual(hermes_limits.describe(config,8)['notices_remaining'],0)
            self.assertTrue(hermes_limits.describe(config,8)['budget_exhausted'])
        self.now+=60
        self.assertEqual(self.authorize(prepared,notification_policy=policy)['runtime'],approved['runtime'])
        self.assertEqual(self.authorize(prepared)['status'],'runtime_authorized_existing')
        for replacement in ({'mode':'unlimited'},{'mode':'limited','limit':7}):
            self.assertEqual(self.authorize(prepared,notification_policy=replacement)['status'],'blocked')
        self.assertEqual(setup.read_bytes(),saved)
        self.now+=3600
        self.assertEqual(self.authorize(prepared,notification_policy=policy)['status'],'runtime_not_authorized')
        self.assertEqual(setup.read_bytes(),saved)

    def test_invalid_notice_caps_and_expired_preview_leave_preparation_untouched(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);before=setup.read_bytes()
        for policy in (None,{}, {'mode':'limited','limit':True},{'mode':'limited','limit':0},
                       {'mode':'limited','limit':9},{'mode':'limited','limit':1.5},
                       {'mode':'unlimited','limit':8}):
            self.assertEqual(self.authorize(prepared,notification_policy=policy)['status'],'blocked')
            self.assertEqual(setup.read_bytes(),before)
        self.invitation_expiry=self.now-1
        self.assertEqual(self.authorize(prepared,notification_policy={'mode':'limited','limit':8})['status'],'blocked')
        self.assertEqual(setup.read_bytes(),before)

    def test_connection_lifetime_accepts_same_cap_without_reset_or_widening(self):
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        args=dict(connection=prepared['connection'],deliberate=True,
                  runtime_policy={'mode':'connection'},notification_policy={'mode':'limited','limit':8})
        def authorize():
            return json.loads(self.plugin.handle('pantheon_authorize',args,session_id='chosen'))
        self.assertEqual(authorize()['status'],'runtime_authorized')
        config=load_private(setup);saved=setup.read_bytes()
        self.assertEqual(config['hermes_runtime_grant']['notification_policy'],args['notification_policy'])
        self.assertTrue(hermes_limits.trial_allowed(config))
        self.assertEqual(authorize()['status'],'runtime_authorized_existing')
        for replacement in ({'mode':'unlimited'},{'mode':'limited','limit':7}):
            args['notification_policy']=replacement
            self.assertEqual(authorize()['status'],'blocked')
        self.assertEqual(setup.read_bytes(),saved)
        config['allow_runtime_turns']=False;save_private(setup,config)
        args['notification_policy']={'mode':'limited','limit':8}
        self.assertEqual(authorize()['status'],'runtime_not_authorized')
        self.assertFalse(load_private(setup)['allow_runtime_turns'])
