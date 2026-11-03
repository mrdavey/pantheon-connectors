import json
import os
import pathlib
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from pantheon_connectors.codex_output import OutputNotices, SURFACE
from pantheon_connectors.state import save_private, load_private
from pantheon_connectors.worker import Blocked

THREAD='00000000-0000-4000-8000-000000000044'
CONNECTION='a'*32


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=dict(binding=dict(runtime='codex',surface=SURFACE,session=THREAD,profile='default',version='fixture'),
                         url='http://127.0.0.1:8080',state_dir=str(pathlib.Path(self.temp.name).resolve()/'state'),
                         codex_home='/selected/home',app_server_socket='/selected/owner.sock',
                         owner_socket_identity={'inode':1},codex_executable='/selected/codex',
                         allow_tool_output=True,allow_runtime_turns=True,allow_ui_fallback=False,
                         allow_active_wait=False,trial_expires_at=time.time()+120,max_notices=3)
        self.pending=[dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':'m1','text':'PRIVATE PEER'})]
        self.client=types.SimpleNamespace(request=lambda _:self.pending)
        self.service=types.SimpleNamespace(member=lambda *a,**k:(None,None,self.client))
        self.calls=[];self.failure=None;self.before='idle';self.on_check=lambda:None
        def send(output):
            self.calls.append(json.loads(output))
            if self.failure: raise self.failure
            return {'turn':{'id':'shared-turn'}}
        def check():self.on_check();return self.before
        self.owner=types.SimpleNamespace(check=check,send=send)
        self.bridge=self.restart()

    def restart(self):return OutputNotices(self.config,service=self.service,owner=self.owner)
    def lifecycle(self, identity, finish=False):
        with patch.dict(os.environ,{'CODEX_THREAD_ID':THREAD}):return self.restart().wake(CONNECTION,identity,finish=finish)

    def test_acceptance_coalesces_busy_restart_and_requires_explicit_drain(self):
        first=self.bridge.tick(CONNECTION)
        self.assertEqual(first['status'],'accepted_not_received')
        self.assertEqual(set(self.calls[0]),{'connection','wake_id','has_unread'})
        self.assertNotIn('PRIVATE',str(self.calls))
        self.pending.append(dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':'m2'}))
        self.before='active'
        self.assertEqual(self.restart().tick(CONNECTION)['wake_id'],first['wake_id'])
        with self.assertRaises(Blocked):self.lifecycle(first['wake_id'],finish=True)
        self.lifecycle(first['wake_id'])
        self.assertEqual(self.lifecycle(first['wake_id'],finish=True)['status'],'drain_required')
        self.pending=[]
        self.assertEqual(self.lifecycle(first['wake_id'],finish=True),dict(status='completed',owns_turn=False))
        self.assertEqual(self.restart().tick(CONNECTION)['status'],'idle')
        self.assertEqual(len(self.calls),1)

    def test_uncertain_and_crash_fences_survive_restart_without_fallback(self):
        self.failure=TimeoutError()
        with self.assertRaises(Blocked):self.bridge.tick(CONNECTION)
        with self.assertRaises(Blocked):self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls),1)
        data=self.bridge.read();identity=next(iter(data['notices']))
        data['notices'][identity]['state']='attempting';save_private(self.bridge.path,data)
        with self.assertRaises(Blocked):self.restart().tick(CONNECTION)
        # Actual same-native-thread begin is positive arrival evidence, unlike a
        # slow acknowledgment or an empty inbox. It can reconcile without replay.
        self.lifecycle(identity);self.pending=[];self.lifecycle(identity,finish=True)
        self.assertEqual(len(self.calls),1)

    def test_revocation_consumption_expiry_and_stop_during_preflight_do_not_send(self):
        for change in ('consumed','expired','revoked','stopped'):
            with self.subTest(change=change):
                self.config['trial_expires_at']=time.time()+120
                self.pending=[dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':'m1'})]
                self.client.request=lambda _:self.pending
                self.bridge.unchanged=lambda:True
                def alter():
                    if change=='consumed':self.pending=[]
                    if change=='expired':self.config['trial_expires_at']=0
                    if change=='revoked':self.client.request=lambda _:(_ for _ in ()).throw(Blocked('revoked'))
                    if change=='stopped':self.bridge.unchanged=lambda:False
                self.on_check=alter
                if change=='consumed':self.assertEqual(self.bridge.tick(CONNECTION)['status'],'idle')
                else:
                    with self.assertRaises(Blocked):self.bridge.tick(CONNECTION)
                self.assertEqual(self.calls,[])
                self.assertFalse(self.bridge.path.exists())

    def test_exact_mode_native_identity_and_home_fail_closed(self):
        from pantheon_connectors.native_cli import execute
        for field,value in [('allow_tool_output',False),('allow_ui_fallback',True),('allow_active_wait',True)]:
            config=dict(self.config,**{field:value})
            with self.assertRaises(Blocked):OutputNotices(config,service=self.service,owner=self.owner).tick(CONNECTION)
        for surface in ('native-queue','app-server','auto'):
            config=dict(self.config,binding=dict(self.config['binding'],surface=surface))
            with self.assertRaises(Blocked):OutputNotices(config)
        result=self.bridge.tick(CONNECTION)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'wrong','CODEX_HOME':'/selected/home'}):
            with self.assertRaises(Blocked):execute(self.config,'pantheon_begin_wake',dict(connection=CONNECTION,wake_id=result['wake_id']))
        with patch.dict(os.environ,{'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/wrong'}):
            with self.assertRaises(Blocked):execute(self.config,'pantheon_begin_wake',dict(connection=CONNECTION,wake_id=result['wake_id']))
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'wrong'}):
            with self.assertRaises(Blocked):self.bridge.wake(CONNECTION,result['wake_id'])

    def test_failed_withdrawal_fences_even_after_expiry_without_interrupt(self):
        self.bridge.tick(CONNECTION);self.config['allow_runtime_turns']=False
        with self.assertRaises(TimeoutError):self.bridge.withdraw(CONNECTION,lambda:(_ for _ in ()).throw(TimeoutError()))
        self.config['allow_runtime_turns']=True
        with self.assertRaises(Blocked):self.restart().tick(CONNECTION)
        result=self.bridge.withdraw(CONNECTION,lambda:{'id':'withdraw'})
        self.assertFalse(result['wake_cleanup']['native_turn_interrupted'])
        self.assertEqual(len(self.calls),1)

    def test_hosted_withdraw_disables_access_before_remote_write_and_preserves_evidence(self):
        from pantheon_connectors.native_cli import execute
        for failed in (False, True):
            with self.subTest(failed=failed):
                setup=pathlib.Path(self.temp.name)/('failed.json' if failed else 'success.json')
                config=dict(self.config,hosted_consent=True,invitation_connection=CONNECTION,
                    allow_daemon_access=True,
                    hosted_runtime_grant=dict(connection=CONNECTION,expires_at=self.config['trial_expires_at'],max_notices=3))
                save_private(setup,config)
                grant=config['hosted_runtime_grant'].copy()
                def remote(connection, binding, op, **args):
                    saved=load_private(setup)
                    for flag in ('allow_runtime_turns','allow_daemon_access','allow_tool_output'):
                        self.assertIs(saved[flag],False)
                    self.assertEqual(saved['hosted_runtime_grant'],grant)
                    self.assertEqual(saved['trial_expires_at'],config['trial_expires_at'])
                    self.assertEqual((connection,binding,op),(CONNECTION,config['binding'],'withdraw'))
                    if failed:raise TimeoutError('uncertain withdrawal')
                    return {'id':'withdrawal'}
                service=types.SimpleNamespace(member=lambda *a,**k: (None,None,self.client),command=remote)
                args=dict(connection=CONNECTION,op='withdraw',key='withdraw-key')
                with patch.dict(os.environ,{'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/selected/home'}), \
                        patch('pantheon_connectors.native_cli.Onboarding',return_value=service):
                    if failed:
                        with self.assertRaises(TimeoutError):
                            execute(config,'pantheon_command',args,setup_file=setup)
                    else:
                        result=execute(config,'pantheon_command',args,setup_file=setup)
                        self.assertTrue(result['wake_cleanup']['private_runtime_disabled'])
                        self.assertFalse(result['wake_cleanup']['native_turn_interrupted'])
                        before=load_private(setup)
                        retry=execute(before,'pantheon_command',args,setup_file=setup)
                        self.assertTrue(retry['wake_cleanup']['private_runtime_disabled'])
                        self.assertEqual(load_private(setup),before)
                self.assertFalse(load_private(setup)['allow_runtime_turns'])
                self.assertIn(CONNECTION,OutputNotices(config).read()['withdrawn'])

    def test_hosted_cleanup_rejects_wrong_scope_and_does_not_disable_legacy_shared_setup(self):
        from pantheon_connectors.native_cli import fence_hosted_runtime
        setup=pathlib.Path(self.temp.name)/'setup.json'
        save_private(setup,self.config);before=setup.read_bytes()
        self.assertFalse(fence_hosted_runtime(setup,self.config,CONNECTION))
        self.assertEqual(setup.read_bytes(),before)
        config=dict(self.config,hosted_consent=True,invitation_connection=CONNECTION,
                    hosted_runtime_grant={'connection':CONNECTION})
        save_private(setup,config);before=setup.read_bytes()
        with self.assertRaises(Blocked):fence_hosted_runtime(setup,config,'b'*32)
        self.assertEqual(setup.read_bytes(),before)
        with self.assertRaises(Blocked):fence_hosted_runtime(setup,{**config,'url':'https://other.invalid'},CONNECTION)
        self.assertEqual(setup.read_bytes(),before)

    def test_fenced_wake_and_doctor_report_local_state_without_reopening_access(self):
        from pantheon_connectors.native_cli import execute
        from pantheon_connectors.codex_delivery import DEFAULT_DAEMON
        config = dict(self.config, delivery=DEFAULT_DAEMON, allow_daemon_access=True,
                      hosted_consent=True, invitation_connection=CONNECTION,
                      hosted_runtime_grant={'connection': CONNECTION, 'expires_at': self.config['trial_expires_at']})
        bridge = OutputNotices(config, service=self.service, owner=self.owner)
        wake = bridge.tick(CONNECTION)['wake_id']
        bridge.withdraw(CONNECTION, lambda: {'id': 'withdrawn'})
        config.update(allow_runtime_turns=False, allow_daemon_access=False, allow_tool_output=False)
        setup = pathlib.Path(self.temp.name)/'fenced.json'
        save_private(setup, config)
        before_setup, before_journal = setup.read_bytes(), bridge.path.read_bytes()
        with patch.dict(os.environ, CODEX_HOME='/selected/home'), \
                patch('pantheon_connectors.native_cli.preflight', side_effect=AssertionError('daemon access')), \
                patch('pantheon_connectors.onboarding.Onboarding.member', side_effect=AssertionError('network access')):
            doctor = execute(config, 'pantheon_doctor', {}, native_thread=THREAD, setup_file=setup)
            self.assertEqual(doctor['status'], 'local_runtime_inactive')
            self.assertFalse(doctor['daemon_inspected'])
            self.assertEqual(doctor['next_action'], 'stop_inactive_grant')
            for _ in range(2):
                result = execute(config, 'pantheon_finish_wake', dict(connection=CONNECTION, wake_id=wake),
                                 native_thread=THREAD, setup_file=setup)
                self.assertEqual(result['status'], 'connection_fenced')
                self.assertFalse(result['acknowledgement_inferred'])
                self.assertFalse(result['owns_turn'])
                self.assertFalse(result['daemon_inspected'])
            for connection, identity, thread in [('b'*32, wake, THREAD), (CONNECTION, 'unknown', THREAD),
                                                  (CONNECTION, wake, 'wrong')]:
                with self.assertRaises(Blocked):
                    execute(config, 'pantheon_finish_wake', dict(connection=connection, wake_id=identity),
                            native_thread=thread, setup_file=setup)
            # Expiry/disabled flags alone never prove that an existing wake was fenced.
            data = bridge.read(); data['withdrawn'] = []; save_private(bridge.path, data)
            with self.assertRaises(Blocked):
                execute(config, 'pantheon_finish_wake', dict(connection=CONNECTION, wake_id=wake),
                        native_thread=THREAD, setup_file=setup)
            save_private(bridge.path, json.loads(before_journal))
            changed = dict(config, reviewed_provider={'model': 'changed'})
            save_private(setup, changed)
            for name, args in [('pantheon_doctor', {}), ('pantheon_finish_wake', dict(connection=CONNECTION, wake_id=wake))]:
                with self.assertRaises(Blocked):
                    execute(config, name, args, native_thread=THREAD, setup_file=setup)
            save_private(setup, config)
        self.assertEqual(setup.read_bytes(), before_setup)
        self.assertEqual(bridge.path.read_bytes(), before_journal)
        self.assertEqual(len(self.calls), 1)

    def test_fenced_wake_status_refuses_partial_disable_and_changed_journal_scope(self):
        from pantheon_connectors.codex_delivery import DEFAULT_DAEMON
        config = dict(self.config, delivery=DEFAULT_DAEMON, allow_daemon_access=True,
                      hosted_consent=True, invitation_connection=CONNECTION,
                      hosted_runtime_grant={'connection': CONNECTION})
        bridge = OutputNotices(config, service=self.service, owner=self.owner)
        wake = bridge.tick(CONNECTION)['wake_id']
        bridge.withdraw(CONNECTION, lambda: {})
        config.update(allow_runtime_turns=False, allow_daemon_access=False, allow_tool_output=False)
        for field, value in [('allow_daemon_access', True), ('allow_tool_output', True),
                             ('hosted_consent', False), ('invitation_connection', 'b'*32),
                             ('hosted_runtime_grant', {'connection': 'b'*32}), ('url', 'https://changed.invalid')]:
            with self.subTest(field=field), self.assertRaises(Blocked):
                OutputNotices(dict(config, **{field: value})).fenced_wake_status(CONNECTION, wake, native_thread=THREAD)
        with self.assertRaises(Blocked):
            bridge.fenced_wake_status(CONNECTION, wake, native_thread=THREAD,
                                     scope_check=lambda: (_ for _ in ()).throw(Blocked('scope changed')))

    def test_active_default_doctor_still_requires_preflight_and_inactive_arguments_are_validated(self):
        from pantheon_connectors.native_cli import execute
        from pantheon_connectors.codex_delivery import DEFAULT_DAEMON
        config = dict(self.config, delivery=DEFAULT_DAEMON, allow_daemon_access=True)
        with patch.dict(os.environ, CODEX_HOME='/selected/home'), \
                patch('pantheon_connectors.native_cli.preflight', side_effect=Blocked('owner changed')) as preflight:
            with self.assertRaisesRegex(Blocked, 'owner changed'):
                execute(config, 'pantheon_doctor', {}, native_thread=THREAD)
            preflight.assert_called_once()
            config['allow_runtime_turns'] = False
            with self.assertRaisesRegex(Blocked, 'Invalid native tool arguments'):
                execute(config, 'pantheon_doctor', {'unexpected': True}, native_thread=THREAD)
            self.assertEqual(preflight.call_count, 1)

    def test_two_workers_send_once_and_scope_changes_preserve_fence(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.restart().tick(CONNECTION))) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=5)
        self.assertEqual(len(results),2);self.assertEqual(len(self.calls),1)
        for field,value in [('url','http://127.0.0.1:9999'),('owner_socket_identity',{'inode':2}),('codex_home','/other')]:
            config=dict(self.config,**{field:value})
            with self.assertRaises(Blocked):OutputNotices(config,service=self.service,owner=self.owner).tick(CONNECTION)
        self.assertEqual(len(self.calls),1)


