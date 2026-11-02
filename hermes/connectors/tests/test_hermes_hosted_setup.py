import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from pantheon_connectors import hermes_setup, hermes_limits
from pantheon_connectors.hermes_plugin import NativePlugin
from pantheon_connectors.onboarding import HERMES_COMMIT, Onboarding
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked
from test_hosted_discovery import descriptor


class Context:
    profile_name='default'
    def inject_message(self,*args,**kwargs):raise AssertionError('Preparation cannot inject')


class HostedHermesTests(unittest.TestCase):
    def setUp(self):
        # This fixture exercises the retained v2 contract even when it ships
        # inside a v3 distribution. Never consult a real package or live service.
        broker = patch('pantheon_connectors.hosted_v3.installed_hermes', return_value=None)
        broker.start(); self.addCleanup(broker.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.plugin=NativePlugin(Context(),self.root/'legacy.json','git:'+HERMES_COMMIT,HERMES_COMMIT,
                                 prepare_root=self.root/'hosted', incoming_probe=lambda _: True)
        self.addCleanup(self.plugin.close)
        self.plugin.observe(session_id='chosen',platform='desktop',model='fixture-model',provider='fixture-provider')
        self.binding=self.plugin.binding('chosen')
        self.link='https://pantheon.talk/join#invite='+'Q'*40
        self.d=descriptor();self.d['packages']['hermes']['version']=hermes_setup.VERSION;self.d['packages']['hermes']['host']['runtime_version']=self.binding['version']
        self.calls=[]
        def discover(origin,runtime):self.calls.append((origin,runtime));return copy.deepcopy(self.d)
        self.discover=discover
        original=hermes_setup.prepare
        def prepared(root,binding,args,**kwargs):return original(root,binding,args,discover=discover,verify=lambda _:None,**kwargs)
        self.patch=patch('pantheon_connectors.hermes_setup.prepare',side_effect=prepared)
        self.patch.start();self.addCleanup(self.patch.stop)

    def prepare(self,link=None,session='chosen',**extra):
        return json.loads(self.plugin.handle('pantheon_prepare',dict(link=link or self.link,deliberate=True,**extra),session_id=session))

    def test_native_prepare_disabled_without_credentials_receiver_or_profile_write(self):
        result=self.prepare();self.assertEqual(result['status'],'prepared_disabled')
        setup=Path(result['setup_file']);config=load_private(setup)
        self.assertFalse(config['allow_runtime_turns']);self.assertEqual(config['trial_expires_at'],0)
        self.assertEqual(config['trial_started_at'],0);self.assertEqual(config['notification_policy'],{'mode':'unlimited'});self.assertNotIn('hermes_notice_limit',config)
        self.assertNotIn('hermes_trial_duration_seconds',config)
        self.assertEqual(config['prepared_binding'],self.binding)
        self.assertNotIn('credential',config);self.assertNotIn('invitation',config)
        self.assertEqual(list(Path(config['state_dir']).iterdir()),[])
        self.assertEqual(self.plugin.active,{})
        self.assertTrue(all(child.active=={} and child.expiry_thread is None for child in self.plugin.prepared.values()))
        self.assertFalse((self.root/'legacy.json').exists())
        self.assertNotIn('Q'*40,setup.read_text())
        doctor=json.loads(self.plugin.handle('pantheon_doctor',{},session_id='chosen'))
        self.assertEqual(doctor['status'],'trial_approval_required')
        self.assertIsNone(doctor['limits']['duration_seconds'])
        self.assertIsNone(doctor['limits']['maximum_duration_seconds'])
        self.assertEqual(doctor['limits']['runtime_policy'],{'mode':'unapproved'})
        self.assertEqual(doctor['limits']['supported_runtime_policies'],['finite','connection'])
        self.assertEqual(doctor['limits']['proposed_duration_seconds'],3600)
        joined=json.loads(self.plugin.handle('pantheon_join',dict(link=self.link,deliberate=True),session_id='chosen'))
        self.assertEqual(joined['status'],'blocked')

    def authorize(self, connection):
        return json.loads(self.plugin.handle('pantheon_authorize',dict(connection=connection,
            deliberate=True,runtime_policy={'mode':'connection'}),session_id='chosen'))

    def test_explicit_new_connection_consent_has_no_timer_and_starts_no_work(self):
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        before=load_private(setup)
        self.assertTrue(before['connection_runtime_supported'])
        self.assertNotIn('runtime_policy',before)
        with patch('time.time',return_value=1000):
            self.assertEqual(self.authorize(prepared['connection'])['status'],'runtime_authorized')
            approved=load_private(setup)
            self.assertEqual(approved['trial_expires_at'],0)
            self.assertEqual(approved['runtime_started_at'],1000)
            self.assertEqual(approved['runtime_policy'],{'mode':'connection'})
            self.assertTrue(hermes_limits.trial_allowed(approved))
        with patch('time.time',return_value=10000):
            self.assertTrue(hermes_limits.trial_allowed(approved))
            self.assertIsNone(hermes_limits.describe(approved)['remaining_seconds'])
            self.assertIsNone(hermes_limits.describe(approved)['maximum_duration_seconds'])
            self.assertEqual(self.authorize(prepared['connection'])['status'],'runtime_authorized_existing')
        self.assertEqual(load_private(setup),approved)
        self.assertEqual(list(Path(approved['state_dir']).iterdir()),[])
        self.assertTrue(all(child.active=={} and child.expiry_thread is None for child in self.plugin.prepared.values()))
        self.assertEqual(self.prepare()['status'],'prepared_existing')
        self.assertEqual(load_private(setup),approved)
        save_private(setup,approved | dict(allow_runtime_turns=False))
        self.assertEqual(self.authorize(prepared['connection'])['status'],'runtime_not_authorized')
        self.assertFalse(load_private(setup)['allow_runtime_turns'])

    def test_connection_consent_cannot_widen_existing_finite_or_changed_grants(self):
        for fields in (dict(trial_started_at=10,trial_expires_at=20),
                       dict(trial_started_at=10,trial_expires_at=0),
                       dict(hermes_trial_duration_seconds=600),
                       dict(connection_runtime_supported=False),
                       dict(allow_runtime_turns=True)):
            with self.subTest(fields=fields):
                link=self.link.replace('Q'*40,hashlib.sha256(repr(fields).encode()).hexdigest())
                prepared=self.prepare(link);setup=Path(prepared['setup_file'])
                saved=load_private(setup) | fields;save_private(setup,saved)
                self.assertEqual(self.authorize(prepared['connection'])['status'],'blocked')
                self.assertEqual(load_private(setup),saved)
        prepared=self.prepare();setup=Path(prepared['setup_file'])
        self.assertEqual(self.authorize(prepared['connection'])['status'],'runtime_authorized')
        saved=load_private(setup);saved['runtime_started_at'] -= 1;save_private(setup,saved)
        self.assertEqual(self.authorize(prepared['connection'])['status'],'blocked')
        self.assertFalse(hermes_limits.trial_allowed(saved))
        self.assertEqual(load_private(setup),saved)

    def test_shipped_skill_does_not_offer_a_trial_or_assume_subscription_coverage(self):
        skill=(Path(hermes_setup.__file__).parent/'skills/collaborate/SKILL.md').read_text()
        self.assertNotIn('Propose the short default of ten minutes',skill)
        self.assertNotIn('with a short `trial_expires_at`',skill)
        self.assertNotIn('with proposed `hermes_trial_duration_seconds` 600',skill)
        normalized=' '.join(skill.split())
        for text in ('one hour or the already requested shorter window',
                     'fixed absolute deadline in the review',
                     'No separate prepare, authorize, join, name, profile, timezone',
                     'explicitly chose connection lifetime',
                     'rather than assuming free use',
                     'No retry, reload, room extension or notice policy can renew a grant'):
            self.assertIn(text, normalized)

    def test_unknown_native_identity_and_untrusted_origin_write_nothing(self):
        self.assertEqual(self.prepare(session='other')['status'],'blocked')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.prepare(self.link.replace('pantheon.talk','evil.example'))['status'],'blocked')
        self.assertEqual(self.prepare(session_id='invented')['status'],'blocked')
        self.assertFalse((self.root/'hosted').exists())

    def test_two_invitations_and_native_sessions_are_isolated(self):
        first=self.prepare();second=self.prepare(self.link.replace('Q'*40,'b'*40))
        self.assertNotEqual(first['setup_file'],second['setup_file'])
        self.plugin.observe(session_id='other',platform='desktop',model='fixture-model',provider='fixture-provider')
        third=self.prepare(session='other')
        self.assertNotEqual(first['setup_file'],third['setup_file'])
        chosen=hermes_setup.select(self.root/'hosted',self.binding,{'connection':first['connection']})
        self.assertEqual(str(chosen),first['setup_file'])
        with self.assertRaises(Blocked):hermes_setup.select(self.root/'hosted',self.binding,{'connection':'f'*32})
        with self.assertRaises(Blocked):hermes_setup.select(self.root/'hosted',self.plugin.binding('other'),{'connection':second['connection']})

    def test_retry_preserves_expiry_budget_and_journal_and_reload_binding(self):
        first=self.prepare();setup=Path(first['setup_file']);saved=load_private(setup)
        saved.update(trial_started_at=10,trial_expires_at=0,hermes_trial_duration_seconds=600,
                     notification_policy={'mode':'limited','limit':8},cleanup={'preserve':True})
        save_private(setup,saved);journal=Path(saved['state_dir'])/'evidence';journal.write_text('preserve')
        self.assertEqual(self.prepare()['status'],'prepared_existing')
        self.assertEqual(load_private(setup),saved);self.assertEqual(journal.read_text(),'preserve')
        self.d['expires_at'] += 60
        self.assertEqual(self.prepare()['status'],'prepared_existing')
        refreshed=load_private(setup)
        self.assertEqual(refreshed['hosted_onboarding']['expires_at'],self.d['expires_at'])
        self.assertEqual(refreshed['trial_expires_at'],0)
        self.assertEqual(refreshed['trial_started_at'],10)
        replacement=NativePlugin(Context(),self.root/'legacy.json','git:'+HERMES_COMMIT,HERMES_COMMIT,prepare_root=self.root/'hosted', incoming_probe=lambda _: True)
        self.addCleanup(replacement.close)
        self.assertEqual(json.loads(replacement.handle('pantheon_doctor',{},session_id='chosen'))['status'],'session_observation_pending')
        replacement.observe(session_id='chosen',platform='desktop',model='fixture-model',provider='fixture-provider')
        self.assertEqual(json.loads(replacement.handle('pantheon_doctor',{},session_id='chosen'))['status'],'trial_approval_required')
        self.plugin.close()
        self.assertTrue(all(child.stop.is_set() for child in self.plugin.prepared.values()))
        self.assertEqual(self.prepare()['status'],'blocked')

    def test_changed_package_or_symlink_cannot_replace_setup(self):
        first=self.prepare();setup=Path(first['setup_file']);before=setup.read_bytes()
        self.d['packages']['hermes']['source_revision']='c'*40
        self.assertEqual(self.prepare()['status'],'blocked');self.assertEqual(setup.read_bytes(),before)
        self.d['packages']['hermes']['source_revision']='b'*40
        setup.rename(setup.with_suffix('.old'));setup.symlink_to(setup.with_suffix('.old'))
        self.assertEqual(self.prepare()['status'],'blocked')

    def test_hosted_preparation_preserves_explicit_legacy_connection_routing(self):
        self.prepare()
        connection='e'*32
        state=self.root/'legacy-state';state.mkdir(mode=0o700)
        config=dict(url='http://127.0.0.1:12345',state_dir=str(state),allow_runtime_turns=False,trial_expires_at=0)
        save_private(self.root/'legacy.json',config)
        save_private(state/(connection+'.json'),dict(binding=self.binding,url=config['url'],redeemed=True,credential='fixture'))
        status=json.loads(self.plugin.handle('pantheon_status',{'connection':connection},session_id='chosen'))
        self.assertEqual(status['status'],'runtime_not_authorized')
        self.assertEqual(load_private(self.root/'legacy.json'),config)
        self.plugin.observe(session_id='other',platform='desktop')
        result=json.loads(self.plugin.handle('pantheon_status',{'connection':connection},session_id='other'))
        self.assertEqual(result['status'],'blocked')

    def test_native_hosted_join_rechecks_admission_before_minting_and_reuses_binding(self):
        requests=[]
        outer=self
        class FakeHTTP:
            def request(self,path,payload=None):
                requests.append((path,copy.deepcopy(payload)))
                if path=='/v1/manifest':return {'protocol':'pyros.v1','local_test':False}
                if path=='/v1/preview':return {'purpose':'untrusted task text'}
                if path=='/v1/onboarding':return copy.deepcopy(outer.d)
                if path=='/v1/redeem':return {'id':'hermes-member'}
                raise AssertionError(path)
        def factory(origin,credential):
            self.assertEqual(origin,'https://pantheon.talk')
            return FakeHTTP()
        self.plugin.service_factory=lambda config:Onboarding(config,client_factory=factory)
        result=self.prepare();setup=Path(result['setup_file']);config=load_private(setup)
        config.update(allow_runtime_turns=True,trial_started_at=time.time(),trial_expires_at=time.time()+60)
        save_private(setup,config)
        self.d['admission']['enabled']=False
        join=lambda:json.loads(self.plugin.handle('pantheon_join',{'link':self.link,'deliberate':True},session_id='chosen'))
        self.assertEqual(join()['status'],'blocked')
        self.assertEqual(list(Path(config['state_dir']).iterdir()),[])
        self.assertNotIn('/v1/redeem',[path for path,_ in requests])
        self.d['admission']['enabled']=True
        with patch.object(NativePlugin,'start',return_value=True) as start:
            first=join();second=join()
        self.assertEqual(first['status'],'joined_awaiting_real_agent_probe')
        self.assertEqual(first['connection'],second['connection'])
        redemptions=[payload for path,payload in requests if path=='/v1/redeem']
        self.assertEqual(len(redemptions),2);self.assertEqual(redemptions[0],redemptions[1])
        self.assertEqual(redemptions[0]['binding'],self.binding)
        self.assertEqual(start.call_args.args,(first['connection'],self.binding))

    def test_installed_provenance_is_checked_before_preparation(self):
        package=self.d['packages']['hermes'];root=self.root/'package';root.mkdir()
        names=['plugin.yaml','__init__.py','connectors/pantheon_connectors/hermes_plugin.py',
               'connectors/pantheon_connectors/hermes_setup.py','connectors/pantheon_connectors/hosted_discovery.py']
        records={}
        for name in names:
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'fixture')
            records[name]={'sha256':hashlib.sha256(b'fixture').hexdigest()}
        (root/'SOURCE-PROVENANCE.json').write_text(json.dumps({'source_commit':package['source_revision'],'files':records}))
        hermes_setup.verify_installed(package,root)
        (root/'plugin.yaml').write_text('modified')
        with self.assertRaises(Blocked):hermes_setup.verify_installed(package,root)

    def test_verified_staging_creates_only_local_native_install_source(self):
        def download(descriptor,runtime,output,**kwargs):
            output.mkdir();(output/'plugin.yaml').write_text('fixture')
            return {'directory':str(output),'installed':False}
        output=self.root/'inert-source'
        with patch('pantheon_connectors.hermes_setup.download_package',side_effect=download), patch(
                'pantheon_connectors.hermes_setup.verify_installed') as verify:
            result=hermes_setup.stage_native_source(self.d,output)
        verify.assert_called_once_with(self.d['packages']['hermes'],output)
        self.assertFalse(result['installed']);self.assertEqual(result['source'],output.as_uri())
        self.assertRegex(result['artifact_commit'],r'^[0-9a-f]{40}$')
        self.assertFalse((self.root/'hosted').exists())
        with self.assertRaises(Blocked):hermes_setup.stage_native_source(self.d,output/'nested')
