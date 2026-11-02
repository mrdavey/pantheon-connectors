import json
import os
import pathlib
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from pantheon_connectors.codex_queue import Notices, QueueCLI
from pantheon_connectors.codex_panel import Server, URI
from pantheon_connectors.native_cli import binding_for
from pantheon_connectors.state import save_private
from pantheon_connectors.worker import Blocked

THREAD = '00000000-0000-4000-8000-000000000001'
CONNECTION = 'a' * 32

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = dict(binding=dict(runtime='codex', surface='native-queue', profile='default', session=THREAD, version='0.162.1'),
            url='http://127.0.0.1:8080', codex_executable='/pinned/codex', codex_home='/chosen/home',
            state_dir=str(pathlib.Path(self.temp.name).resolve()/'state'), max_notices=2,
            allow_runtime_turns=True, allow_ui_fallback=True, trial_expires_at=time.time()+300)
        self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'m1', 'text':'PRIVATE PEER TEXT'})]
        self.calls = []
        self.available = True
        self.failure = None
        outer = self
        class Queue:
            def available(self):
                if isinstance(outer.available, Exception): raise outer.available
                return outer.available
            def send(self, text):
                outer.calls.append(text)
                if outer.failure: raise outer.failure
        class Service:
            def member(self, connection, binding):
                if connection != CONNECTION: raise Blocked('unknown membership')
                return None, None, types.SimpleNamespace(request=lambda _: outer.pending)
        self.queue, self.service = Queue(), Service()
        self.router = self.restart()

    def restart(self):
        return Notices(self.config, service=self.service, queue=self.queue)

    def test_withdraw_remains_scoped_after_expiry_and_queue_failure(self):
        from pantheon_connectors.native_cli import execute
        from pantheon_connectors.onboarding import Onboarding
        path = pathlib.Path(self.config['state_dir']) / (CONNECTION + '.json')
        member = dict(redeemed=True, binding=self.config['binding'],
                      url=self.config['url'], credential='only-this-membership')
        save_private(path, member)
        unrelated = path.with_name('b' * 32 + '.json')
        save_private(unrelated, dict(preserved=True))
        preserved = unrelated.read_bytes()
        calls = []
        def factory(url, credential):
            self.assertEqual(credential, member['credential'])
            return types.SimpleNamespace(command=lambda op, **args: calls.append((op, args)) or {'status':'withdrawn'})
        self.config.update(allow_runtime_turns=False, allow_ui_fallback=False, trial_expires_at=time.time()-1)
        service = Onboarding(self.config, client_factory=factory)
        args = dict(connection=CONNECTION, op='withdraw', key='owner-withdraw')
        with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD, 'CODEX_HOME':'/chosen/home'}), \
                patch('pantheon_connectors.native_cli.Onboarding', return_value=service), \
                patch('pantheon_connectors.native_cli.preflight', side_effect=PermissionError('queue denied')) as preflight:
            self.assertEqual(execute(self.config, 'pantheon_command', args)['status'], 'withdrawn')
            preflight.assert_not_called()
            with self.assertRaises(Blocked):
                execute(self.config, 'pantheon_command', dict(args, connection='b'*32))
            with self.assertRaises(Blocked):
                execute(self.config, 'pantheon_command', dict(args, unexpected=True))
            with self.assertRaisesRegex(Blocked, 'authorization'):
                execute(self.config, 'pantheon_command', dict(args, op='ready'))
            preflight.assert_not_called()
            with patch.dict(os.environ, {'CODEX_THREAD_ID':'wrong'}):
                with self.assertRaises(Blocked): execute(self.config, 'pantheon_command', args)
        self.assertEqual(calls, [('withdraw', {'key':'owner-withdraw'})])
        self.assertEqual(unrelated.read_bytes(), preserved)

    def pair(self):
        token = self.router.panel_open()['token']
        with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD}): self.router.panel_confirm(token)
        return token

    def test_queue_acceptance_never_ack_or_fallback_and_restart_deduplicates(self):
        self.assertEqual(self.router.tick(CONNECTION), 'queue_accepted_not_received')
        self.assertNotIn('PRIVATE PEER TEXT', self.calls[0])
        self.assertEqual(self.restart().tick(CONNECTION), 'idle_or_notice_unresolved')
        token = self.pair()
        self.assertEqual(self.router.panel_take(token)['status'], 'idle')
        self.assertEqual(len(self.calls), 1)

    def test_timeout_and_nonzero_post_invocation_never_fallback(self):
        self.failure = subprocess.TimeoutExpired('codex', 30)
        with self.assertRaises(Blocked): self.router.tick(CONNECTION)
        token = self.pair()
        self.assertEqual(self.router.panel_take(token)['status'], 'idle')
        with self.assertRaises(Blocked): self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls), 1)

    def test_permission_failure_preflight_does_not_fallback(self):
        self.available = PermissionError('denied')
        with self.assertRaises(PermissionError): self.router.tick(CONNECTION)
        self.assertFalse(self.router.path.exists())
        self.assertEqual(self.calls, [])

    def test_pairing_and_one_shot_fallback_after_proven_absence(self):
        self.available = False
        self.assertEqual(self.router.tick(CONNECTION), 'ui_waiting')
        token = self.router.panel_open()['token']
        self.assertEqual(self.router.panel_take(token)['status'], 'awaiting_native_pairing')
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'wrong'}):
            with self.assertRaises(Blocked): self.router.panel_confirm(token)
        with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD}): self.router.panel_confirm(token)
        send = self.router.panel_take(token)
        self.assertEqual(send['status'], 'send_once')
        self.assertNotIn('PRIVATE PEER TEXT', send['text'])
        self.assertEqual(self.restart().panel_take(token)['status'], 'idle')
        self.assertEqual(self.router.panel_result(token, send['id'], True)['status'], 'ui_accepted')
        self.assertEqual(self.router.tick(CONNECTION), 'idle_or_notice_unresolved')
        self.assertEqual(self.calls, [])
        with self.assertRaises(Blocked): self.router.panel_open()

    def test_expiry_revocation_wrong_binding_and_budget(self):
        token = self.pair()
        self.config['allow_runtime_turns'] = False
        with self.assertRaises(Blocked): self.router.panel_take(token)
        self.config['allow_runtime_turns'] = True
        self.router.tick(CONNECTION)
        identity = next(iter(self.router.read()['notices']))
        self.router.wake(CONNECTION, identity)
        self.pending = []
        self.router.wake(CONNECTION, identity, finish=True)
        self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'m2'})]
        self.router.tick(CONNECTION)
        identity = list(self.router.read()['notices'])[-1]
        self.router.wake(CONNECTION, identity)
        self.pending = []
        self.router.wake(CONNECTION, identity, finish=True)
        self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'m3'})]
        with self.assertRaises(Blocked): self.router.tick(CONNECTION)
        self.config['trial_expires_at'] = time.time()-1
        with self.assertRaises(Blocked): self.router.tick(CONNECTION)

    def test_fallback_suppresses_consumed_input_and_rechecks_membership(self):
        self.available = False
        self.router.tick(CONNECTION)
        token = self.pair()
        self.pending = []
        self.assertEqual(self.router.panel_take(token)['status'], 'idle')
        self.assertEqual(next(iter(self.router.read()['notices'].values()))['state'], 'suppressed')

    def test_preflight_rechecks_pending_and_grant_before_reserving_delivery(self):
        for change in ('consumed', 'revoked', 'expired'):
            with self.subTest(change=change):
                self.config['state_dir'] = str(pathlib.Path(self.temp.name).resolve()/change)
                self.calls.clear()
                self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'m1'})]
                self.config['trial_expires_at'] = time.time()+300
                def preflight():
                    if change == 'consumed': self.pending = []
                    elif change == 'revoked': self.router.pending = lambda _: (_ for _ in ()).throw(Blocked('revoked'))
                    else: self.config['trial_expires_at'] = time.time()-1
                    return True
                self.router = self.restart()
                self.router.queue = types.SimpleNamespace(available=preflight, send=self.calls.append)
                if change == 'consumed':
                    self.assertEqual(self.router.tick(CONNECTION),'idle_or_notice_unresolved')
                else:
                    with self.assertRaises(Blocked): self.router.tick(CONNECTION)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.router.path.exists())

    def test_cli_uses_pinned_uuid_home_profile_without_shell_or_resume(self):
        calls=[]
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            output = 'codex-cli 0.162.1' if argv[-1]=='--version' else '--thread --message'
            return types.SimpleNamespace(returncode=0, stdout=output, stderr='')
        cli = QueueCLI(self.config, run)
        self.assertTrue(cli.available())
        cli.send('harmless')
        self.assertEqual(calls[-1][0], ['/pinned/codex','queue','--thread',THREAD,'--message','harmless'])
        self.assertEqual(calls[-1][1]['env']['CODEX_HOME'], '/chosen/home')
        self.assertNotIn('shell', calls[-1][1])
        self.config['binding']['profile']='chosen-profile'
        cli.send('harmless')
        self.assertEqual(calls[-1][0][-2:], ['--profile','chosen-profile'])

    def test_only_missing_executable_or_known_missing_queue_allows_fallback(self):
        def absent(*args, **kwargs): raise FileNotFoundError()
        self.assertFalse(QueueCLI(self.config, absent).available())
        def fail(argv, **kwargs):
            return types.SimpleNamespace(returncode=0 if argv[-1]=='--version' else 1,
                stdout='codex-cli 0.162.1', stderr='permission denied')
        with self.assertRaises(Blocked): QueueCLI(self.config, fail).available()
        def failed_send(*args, **kwargs): return types.SimpleNamespace(returncode=1)
        with self.assertRaises(Blocked): QueueCLI(self.config, failed_send).send('harmless')

    def test_one_wake_covers_new_work_even_after_original_is_acked(self):
        self.router.tick(CONNECTION)
        identity = next(iter(self.router.read()['notices']))
        self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'start'})]
        self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.router.read()['notices'][identity]['remaining_messages'], ['start'])
        # Even an empty server queue is not evidence that native input vanished.
        self.pending = []
        self.restart().tick(CONNECTION)
        self.assertEqual(self.router.read()['notices'][identity]['state'], 'queue_accepted')
        with self.assertRaises(Blocked): self.router.wake(CONNECTION, identity, finish=True)

    def test_active_wake_drains_then_allows_next_genuinely_new_message(self):
        self.router.tick(CONNECTION)
        identity = next(iter(self.router.read()['notices']))
        self.assertEqual(self.router.wake(CONNECTION, identity)['status'], 'draining')
        self.pending.append(dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'start'}))
        self.restart().tick(CONNECTION)
        self.assertEqual(self.router.wake(CONNECTION, identity, finish=True)['status'], 'drain_required')
        self.assertEqual(len(self.calls), 1)
        self.pending = []
        self.assertEqual(self.router.wake(CONNECTION, identity, finish=True)['status'], 'completed')
        self.assertEqual(self.router.wake(CONNECTION, identity, finish=True)['status'], 'completed')
        self.pending = [dict(protocol='pyros.v1', binding=self.config['binding'], message={'id':'new-peer-message'})]
        self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(list(self.router.read()['notices'])[-1], identity)

    def test_concurrent_watchers_reserve_only_one_wake(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.restart().tick(CONNECTION),range(2)))
        self.assertEqual(results.count('queue_accepted_not_received'),1)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.router.read()['notices']),1)

    def test_native_wake_requires_exact_host_and_valid_notice(self):
        from pantheon_connectors.native_cli import execute
        self.router.tick(CONNECTION)
        identity = next(iter(self.router.read()['notices']))
        with patch('pantheon_connectors.codex_queue.Notices', return_value=self.router), patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/chosen/home'}):
            with self.assertRaises(Blocked): execute(self.config, 'pantheon_begin_wake', {'connection':CONNECTION,'wake_id':'unknown'})
            with self.assertRaises(Blocked): execute(self.config, 'pantheon_begin_wake', {'connection':'b'*32,'wake_id':identity})
            with self.assertRaises(Blocked): execute(self.config, 'pantheon_begin_wake', {'connection':CONNECTION,'wake_id':identity,'extra':True})
            with patch.dict(os.environ, {'CODEX_THREAD_ID':'wrong'}):
                with self.assertRaises(Blocked): execute(self.config, 'pantheon_begin_wake', {'connection':CONNECTION,'wake_id':identity})
            self.assertEqual(execute(self.config, 'pantheon_begin_wake', {'connection':CONNECTION,'wake_id':identity})['status'], 'draining')

    def test_queue_id_and_local_timestamps_never_store_arbitrary_output(self):
        native_id = '11111111-1111-4111-8111-111111111111'
        def run(*args, **kwargs): return types.SimpleNamespace(returncode=0,stdout=f'Queued message {native_id} for thread {THREAD}.\n')
        self.assertEqual(QueueCLI(self.config,run).send('notice')['native_queue_id'],native_id)
        self.queue.send=lambda text: QueueCLI(self.config,run).send(text)
        self.config['trial_expires_at']=1534
        with patch('pantheon_connectors.codex_queue.time.time',return_value=1234): self.router.tick(CONNECTION)
        entry=next(iter(self.router.read()['notices'].values()))
        self.assertEqual(entry['native_queue_id'],native_id)
        self.assertEqual(entry['accepted_at'],1234)
        self.assertEqual(entry['attempted_at'],1234)
        for output in ['unknown success PRIVATE CANARY',f'Queued message {native_id} for thread 00000000-0000-4000-8000-000000000002.']:
            run=lambda *a,**k:types.SimpleNamespace(returncode=0,stdout=output)
            if 'PRIVATE' in output:
                result=QueueCLI(self.config,run).send('notice')
                self.assertIsNone(result['native_queue_id'])
                self.assertNotIn('PRIVATE',str(result))
            else:
                with self.assertRaises(Blocked): QueueCLI(self.config,run).send('notice')

    def test_withdraw_fences_only_owned_connection_and_reports_no_cancellation(self):
        self.router.tick(CONNECTION)
        identity=next(iter(self.router.read()['notices']))
        data=self.router.read()
        data['notices']['other']=dict(connection='b'*32,messages=['other'],state='queue_accepted',native_queue_id='user-unrelated')
        save_private(self.router.path,data)
        result=self.router.withdraw(CONNECTION,lambda:{'status':'withdrawn'})
        cleanup=result['wake_cleanup']
        self.assertFalse(cleanup['native_queue_cancelled'])
        self.assertEqual([n['wake_id'] for n in cleanup['owned_unresolved']],[identity])
        self.assertEqual(self.router.read()['notices']['other'],data['notices']['other'])
        with self.assertRaises(Blocked): self.restart().tick(CONNECTION)
        self.assertEqual(len(self.calls),1)

    def test_failed_withdrawal_stays_fenced_and_unsent_ui_is_suppressed(self):
        self.available=False
        self.router.tick(CONNECTION)
        token=self.pair()
        with self.assertRaises(TimeoutError):
            self.router.withdraw(CONNECTION,lambda:(_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(self.router.panel_take(token)['status'],'idle')
        with self.assertRaises(Blocked):self.restart().tick(CONNECTION)
        self.assertEqual(self.calls,[])

    def test_ui_result_after_native_begin_does_not_reopen_wake(self):
        self.available=False
        self.router.tick(CONNECTION)
        token=self.pair(); sent=self.router.panel_take(token)
        self.router.wake(CONNECTION,sent['id'])
        self.pending=[]
        self.router.wake(CONNECTION,sent['id'],finish=True)
        self.router.panel_result(token,sent['id'],True)
        self.assertEqual(self.router.read()['notices'][sent['id']]['state'],'completed')

    def test_native_home_and_thread_must_match(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/chosen/home'}):
            self.assertEqual(binding_for(self.config)['session'], THREAD)
        with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/other/home'}):
            with self.assertRaises(Blocked): binding_for(self.config)

    def test_mcp_panel_has_thread_only_entrypoint_and_no_pair_confirm_tool(self):
        setup = pathlib.Path(self.temp.name)/'setup.json'; save_private(setup,self.config)
        server = Server(setup)
        tools = server.handle('tools/list', {})['tools']
        self.assertEqual(tools[0]['_meta']['openai/ui']['entrypoints'], [{'type':'thread'}])
        self.assertNotIn('pantheon_pair_panel',[t['name'] for t in tools])
        content = server.handle('resources/read', {'uri':URI})['contents'][0]
        self.assertNotIn('/* PANTHEON_PANEL_SCRIPT */',content['text'])
        self.assertEqual(content['_meta']['ui']['csp']['connectDomains'], [])
        with self.assertRaises(Blocked): server.handle('tools/call', {'name':'pantheon_panel_open','arguments':{'thread':'wrong'}})

if __name__=='__main__': unittest.main()

from pantheon_connectors.environment import get as product_env

@unittest.skipUnless(product_env('HTTP_TEST_URL'), 'requires isolated loopback test server')
class QueueHTTPTests(unittest.TestCase):
    def test_expired_native_withdraw_reaches_server_without_queue_or_other_membership_changes(self):
        import secrets
        from pantheon_connectors.client import Client
        from pantheon_connectors.native_cli import execute
        from pantheon_connectors.onboarding import Onboarding
        url = product_env('HTTP_TEST_URL')
        secret = secrets.token_urlsafe(32)
        admin = Client(url, secret)
        admin.request('/v1/chats', dict(secret=secret, key=secrets.token_urlsafe(24),
            title='Synthetic scoped withdrawal', purpose='HTTP regression, no native model', hours=4,
            slots=[dict(id=slot, runtime='codex', required=slot == 'a') for slot in ('a','b')]))
        self.addCleanup(lambda: admin.command('end', key='withdraw-fixture-end'))
        with tempfile.TemporaryDirectory() as temp:
            config = dict(url=url, state_dir=str(pathlib.Path(temp).resolve()/'state'),
                allow_runtime_turns=True, trial_expires_at=time.time()+300,
                codex_home='/fixture', codex_executable='/fixture/absent-codex',
                binding=dict(runtime='codex', surface='native-queue', profile='fixture', version='fixture', session=THREAD))
            service = Onboarding(config)
            handles = []
            for slot in ('a','b'):
                invite = secrets.token_urlsafe(32)
                admin.command('invite', target=slot, secret=invite, hours=1)
                handles.append(service.join(url+'/join#invite='+invite, config['binding'],
                    deliberate=True, preflight=lambda _:None)['connection'])
            before = admin.request('/v1/chat')['chat']['members']
            self.assertFalse(before['a']['revoked'])
            self.assertFalse(before['b']['revoked'])
            config.update(allow_runtime_turns=False, trial_expires_at=time.time()-1)
            args = dict(connection=handles[0], op='withdraw', key='scoped-withdraw')
            with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD, 'CODEX_HOME':'/fixture'}), \
                    patch('pantheon_connectors.native_cli.preflight', side_effect=PermissionError('queue denied')) as preflight:
                execute(config, 'pantheon_command', args)
                preflight.assert_not_called()
            after = admin.request('/v1/chat')['chat']['members']
            self.assertTrue(after['a']['revoked'])
            self.assertFalse(after['b']['revoked'])
            self.assertEqual(after['b']['binding'], before['b']['binding'])
            notices=json.loads((pathlib.Path(config['state_dir'])/'codex-notices.json').read_text())
            self.assertEqual(list(notices['withdrawn_connections']), [handles[0]])

    def test_invitation_queue_notice_then_actual_tool_receipt_and_explicit_ack(self):
        import secrets
        from pantheon_connectors.client import Client
        from pantheon_connectors.onboarding import Onboarding
        url = product_env('HTTP_TEST_URL')
        secret = secrets.token_urlsafe(32)
        admin = Client(url, secret)
        admin.request('/v1/chats', dict(secret=secret,key=secrets.token_urlsafe(24), title='Synthetic queue contract',
            purpose='Protocol fixture, no native model',hours=4,slots=[dict(id='codex',runtime='codex',required=True)]))
        self.addCleanup(lambda: admin.command('end',key='fixture-end'))
        invite = secrets.token_urlsafe(32)
        admin.command('invite',target='codex',secret=invite,hours=1)
        with tempfile.TemporaryDirectory() as temp:
            config=dict(url=url,state_dir=str(pathlib.Path(temp).resolve()/'state'),allow_runtime_turns=True,
                trial_expires_at=time.time()+300,max_notices=2,codex_home='/fixture',codex_executable='/fixture/codex',
                binding=dict(runtime='codex',surface='native-queue',profile='fixture',version='fixture',session=THREAD))
            service=Onboarding(config)
            handle=service.join(url+'/join#invite='+invite,config['binding'],deliberate=True,preflight=lambda _:None)['connection']
            # Codex delivery is explicitly mocked; actual Pantheon HTTP fetch,
            # receipt, ACK, readiness, reply and stale-wake state are exercised.
            import importlib.util
            spec=importlib.util.spec_from_file_location('offline_contract', pathlib.Path(__file__).resolve().parents[2]/'scripts/codex_tool_output_contract.py')
            contract=importlib.util.module_from_spec(spec); spec.loader.exec_module(contract)
            host=contract.ScriptedHost(); driver=contract.CandidateDriver(host)
            calls=[]
            def mock_notification_send(text):
                calls.append(text)
                wake_id=text.split(' and wake_id ',1)[1].split('.',1)[0]
                status=contract.notification(handle,wake_id,bool(router.pending(handle)))
                driver.send(status)  # Scripted host only; no native call.
            router=Notices(config,service=service,queue=types.SimpleNamespace(available=lambda:True,send=mock_notification_send))
            self.assertEqual(router.tick(handle),'queue_accepted_not_received')
            identity=next(iter(router.read()['notices']))
            router.wake(handle,identity)
            _,_,member=service.member(handle,config['binding'])
            pending=member.request('/v1/pending')
            self.assertEqual(pending[0]['delivery']['state'],'pending')
            service.receive(handle,config['binding'])
            pending=member.request('/v1/pending')
            self.assertEqual(pending[0]['delivery']['state'],'received')
            probe=next(e['message'] for e in pending if e['message']['kind']=='probe')
            self.assertNotIn(probe['text'],json.dumps(host.calls))
            self.assertNotIn(probe['text'],calls[0])
            self.assertEqual(len(host.calls),1)
            self.assertEqual(host.calls[0]['input'],[])
            self.assertEqual(host.calls[0]['toolOutput']['name'],'pantheon_pending_notification')
            service.command(handle,config['binding'],'ack',ids=[e['message']['id'] for e in pending],key='fixture-ack')
            service.command(handle,config['binding'],'ready',text=probe['text'],key='fixture-ready')
            self.assertEqual(member.request('/v1/chat')['chat']['state'],'active')
            router.tick(handle)  # The new start event must not create a second wake.
            self.assertEqual(router.wake(handle,identity,finish=True)['status'],'drain_required')
            service.receive(handle,config['binding'])
            remaining=member.request('/v1/pending')
            service.command(handle,config['binding'],'ack',ids=[e['message']['id'] for e in remaining],key='start-ack')
            reply=service.command(handle,config['binding'],'post',text='Synthetic fixture consumed and acknowledged; no real model ran.',key='fixture-reply')
            self.assertTrue(reply['id'])
            self.assertEqual(member.request('/v1/messages/'+reply['id']+'/receipts'),{})
            self.assertEqual(router.wake(handle,identity,finish=True)['status'],'completed')
            router.tick(handle)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(host.calls),1)
            self.assertEqual(member.request('/v1/pending'),[])
            # Real server revocation during CLI preflight must prevent another send.
            admin.command('context', expected=admin.request('/v1/chat')['chat']['context_version'],
                          text='Synthetic unread input before revoke', key='revoke-preflight-source')
            def revoke_during_preflight():
                admin.command('revoke', target='codex', key='revoke-preflight')
                return True
            router.queue=types.SimpleNamespace(available=revoke_during_preflight,send=calls.append)
            from pantheon_connectors.client import APIError
            with self.assertRaises(APIError): router.tick(handle)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(router.read()['notices']),1)