class ClosedIdleTests(unittest.TestCase):
    setUp = OutputTests.setUp
    restart = OutputTests.restart
    lifecycle = OutputTests.lifecycle

    def closed_view(self, **member):
        return dict(role='agent', member='mine', chat=dict(state='closed', members={
            'mine': dict(binding=self.config['binding'], revoked=False, **member)}))

    def test_closed_idle_requires_completed_native_drain(self):
        notice=self.bridge.tick(CONNECTION)
        self.pending=[]
        self.client.request=lambda _: self.closed_view()
        self.assertFalse(self.bridge.fence_closed_idle(CONNECTION))
        self.assertNotIn(CONNECTION,self.bridge.read()['withdrawn'])
        self.client.request=lambda _: self.pending
        self.lifecycle(notice['wake_id'])
        self.lifecycle(notice['wake_id'],finish=True)
        self.client.request=lambda path: self.closed_view() if path=='/v1/chat' else self.pending
        self.assertTrue(self.bridge.fence_closed_idle(CONNECTION))
        self.assertEqual(self.bridge.read()['withdrawn'],[CONNECTION])
        with self.assertRaises(Blocked):self.bridge.tick(CONNECTION)
        self.assertEqual(len(self.calls),1)  # No extra closing turn or replay.

    def test_final_message_arriving_after_idle_check_still_gets_delivered(self):
        self.pending=[]
        self.assertEqual(self.bridge.tick(CONNECTION)['status'],'idle')
        def read(path):
            if path=='/v1/chat':
                self.pending=[dict(binding=self.config['binding'],protocol='pyros.v1',
                                   message={'id':'final-result'})]
                return self.closed_view()
            return self.pending
        self.client.request=read
        self.assertFalse(self.bridge.fence_closed_idle(CONNECTION))
        self.assertNotIn(CONNECTION,self.bridge.read()['withdrawn'])
        self.assertEqual(self.bridge.tick(CONNECTION)['status'],'accepted_not_received')
        self.assertEqual(len(self.calls),1)

    def test_idle_empty_queue_is_not_closure_evidence(self):
        self.pending=[]
        self.assertEqual(self.bridge.tick(CONNECTION)['status'],'idle')
        view=self.closed_view();view['chat']['state']='active'
        self.client.request=lambda _:view
        self.assertFalse(self.bridge.fence_closed_idle(CONNECTION))
        self.assertFalse(self.bridge.path.exists())
        def unavailable(_):raise TimeoutError('temporary authority read failure')
        self.client.request=unavailable
        with self.assertRaises(TimeoutError):self.bridge.fence_closed_idle(CONNECTION)
        self.assertFalse(self.bridge.path.exists())

    def test_idle_closure_rechecks_binding_and_scope_before_fencing(self):
        for change in ('binding','revoked','scope'):
            with self.subTest(change=change):
                self.bridge.unchanged=lambda:True
                view=self.closed_view()
                if change=='binding':view['chat']['members']['mine']['binding']={'session':'other'}
                if change=='revoked':view['chat']['members']['mine']['revoked']=True
                def read(_):
                    if change=='scope':self.bridge.unchanged=lambda:False
                    return view
                self.client.request=read
                with self.assertRaises(Blocked):self.bridge.fence_closed_idle(CONNECTION)
                self.assertFalse(self.bridge.path.exists())

    def test_finite_closed_receiver_disables_only_its_private_scope(self):
        from pantheon_connectors.codex_output_watch import run
        config=dict(self.config,hosted_consent=True,invitation_connection=CONNECTION,
                    allow_daemon_access=True,hosted_runtime_grant=dict(
                        connection=CONNECTION,expires_at=self.config['trial_expires_at']))
        setup=pathlib.Path(self.temp.name)/'setup.json';save_private(setup,config)
        other=pathlib.Path(self.temp.name)/'other.json';save_private(other,config)
        self.client.request=lambda path: self.closed_view() if path=='/v1/chat' else []
        bridge=OutputNotices(config,service=self.service,owner=self.owner)
        closed=[];reports=[]
        owner=types.SimpleNamespace(rpc=types.SimpleNamespace(pending=types.SimpleNamespace(empty=lambda:True)),
                                   close=lambda:closed.append(True))
        with patch('pantheon_connectors.codex_output_watch.Owner',return_value=owner), \
                patch('pantheon_connectors.codex_output_watch.OutputNotices',return_value=bridge):
            run(setup,CONNECTION,threading.Event(),report=reports.append)
        saved=load_private(setup)
        for flag in ('allow_runtime_turns','allow_daemon_access','allow_tool_output'):
            self.assertIs(saved[flag],False)
        self.assertEqual(saved['hosted_runtime_grant'],config['hosted_runtime_grant'])
        self.assertEqual(saved['trial_expires_at'],config['trial_expires_at'])
        self.assertEqual(load_private(other),config)
        self.assertEqual(closed,[True]);self.assertEqual(self.calls,[])
        self.assertEqual(reports,[dict(status='closed',private_runtime_disabled=True)])

    def test_idle_closure_reads_are_throttled(self):
        from pantheon_connectors.codex_output_watch import run
        setup=pathlib.Path(self.temp.name)/'setup.json';save_private(setup,self.config)
        clock=[0];checks=[]
        stop=types.SimpleNamespace(is_set=lambda:clock[0]>=7,
                                   wait=lambda delay:clock.__setitem__(0,clock[0]+1))
        notices=types.SimpleNamespace(allowed=lambda:None,tick=lambda _:dict(status='idle'),
                                     fence_closed_idle=lambda _:checks.append(clock[0]) or False)
        owner=types.SimpleNamespace(rpc=types.SimpleNamespace(pending=types.SimpleNamespace(empty=lambda:True)),
                                   close=lambda:None)
        with patch('pantheon_connectors.codex_output_watch.Owner',return_value=owner), \
                patch('pantheon_connectors.codex_output_watch.OutputNotices',return_value=notices), \
                patch('pantheon_connectors.codex_output_watch.time.monotonic',side_effect=lambda:clock[0]):
            run(setup,CONNECTION,stop,report=lambda _:None)
        self.assertEqual(checks,[0,5])
        self.assertEqual(load_private(setup),self.config)


