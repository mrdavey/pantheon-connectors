import os
import pathlib
import time
import types
import unittest
from unittest.mock import patch
from pantheon_connectors.codex_wait import wait_for_unread
from pantheon_connectors.native_cli import execute
from pantheon_connectors.worker import Blocked
from pantheon_connectors.environment import get as product_env


class NativeWaitTests(unittest.TestCase):
    def setUp(self):
        self.binding = dict(runtime='codex', surface='native-queue', session='native-owner', profile='fixture', version='fixture')
        self.config = dict(delivery='bounded-active-wait', binding=self.binding, codex_home='/selected/home', allow_runtime_turns=True,
                           allow_active_wait=True, trial_expires_at=time.time()+60)
        self.args = dict(connection='a'*32, seconds=1)
        self.now = 0
        self.requests = []
        self.pending = []
        def request(path):
            self.requests.append(path)
            return self.pending
        self.client = types.SimpleNamespace(request=request)
        self.service = types.SimpleNamespace(member=lambda *a: (None, None, self.client))

    def pause(self, seconds):
        self.now += seconds

    def wait(self):
        return wait_for_unread(self.config, self.binding, self.args, service=self.service,
                               clock=lambda:self.now, pause=self.pause)

    def test_selected_wait_doctor_and_join_never_direct_queue_startup(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'native-owner','CODEX_HOME':'/selected/home'}), patch('pantheon_connectors.codex_queue.QueueCLI.available',side_effect=AssertionError('queue preflight')):
            self.config.update(state_dir='/fixture/state')
            with patch('pantheon_connectors.native_cli.Onboarding'):
                result=execute(self.config,'pantheon_doctor',{})
                self.assertEqual(result['status'],'active_wait_binding_checked')
                self.assertFalse(result['runtime_verified'])
                with patch('pantheon_connectors.native_cli.dispatch',return_value={'connection':'a'*32,'next':'Drain the initial probe.'}):
                    result=execute(self.config,'pantheon_join',{})
                    self.assertIn('bounded-active-wait',result['next'])
                    self.assertIn('does not verify',result['next'])
                    self.assertNotIn('Start codex_watch',result['next'])
                    self.assertNotIn('pair a panel',result['next'])

    def test_active_selection_blocks_every_queue_ui_entry_and_conflicting_grants(self):
        import tempfile
        from pantheon_connectors.codex_queue import QueueCLI, Notices
        from pantheon_connectors.codex_delivery import selected
        with tempfile.TemporaryDirectory() as temp:
            config=dict(self.config,state_dir=str(pathlib.Path(temp).resolve()),binding=dict(self.binding,session='00000000-0000-4000-8000-000000000001'))
            queue=QueueCLI(config,run=lambda *a,**k: self.fail('native command attempted'))
            for action in (queue.available,lambda:queue.send('test')):
                with self.assertRaises(Blocked):action()
            notices=Notices(config,service=self.service,queue=queue)
            for action in (lambda:notices.tick('a'*32),notices.panel_open,lambda:notices.panel_confirm('x'),lambda:notices.panel_take('x'),lambda:notices.panel_result('x','y',True),lambda:notices.wake('a'*32,'y')):
                with self.assertRaises(Blocked):action()
            for changes in ({'delivery':'native-queue'},{'allow_ui_fallback':True},{'allow_tool_output':True}):
                with self.assertRaises(Blocked):selected(dict(config,**changes))
        self.assertEqual(self.requests,[])

    def test_timeout_is_bounded_and_only_reads(self):
        self.assertEqual(self.wait()['status'], 'timeout')
        self.assertEqual(self.now, 1)
        self.assertEqual(set(self.requests), {'/v1/pending'})
        self.assertLessEqual(self.client.timeout, 1)

    def test_arrival_returns_status_without_content_or_ack(self):
        self.pending = [dict(protocol='pyros.v1', binding=self.binding, message={'text':'PRIVATE PEER CONTENT'})]
        result = self.wait()
        self.assertTrue(result['has_unread'])
        self.assertFalse(result['autonomous_idle_wake'])
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(self.requests, ['/v1/pending'])

    def test_scope_expiry_and_withdrawal_stop_without_alternate_delivery(self):
        self.config['allow_active_wait'] = False
        with self.assertRaises(Blocked): self.wait()
        self.assertEqual(self.requests, [])
        self.config['allow_active_wait'] = True
        self.pending = [dict(protocol='pyros.v1', binding=dict(self.binding, session='other'))]
        with self.assertRaises(Blocked): self.wait()
        self.pending = []
        self.service.member = lambda *a: (_ for _ in ()).throw(Blocked('withdrawn'))
        with self.assertRaises(Blocked): self.wait()
        self.service.member = lambda *a: (None, None, self.client)
        self.client.request = lambda _: self.config.update(trial_expires_at=0) or []
        with self.assertRaises(Blocked): self.wait()

    def test_native_entry_checks_actual_thread_and_home_and_never_queue(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'native-owner','CODEX_HOME':'/selected/home'}), \
             patch('pantheon_connectors.codex_wait.Onboarding', return_value=self.service), \
             patch('pantheon_connectors.native_cli.preflight', side_effect=AssertionError('No queue preflight')):
            self.pending = [dict(protocol='pyros.v1', binding=self.binding)]
            self.assertTrue(execute(self.config, 'pantheon_wait_for_unread', self.args)['has_unread'])
            with patch.dict(os.environ, {'CODEX_THREAD_ID':'other'}):
                with self.assertRaises(Blocked): execute(self.config, 'pantheon_wait_for_unread', self.args)
            with patch.dict(os.environ, {'CODEX_HOME':'/other'}):
                with self.assertRaises(Blocked): execute(self.config, 'pantheon_wait_for_unread', self.args)
        for seconds in (0, 21, True, '1'):
            self.args['seconds'] = seconds
            with self.assertRaises(Blocked): self.wait()


