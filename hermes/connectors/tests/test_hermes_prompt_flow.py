"""Read-only next steps and consent reuse; no host, provider or live grant calls."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from pantheon_connectors import hermes_review
from pantheon_connectors.state import load_private, save_private
import test_hermes_duration_default as defaults


class PromptFlow(unittest.TestCase):
    setUp = defaults.DurationDefault.setUp
    prepare = defaults.DurationDefault.prepare
    authorize = defaults.DurationDefault.authorize

    def test_fresh_setup_uses_one_review_not_preparation_duration_start_questions(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);before=setup.read_bytes()
        flow=prepared['onboarding']
        self.assertEqual(flow['pantheon_confirmations'],1)
        self.assertEqual(flow['extra_preparation_duration_start_confirmations'],0)
        self.assertEqual(flow['proposed_duration_seconds'],3600)
        self.assertEqual(flow['next_action'],'preview_scope_then_one_missing_approval')
        self.assertEqual(flow['host_prompts'],'native_host_managed_not_bypassed')
        self.assertIn('provider/incoming scope',flow['count_condition'])
        self.assertIn('room/goal and usage checks',prepared['notice'])
        self.assertFalse(load_private(setup)['allow_runtime_turns'])
        self.public.request.assert_not_called()
        doctor=json.loads(self.plugin.handle('pantheon_doctor',{},session_id='chosen'))
        self.assertEqual(doctor['onboarding'],flow)
        self.assertEqual(setup.read_bytes(),before)
        self.assertEqual(self.plugin.active,{})

    def test_exact_still_valid_approval_skips_additional_confirmation_and_does_not_extend(self):
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        approved=self.authorize(prepared)
        self.assertEqual(approved['onboarding']['pantheon_confirmations'],0)
        self.assertEqual(approved['next_action'],'continue_same_scope_after_member_and_access_checks')
        saved=setup.read_bytes();self.public.reset_mock()
        self.now+=30
        with patch('time.time',return_value=self.now):
            reused=self.prepare()
            doctor=json.loads(self.plugin.handle('pantheon_doctor',{},session_id='chosen'))
        self.assertEqual(reused['onboarding']['state'],'saved_runtime_approval_valid_locally')
        self.assertEqual(reused['onboarding']['pantheon_confirmations'],0)
        self.assertIn('not proof of host permission',reused['notice'])
        self.assertEqual(doctor['onboarding']['pantheon_confirmations'],0)
        self.assertEqual(setup.read_bytes(),saved)
        self.public.request.assert_not_called()
        self.assertTrue(all(child.active=={} for child in self.plugin.prepared.values()))

    def test_new_invitation_cannot_inherit_approval_and_binding_change_needs_scope_check(self):
        first=self.prepare();self.authorize(first)
        second=self.prepare(self.link.replace('Q'*40,'z'*40))
        self.assertNotEqual(first['connection'],second['connection'])
        self.assertEqual(second['onboarding']['pantheon_confirmations'],1)
        config=load_private(Path(first['setup_file']));before=copy.deepcopy(config)
        changed=hermes_review.progress(config,self.binding | dict(session='different'))
        self.assertEqual(changed['next_action'],'check_exact_saved_scope')
        self.assertIsNone(changed['pantheon_confirmations'])
        self.assertEqual(config,before)
        legacy=load_private(Path(second['setup_file']))
        legacy.pop('hermes_finite_runtime_supported');legacy.pop('connection_runtime_supported')
        legacy['hermes_trial_duration_seconds']=600
        self.assertEqual(hermes_review.progress(legacy,self.binding)['next_action'],'check_existing_legacy_scope')
        self.assertEqual(legacy['hermes_trial_duration_seconds'],600)

    def test_expired_and_disabled_approval_report_only_stop_blocker_without_renewal(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);self.authorize(prepared)
        saved=setup.read_bytes();self.now+=3601;self.public.reset_mock()
        with patch('time.time',return_value=self.now):flow=self.prepare()['onboarding']
        self.assertEqual(flow['state'],'saved_scope_expired')
        self.assertEqual(flow['next_action'],'stop_preserve_expired_scope')
        self.assertEqual(flow['pantheon_confirmations'],0)
        self.assertEqual(setup.read_bytes(),saved)
        disabled=load_private(setup) | dict(allow_runtime_turns=False)
        save_private(setup,disabled);before=setup.read_bytes()
        with patch('time.time',return_value=self.now):flow=self.prepare()['onboarding']
        self.assertEqual(flow['next_action'],'stop_preserve_disabled_scope')
        self.assertEqual(flow['pantheon_confirmations'],0)
        self.assertEqual(setup.read_bytes(),before)
        self.public.request.assert_not_called()

    def test_native_identity_pending_and_invalid_saved_data_do_not_restart_onboarding(self):
        pending=json.loads(self.plugin.handle('pantheon_doctor',{},session_id='unknown'))
        self.assertEqual(pending['next_action'],'check_doctor_next_turn_same_conversation')
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        config=load_private(setup) | dict(hermes_trial_duration_seconds='bad')
        save_private(setup,config);before=setup.read_bytes()
        flow=hermes_review.progress(config,self.binding)
        self.assertEqual(flow['next_action'],'stop_preserve_saved_scope')
        self.assertEqual(flow['pantheon_confirmations'],0)
        self.assertEqual(setup.read_bytes(),before)

    def test_server_revocation_overrides_saved_approval_without_redeeming_or_reprompting(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);self.authorize(prepared)
        config=load_private(setup)
        save_private(Path(config['state_dir'])/(prepared['connection']+'.json'),dict(
            url=config['url'],binding=self.binding,credential='fixture',redeemed=True,
            connection_runtime_supported=True,trial_expires_at=config['trial_expires_at']))
        self.public.reset_mock()
        self.public.request.return_value={'role':'agent','member':'a','chat':dict(state='active',
            members={'a':dict(binding=self.binding,revoked=True)})}
        self.public.request.side_effect=None
        with patch('time.time',return_value=self.now):
            result=json.loads(self.plugin.handle('pantheon_status',dict(connection=prepared['connection']),session_id='chosen'))
            flow=self.prepare()['onboarding']
        self.assertEqual(result['status'],'blocked')
        self.assertIn('report_specific_blocker',result['next_action'])
        self.assertEqual(flow['next_action'],'stop_preserve_disabled_scope')
        self.assertEqual(flow['pantheon_confirmations'],0)
        self.public.request.assert_called_once_with('/v1/chat')
        self.assertFalse(load_private(setup)['allow_runtime_turns'])
        self.assertTrue(all(child.active=={} for child in self.plugin.prepared.values()))

    def test_doctor_binding_mismatch_cannot_offer_reusable_consent(self):
        prepared=self.prepare();setup=Path(prepared['setup_file']);self.authorize(prepared)
        config=load_private(setup);config['prepared_binding']=self.binding | dict(session='another')
        save_private(setup,config);before=setup.read_bytes()
        child=self.plugin.prepared[setup]
        result=child.doctor('chosen')
        self.assertEqual(result['status'],'blocked')
        self.assertNotIn('onboarding',result)
        self.assertIn('report_specific_blocker',result['next_action'])
        self.assertEqual(setup.read_bytes(),before)

    def test_shipped_guidance_counts_native_prompts_separately_and_continues_bundled_yes(self):
        skill=' '.join((Path(hermes_review.__file__).parent/'skills/collaborate/SKILL.md').read_text().split())
        for text in ('If the exact scope is already approved, include consent immediately',
                     'otherwise connect returns the preview and one missing review',
                     'normal native permission prompts', 'existing model/provider',
                     'No separate prepare, authorize, join',
                     'Provider changes, expired grants and actual revocations stop the connection'):
            self.assertIn(text,skill)