class OutputOwnerTests(unittest.TestCase):
    def test_socket_replacement_and_home_provenance_are_rejected(self):
        from pantheon_connectors.codex_output import Owner
        config=dict(binding=dict(runtime='codex',surface=SURFACE,session=THREAD,profile='default',version='fixture'),
                    app_server_socket='/selected/owner.sock',owner_socket_identity={'inode':1},codex_home='/selected/home',codex_executable='/bin/codex')
        rpc=types.SimpleNamespace(thread=None,require_profile=lambda _:None,require_loaded=lambda _:None,close=lambda:None)
        def call(method,params):
            if method=='config/read':return {'layers':[{'name':{'type':'user','file':'/selected/home/config.toml'}}]}
            return {'thread':{'id':THREAD,'status':{'type':'active'}}}
        rpc.call=call
        with patch('pantheon_connectors.codex_output.socket_identity',return_value={'inode':1}) as identity, \
             patch('pantheon_connectors.codex_output.AttachedAppServer',return_value=rpc):
            owner=Owner(config)
            identity.return_value={'inode':2}
            with self.assertRaises(Blocked):owner.check()
            identity.return_value={'inode':1}
            owner.config=dict(config,codex_home='/other/home')
            with self.assertRaises(Blocked):owner.check()
            self.assertEqual(rpc.thread,THREAD)

    def test_supervisor_closes_only_client_after_stop(self):
        from pantheon_connectors.codex_output_watch import run
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp).resolve()/'setup.json';save_private(path,{'fixture':True})
            stop=threading.Event();closed=[]
            owner=types.SimpleNamespace(rpc=types.SimpleNamespace(pending=types.SimpleNamespace(empty=lambda:True)),close=lambda:closed.append(True))
            notices=types.SimpleNamespace(allowed=lambda:None,tick=lambda _:stop.set() or {'status':'idle'})
            with patch('pantheon_connectors.codex_output_watch.Owner',return_value=owner), \
                 patch('pantheon_connectors.codex_output_watch.OutputNotices',return_value=notices):
                run(path,CONNECTION,stop)
            self.assertEqual(closed,[True])

    def test_setup_mode_is_explicit_disabled_and_never_overwrites(self):
        from pantheon_connectors.codex_setup import prepare
        from pantheon_connectors.state import load_private
        owner=types.SimpleNamespace(close=lambda:None)
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp).resolve();socket=root/'listener.sock';socket.touch()
            path=root/'private'/'setup.json'
            with patch.dict(os.environ,{'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/selected/home'}), \
                 patch('pantheon_connectors.codex_setup.shutil.which',return_value='/bin/codex'), \
                 patch('pantheon_connectors.codex_setup.subprocess.run',return_value=types.SimpleNamespace(stdout='codex-cli fixture')), \
                 patch('pantheon_connectors.codex_output.socket_identity',return_value={'inode':1}), \
                 patch('pantheon_connectors.codex_output.Owner',return_value=owner):
                for mode,sock in [('auto',str(socket)),('native-queue',str(socket)),(SURFACE,None)]:
                    with self.assertRaises(Blocked):prepare(path,'http://127.0.0.1:8080','default',delivery=mode,owner_socket=sock)
                result=prepare(path,'http://127.0.0.1:8080','default',delivery=SURFACE,owner_socket=str(socket))
                self.assertEqual(result['status'],'prepared_disabled')
                config=load_private(path)
                self.assertFalse(config['allow_tool_output']);self.assertFalse(config['allow_runtime_turns'])
                self.assertEqual(config['binding']['session'],THREAD)
                self.assertEqual(config['owner_socket_identity'],{'inode':1})
                with self.assertRaises(Blocked):prepare(path,'http://127.0.0.1:8080','default',delivery=SURFACE,owner_socket=str(socket))