@unittest.skipUnless(product_env('HTTP_TEST_URL'), 'requires isolated loopback test server')
class NativeWaitHTTPTests(unittest.TestCase):
    def test_normal_invitation_join_wait_receive_ack_and_withdraw(self):
        import pathlib
        import secrets
        import tempfile
        import threading
        from pantheon_connectors.client import Client, APIError
        from pantheon_connectors.onboarding import Onboarding
        url = product_env('HTTP_TEST_URL')
        secret = secrets.token_urlsafe(32)
        admin = Client(url, secret)
        admin.request('/v1/chats', dict(secret=secret, key=secrets.token_urlsafe(24), title='Native wait fixture',
                      purpose='Synthetic waiting tool validation', hours=4,
                      slots=[dict(id='codex', runtime='codex', required=True)]))
        self.addCleanup(lambda: admin.command('end', key='wait-fixture-cleanup'))
        token = secrets.token_urlsafe(32)
        admin.command('invite', target='codex', secret=token, hours=1)
        with tempfile.TemporaryDirectory() as directory:
            binding = dict(runtime='codex', surface='native-queue', session='fixture', profile='fixture', version='fixture')
            config = dict(delivery='bounded-active-wait', binding=binding, url=url, state_dir=str(pathlib.Path(directory).resolve()/'state'),
                          allow_runtime_turns=True, allow_active_wait=True, trial_expires_at=time.time()+60)
            service = Onboarding(config)
            connection = service.join(url+'/join#invite='+token, binding, deliberate=True, preflight=lambda _:None)['connection']
            def drain():
                result = service.receive(connection, binding)
                ids = [m['untrusted_member_content']['message']['id'] for m in result['messages']]
                if ids: service.command(connection, binding, 'ack', ids=ids, key=secrets.token_urlsafe(20))
                return result
            initial = drain()
            probe = next(m['untrusted_member_content']['message']['text'] for m in initial['messages'] if m['untrusted_member_content']['message']['kind']=='probe')
            service.command(connection, binding, 'ready', text=probe, key='ready')
            drain()
            def publish():
                time.sleep(.2)
                admin.command('context', expected=admin.request('/v1/chat')['chat']['context_version'],
                              text='Test unread event', key='wait-context')
            worker = threading.Thread(target=publish); worker.start()
            result = wait_for_unread(config, binding, dict(connection=connection, seconds=2), service=service)
            worker.join(timeout=2)
            self.assertTrue(result['has_unread'])
            _, _, member = service.member(connection, binding)
            pending = member.request('/v1/pending')
            self.assertEqual(len(pending), 1)
            self.assertFalse(pending[0]['delivery']['ack'])
            self.assertEqual(pending[0]['delivery']['state'], 'pending')
            drain()
            self.assertEqual(member.request('/v1/pending'), [])
            reply = service.command(connection, binding, 'post', text='Synthetic explicit reply', key='reply')
            self.assertIn('id', reply)
            service.command(connection, binding, 'withdraw', key='withdraw')
            with self.assertRaises(APIError):
                wait_for_unread(config, binding, dict(connection=connection, seconds=1), service=service)


if __name__ == '__main__': unittest.main()
