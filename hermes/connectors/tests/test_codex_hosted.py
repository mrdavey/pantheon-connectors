import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from pantheon_connectors import codex_hosted as hosted
from pantheon_connectors.state import save_private, load_private
from pantheon_connectors.worker import Blocked

THREAD='00000000-0000-4000-8000-000000000111'
OTHER='00000000-0000-4000-8000-000000000222'
BASE='https://pantheon.talk'
TOKEN='a'*43
LINK=BASE+'/join#invite='+TOKEN
CONNECTION=hashlib.sha256((BASE+'\0'+TOKEN).encode()).hexdigest()[:32]

class HostedCodexTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()/'private'/'hosted'
        self.server=hosted.HostedServer(self.root);self.addCleanup(self.server.close)
        self.metadata=dict(version=3,delivery='default-daemon-tool-output',service_origin=BASE,package_version='0.5.8',source_revision='b'*40)
        self.discovery=dict(schema='pantheon.hosted-onboarding.v2',service_origin=BASE,expires_at=time.time()+3600,admission=dict(enabled=True,runtimes=['codex']),packages={'codex':dict(runtime='codex',version='0.5.8',source_revision='b'*40,sha256='a'*64,format='tar.gz',artifact_url=BASE+'/plugins/'+'a'*64+'/codex.tar.gz',host=dict(runtime_version='0.162.1'))})
        for target,value in [('descriptor',self.metadata),('fetch_discovery',self.discovery),('validate_hosted_admission',self.discovery)]:
            p=patch.object(hosted,target,return_value=value);p.start();self.addCleanup(p.stop)
        def prepare(path,url,profile,**kw):
            save_private(path,dict(url=url,state_dir=str(path.parent / "state"),binding=dict(runtime='codex',version='0.162.1',surface='app-server-tool-output',profile=profile,session=kw['native_thread']),delivery='default-daemon-tool-output',allow_runtime_turns=False,allow_tool_output=False,allow_daemon_access=False,trial_expires_at=0,notification_policy={'mode':'unlimited'}))
            return dict(status='prepared_disabled',setup_file=str(path))
        p=patch.object(hosted,'prepare',side_effect=prepare);self.prepare=p.start();self.addCleanup(p.stop)
    def call(self,name,args,thread=THREAD):
        return self.server.handle('tools/call',dict(name=name,arguments=args,_meta={'threadId':thread}))['structuredContent']
    def prepare_scope(self,thread=THREAD):
        return self.call('pantheon_prepare',dict(link=LINK,deliberate=True,inspect_default_daemon=True),thread)
    def test_site_tools_are_advertised_without_creating_runtime_or_permissions(self):
        names = {tool['name']:tool for tool in self.server.handle('tools/list', {})['tools']}
        for name in ('pantheon_site_deploy','pantheon_site_status'):
            self.assertIn(name, names)
            self.assertFalse(names[name]['inputSchema']['additionalProperties'])
        self.assertFalse(self.root.exists())
        with self.assertRaises((Blocked, FileNotFoundError)):
            self.call('pantheon_site_status', {'connection': CONNECTION})
        self.assertEqual(self.server.children,{})

    def test_startup_and_discovery_are_inert_and_prepare_stays_disabled(self):
        self.server.handle('initialize',{});self.server.handle('tools/list',{})
        self.assertFalse(self.root.exists())
        result=self.prepare_scope();self.assertEqual(result['connection'],CONNECTION)
        config=load_private(self.server.location(THREAD,CONNECTION))
        for flag in ['allow_runtime_turns','allow_tool_output','allow_daemon_access']:self.assertIs(config[flag],False)
        self.assertEqual(config['trial_expires_at'],0);self.assertEqual(self.server.children,{})
        self.assertNotIn('setup_file',result)
    def test_two_native_conversations_never_share_or_overwrite_setup(self):
        self.prepare_scope();first=self.server.location(THREAD,CONNECTION).read_bytes()
        self.prepare_scope(OTHER)
        self.assertEqual(self.server.location(THREAD,CONNECTION).read_bytes(),first)
        self.assertNotEqual(self.server.location(THREAD,CONNECTION),self.server.location(OTHER,CONNECTION))
        self.prepare_scope();self.assertEqual(self.prepare.call_count,2)
    def test_identity_origin_and_consent_fail_before_setup(self):
        for args in [dict(link=LINK,deliberate=False,inspect_default_daemon=True),dict(link=LINK.replace('pantheon.talk','evil.example'),deliberate=True,inspect_default_daemon=True)]:
            with self.assertRaises(Blocked):self.call('pantheon_prepare',args)
        with self.assertRaises(Blocked):self.call('pantheon_prepare',{},'not-a-thread')
        self.assertFalse(self.root.exists())
    def test_authorize_is_bounded_scoped_and_does_not_start_receiver(self):
        self.prepare_scope()
        args=dict(connection=CONNECTION,deliberate=True,expires_at=time.time()+600,max_notices=8)
        for change in [dict(expires_at=float('inf')),dict(expires_at=time.time()+7200),dict(max_notices=0),dict(deliberate=False)]:
            with self.assertRaises(Blocked):self.call('pantheon_authorize',{**args,**change})
        with self.assertRaises((Blocked,FileNotFoundError)):self.call('pantheon_authorize',args,OTHER)
        value=self.call('pantheon_authorize',args)
        self.assertFalse(value['receiver_started']);self.assertFalse(self.server.children[(THREAD,CONNECTION)].workers)
        before=self.server.location(THREAD,CONNECTION).read_bytes()
        self.call('pantheon_authorize',args)
        self.assertEqual(self.server.location(THREAD,CONNECTION).read_bytes(),before)
        with self.assertRaises(Blocked):self.call('pantheon_authorize',{**args,'max_notices':9})
        config=load_private(self.server.location(THREAD,CONNECTION));self.assertTrue(config['allow_runtime_turns'])
        log=(self.server.location(THREAD,CONNECTION).parent/'codex-audit.jsonl').read_text()
        self.assertNotIn(TOKEN,log);self.assertNotIn(THREAD,log)
        rows=[json.loads(row) for row in log.splitlines()]
        self.assertTrue(all(row['plugin_version']=='0.5.8' for row in rows))
        self.assertTrue(any(row['event']=='tool_completed' and row.get('tool')=='pantheon_prepare' for row in rows))
        self.assertTrue(any(row['event']=='tool_failed' and row.get('tool')=='pantheon_authorize' for row in rows))
    def test_discovery_failure_is_audited_without_setup_or_grant(self):
        from pantheon_connectors.client import APIError
        with patch.object(hosted, 'fetch_discovery', side_effect=APIError(0, 'tls_verification_failed')):
            with self.assertRaises(APIError): self.prepare_scope()
        setup = self.server.location(THREAD, CONNECTION)
        self.assertFalse(setup.exists())
        self.prepare.assert_not_called()
        log = (setup.parent / 'codex-audit.jsonl').read_text()
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(THREAD, log)
        rows = [json.loads(row) for row in log.splitlines()]
        self.assertEqual([r['event'] for r in rows], ['tool_requested', 'tool_failed'])
        self.assertEqual(rows[-1]['reason'], 'tls_verification_failed')
        self.assertEqual(rows[0]['request_id_sha256'], rows[1]['request_id_sha256'])
    def test_connection_lifetime_requires_new_explicit_grant_and_never_reactivates(self):
        from pantheon_connectors.runtime_policy import allowed
        self.prepare_scope()
        path=self.server.location(THREAD,CONNECTION)
        before=load_private(path)
        self.assertNotIn('runtime_policy',before)
        args=dict(connection=CONNECTION,deliberate=True,runtime_policy={'mode':'connection'})
        result=self.call('pantheon_authorize',args)
        config=load_private(path)
        self.assertEqual(result['runtime_policy'],{'mode':'connection'})
        self.assertTrue(allowed(config,now=time.time()+86400))
        again=self.prepare_scope();self.assertEqual(again['runtime_options'],[]);self.assertTrue(again['runtime_authorized'])
        unchanged=path.read_bytes();self.call('pantheon_authorize',args)
        self.assertEqual(path.read_bytes(),unchanged)
        config['allow_runtime_turns']=False;save_private(path,config)
        disabled=path.read_bytes();retry=self.call('pantheon_authorize',args)
        self.assertFalse(retry['runtime_authorized']);self.assertEqual(retry['next_action'],'stop_inactive_grant')
        self.assertEqual(path.read_bytes(),disabled)
        self.assertFalse(allowed(load_private(path)))
    def test_connection_lifetime_cannot_replace_finite_legacy_or_malformed_grants(self):
        self.prepare_scope();path=self.server.location(THREAD,CONNECTION)
        args=dict(connection=CONNECTION,deliberate=True,runtime_policy={'mode':'connection'})
        for change in [dict(expires_at=time.time()+60),dict(runtime_policy={'mode':'other'}),dict(deliberate=False)]:
            with self.assertRaises(Blocked):self.call('pantheon_authorize',{**args,**change})
        config=load_private(path);config.pop('connection_runtime_supported');save_private(path,config)
        with self.assertRaises(Blocked):self.call('pantheon_authorize',args)
        config['connection_runtime_supported']=True;save_private(path,config)
        self.call('pantheon_authorize',dict(connection=CONNECTION,deliberate=True,expires_at=time.time()+60))
        config=load_private(path);config['allow_runtime_turns']=False;config['trial_expires_at']=1;save_private(path,config)
        before=path.read_bytes()
        with self.assertRaises(Blocked):self.call('pantheon_authorize',args)
        self.assertEqual(path.read_bytes(),before)
    def test_join_without_matching_preparation_is_local_and_actionable(self):
        with patch.object(hosted,'fetch_discovery') as fetch:
            with self.assertRaisesRegex(Blocked,'No prepared Pantheon setup exists'):
                self.call('pantheon_join',dict(link=LINK,deliberate=True))
            fetch.assert_not_called()
        self.assertFalse(self.root.exists())
        self.assertEqual(self.server.children,{})

    def test_public_preview_before_preparation_does_not_open_private_state(self):
        from unittest.mock import Mock
        client=Mock()
        client.request.side_effect=[{'protocol':'pyros.v1'},{'purpose':'untrusted scope'}]
        service=hosted.Onboarding({'url':BASE},client_factory=lambda *a:client)
        with patch.object(hosted,'Onboarding',return_value=service),patch.object(hosted,'load_private') as load:
            result=self.call('pantheon_preview',{'link':LINK})
        load.assert_not_called()
        self.assertEqual(result['scope'],{'purpose':'untrusted scope'})
        self.assertEqual([call.args[0] for call in client.request.call_args_list],['/v1/manifest','/v1/preview'])
        self.assertFalse(self.root.exists());self.prepare.assert_not_called()
        self.assertEqual(self.server.children,{})

    def test_fresh_review_runs_offline_checks_without_inspection_or_grant(self):
        self.assertNotIn('inspect_default_daemon',hosted.PREPARE['inputSchema']['required'])
        args=dict(link=LINK,deliberate=True)
        with patch.object(hosted,'cli_compatibility',return_value=('/fixture/codex','fixture')) as cli, \
                patch.object(hosted,'require_capabilities') as caps:
            result=self.call('pantheon_prepare',args)
        cli.assert_called_once_with();caps.assert_called_once_with('/fixture/codex')
        self.assertEqual(result['status'],'inspection_consent_required')
        self.assertFalse(result['native_owner_verified']);self.assertFalse(result['runtime_authorized'])
        self.assertFalse(self.server.location(THREAD,CONNECTION).exists())
        self.prepare.assert_not_called();self.assertEqual(self.server.children,{})
        # Only explicit inspection consent proceeds; runtime remains disabled.
        prepared=self.prepare_scope()
        self.assertEqual(prepared['next_action'],'authorize_with_settled_consent')
        self.assertFalse(prepared['runtime_authorized'])

    def test_saved_setup_reuse_needs_no_inspection_flag_including_reload(self):
        self.prepare_scope()
        self.call('pantheon_authorize',dict(connection=CONNECTION,deliberate=True,runtime_policy={'mode':'connection'}))
        path=self.server.location(THREAD,CONNECTION);before=path.read_bytes()
        self.server.close();self.server=hosted.HostedServer(self.root);self.addCleanup(self.server.close)
        with patch.object(hosted,'cli_compatibility') as cli:
            for inspect in ({},{'inspect_default_daemon':False}):
                result=self.call('pantheon_prepare',dict(link=LINK,deliberate=True,**inspect))
                self.assertEqual(result['next_action'],'join_with_existing_checks')
                self.assertTrue(result['runtime_authorized']);self.assertFalse(result['native_owner_verified'])
        cli.assert_not_called();self.assertEqual(self.prepare.call_count,1)
        self.assertEqual(path.read_bytes(),before)

    def test_different_room_or_thread_never_inherits_inspection_consent(self):
        self.prepare_scope()
        self.call('pantheon_authorize',dict(connection=CONNECTION,deliberate=True,runtime_policy={'mode':'connection'}))
        with patch.object(hosted,'cli_compatibility',return_value=('/fixture/codex','fixture')),patch.object(hosted,'require_capabilities'):
            for link,thread in [(BASE+'/join#invite='+'b'*43,THREAD),(LINK,OTHER)]:
                result=self.call('pantheon_prepare',dict(link=link,deliberate=True),thread)
                self.assertEqual(result['status'],'inspection_consent_required')
                self.assertFalse(result['runtime_authorized'])
        self.assertEqual(self.prepare.call_count,1)

    def test_inactive_approval_reports_stop_without_reactivation(self):
        self.prepare_scope();path=self.server.location(THREAD,CONNECTION)
        self.call('pantheon_authorize',dict(connection=CONNECTION,deliberate=True,expires_at=time.time()+60))
        original=load_private(path)
        for change in [dict(trial_expires_at=1),dict(allow_runtime_turns=False),dict(allow_daemon_access=False),dict(allow_tool_output=False)]:
            config={**original,**change};save_private(path,config);before=path.read_bytes()
            result=self.call('pantheon_prepare',dict(link=LINK,deliberate=True))
            self.assertEqual(result['next_action'],'stop_inactive_grant')
            self.assertFalse(result['runtime_authorized']);self.assertFalse(result['runtime_enabled'])
            self.assertEqual(result['runtime_options'],[]);self.assertEqual(path.read_bytes(),before)

    def test_package_change_or_admission_revocation_preserves_setup(self):
        self.prepare_scope();path=self.server.location(THREAD,CONNECTION);before=path.read_bytes()
        changed=copy.deepcopy(self.discovery);changed['packages']['codex']['source_revision']='c'*40
        with patch.object(hosted,'fetch_discovery',return_value=changed):
            with self.assertRaisesRegex(Blocked,'Installed package differs'):
                self.call('pantheon_prepare',dict(link=LINK,deliberate=True))
        with patch.object(hosted,'validate_hosted_admission',side_effect=Blocked('admission revoked')):
            with self.assertRaisesRegex(Blocked,'admission revoked'):
                self.call('pantheon_prepare',dict(link=LINK,deliberate=True))
        self.assertEqual(path.read_bytes(),before);self.assertEqual(self.prepare.call_count,1)

    def test_offline_diagnostic_failure_never_requests_or_grants_daemon_access(self):
        with patch.object(hosted,'cli_compatibility',side_effect=Blocked('CLI unavailable')),patch.object(hosted,'require_capabilities') as caps:
            with self.assertRaisesRegex(Blocked,'CLI unavailable'):
                self.call('pantheon_prepare',dict(link=LINK,deliberate=True))
        caps.assert_not_called();self.prepare.assert_not_called()
        self.assertFalse(self.server.location(THREAD,CONNECTION).exists())