class DefaultDaemonOutputTests(OutputTests):
    """Run the same durable replay/revocation suite under the new explicit mode."""
    def setUp(self):
        super().setUp()
        self.config.update(delivery='default-daemon-tool-output',allow_daemon_access=True,
                           daemon_endpoint={'fixture':'pinned'})
        self.bridge=self.restart()

    def test_reviewed_provider_cannot_change_under_existing_journal(self):
        self.bridge.tick(CONNECTION)
        self.config['reviewed_provider'] = {'model':'another', 'model_provider':'another'}
        with self.assertRaises(Blocked): self.restart().read()

    def test_audit_write_failure_prevents_delivery_and_fences_attempt(self):
        audit=types.SimpleNamespace(emit=lambda *a, **k: (_ for _ in ()).throw(Blocked('audit unavailable')))
        self.bridge.audit=audit
        with self.assertRaises(Blocked):self.bridge.tick(CONNECTION)
        self.assertEqual(self.calls, [])
        self.assertEqual(next(iter(self.bridge.read()['notices'].values()))['state'], 'uncertain')

class ConnectionRuntimeTests(unittest.TestCase):
    setUp = OutputTests.setUp
    restart = OutputTests.restart
    def test_closed_or_revoked_connection_never_schedules(self):
        self.config.update(runtime_policy={'mode':'connection'},runtime_started_at=time.time()-86400,connection_runtime_supported=True,trial_expires_at=0)
        view={'member':'agent','chat':{'state':'active','members':{'agent':{'revoked':False,'binding':self.config['binding']}}}}
        self.client.request=lambda path:view if path=='/v1/chat' else self.pending
        self.bridge=self.restart()
        self.assertEqual(self.bridge.tick(CONNECTION)['status'],'accepted_not_received')
        for state,revoked in [('closed',False),('active',True)]:
            view['chat']['state']=state;view['chat']['members']['agent']['revoked']=revoked
            with self.assertRaises(Blocked):self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls),1)
    def test_runtime_policy_is_pinned_in_delivery_journal(self):
        self.bridge.tick(CONNECTION)
        self.config.update(runtime_policy={'mode':'connection'},runtime_started_at=time.time(),connection_runtime_supported=True,trial_expires_at=0)
        with self.assertRaises(Blocked):self.restart().read()

    def test_close_during_owner_check_prevents_new_delivery(self):
        self.config.update(runtime_policy={'mode':'connection'},runtime_started_at=time.time(),connection_runtime_supported=True,trial_expires_at=0)
        view={'member':'agent','chat':{'state':'active','members':{'agent':{'revoked':False,'binding':self.config['binding']}}}}
        self.client.request=lambda path:view if path=='/v1/chat' else self.pending
        self.on_check=lambda:view['chat'].update(state='closed')
        with self.assertRaises(Blocked):self.restart().tick(CONNECTION)
        self.assertEqual(self.calls,[])