class NativeSetupTests(unittest.TestCase):
    def test_capture_native_identity_is_disabled_and_never_overwrites(self):
        from pantheon_connectors.codex_setup import prepare
        from pantheon_connectors.state import load_private
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp).resolve()/'private'/'setup.json'
            with patch.dict(os.environ, {'CODEX_THREAD_ID':THREAD,'CODEX_HOME':'/chosen/home'}), \
                 patch('pantheon_connectors.codex_setup.shutil.which', return_value='/pinned/codex'), \
                 patch('pantheon_connectors.codex_setup.subprocess.run', return_value=types.SimpleNamespace(stdout='codex-cli 0.162.1')):
                value=prepare(target,'http://127.0.0.1:8080','default')
                self.assertEqual(value['binding']['session'],THREAD)
                saved=load_private(target)
                self.assertFalse(saved['allow_runtime_turns'])
                self.assertFalse(saved['allow_ui_fallback'])
                self.assertEqual(saved['trial_expires_at'],0)
                with self.assertRaises(Blocked): prepare(target,'http://127.0.0.1:8080','different')
                self.assertEqual(load_private(target),saved)
            with patch.dict(os.environ, {'CODEX_THREAD_ID':''}):
                with self.assertRaises(Blocked): prepare(target,'http://127.0.0.1:8080','default')
