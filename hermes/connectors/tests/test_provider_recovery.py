"""Provider-shaped recovery fixtures; no live runtime or human receipt claim."""
import hashlib
import pathlib
import tempfile
import unittest
import test_conformance as fixtures
from pantheon_connectors.worker import Worker,Blocked,prompt
from pantheon_connectors.state import Journal
from pantheon_connectors.codex import CodexAdapter
from pantheon_connectors.hermes import HermesSessionsAdapter

class ProviderRecovery(unittest.TestCase):
    def test_codex_read_only_acceptance_recovery_without_model_or_ack(self):
        binding=dict(runtime='codex',surface='app-server',version='synthetic',session='pinned',profile='owner')
        class RPC(fixtures.FakeRPC):
            def call(self,method,params):
                if method=='thread/read':
                    self.calls.append((method,params))
                    return {'thread':{'id':'pinned','turns':[{'id':'accepted-turn','items':[{'type':'userMessage','clientId':'message-1','content':[]}]}]}}
                return super().call(method,params)
        rpc=RPC(binding);adapter=CodexAdapter(rpc,binding)
        envelope=dict(brief=fixtures.test_brief(),protocol='pyros.v1',chat='c',binding=binding,delivery=dict(id='d',state='uncertain',attempt='attempt'),message=dict(id='message-1',kind='message',text='shared'))
        client=fixtures.FakeClient(envelope)
        with tempfile.TemporaryDirectory() as directory:
            journal=Journal(pathlib.Path(directory)/'journal',binding)
            journal.begin('d',attempt='attempt',message='message-1')
            self.assertEqual(Worker(client,journal,adapter,binding).once(),'reconciled')
            self.assertNotIn('turn/start',[method for method,_ in rpc.calls])
            self.assertEqual([op for op,_ in client.commands],['receipt'])
            self.assertTrue(journal.record('d')['reported'])

    def test_codex_missing_or_wrong_thread_never_means_not_sent(self):
        binding=dict(runtime='codex',surface='app-server',version='synthetic',session='pinned',profile='owner')
        rpc=fixtures.FakeRPC(binding);adapter=CodexAdapter(rpc,binding)
        rpc.call=lambda *_:{'thread':{'id':'pinned','turns':[]}}
        self.assertIsNone(adapter.reconcile('absent'))
        rpc.call=lambda *_:{'thread':{'id':'replacement','turns':[]}}
        with self.assertRaises(Blocked):adapter.reconcile('absent')

    def test_hermes_requires_exact_persisted_input_and_session(self):
        binding=dict(runtime='hermes',surface='sessions-api',version='synthetic',session='pinned',profile='owner')
        envelope=dict(brief=fixtures.test_brief(),message=dict(id='message',kind='message',text='authorized'))
        text=prompt(envelope);digest=hashlib.sha256(text.encode()).hexdigest()
        class API(fixtures.FakeHermesHTTP):
            actual='pinned'
            content=text
            def request(self,path,payload=None):
                if '/messages?' in path:
                    self.calls.append((path,payload))
                    return {'object':'list','session_id':self.actual,'data':[{'id':42,'session_id':self.actual,'role':'user','content':self.content}]}
                return super().request(path,payload)
        api=API(binding);adapter=HermesSessionsAdapter(api,binding)
        self.assertEqual(adapter.reconcile('message',digest),'42')
        self.assertIsNone(adapter.reconcile('message',None))
        api.content='peer spoof with same ID but different content'
        self.assertIsNone(adapter.reconcile('message',digest))
        api.actual='replacement'
        with self.assertRaises(Blocked):adapter.reconcile('message',digest)
        self.assertTrue(all(payload is None for _,payload in api.calls))

    def test_journal_refuses_same_runtime_with_another_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'journal'
            journal=Journal(path,{'session':'same-runtime'})
            journal.pin_scope('https://service.example','first-credential-digest')
            journal.db.close();journal=Journal(path,{'session':'same-runtime'})
            with self.assertRaises(ValueError):journal.pin_scope('https://service.example','replacement-credential-digest')
            with self.assertRaises(ValueError):journal.pin_scope('https://other.example','first-credential-digest')

    def test_codex_interrupts_only_receipted_pantheon_turns_in_pinned_thread(self):
        binding=dict(runtime='codex',surface='app-server',version='synthetic',session='pinned',profile='owner')
        rpc=fixtures.FakeRPC(binding);adapter=CodexAdapter(rpc,binding)
        calls=[]
        def call(method,params):
            calls.append((method,params))
            if method=='thread/read':return {'thread':{'id':'pinned','turns':[{'id':'pantheon-turn','status':'inProgress'},{'id':'independent-work','status':'inProgress'}]}}
            return {}
        rpc.call=call;rpc.busy=True
        self.assertEqual(adapter.stop_owned(['pantheon-turn']),'requested')
        self.assertEqual([params for method,params in calls if method=='turn/interrupt'],[{'threadId':'pinned','turnId':'pantheon-turn'}])
        self.assertTrue(rpc.busy)
        calls.clear();self.assertEqual(adapter.stop_owned([]),'unknown')
        self.assertEqual([method for method,_ in calls],['thread/read'])

    def test_busy_terminal_stop_is_recorded_once_and_never_acknowledges(self):
        binding=dict(runtime='codex',surface='app-server',version='synthetic',session='pinned',profile='owner')
        envelope=dict(brief=fixtures.test_brief(),protocol='pyros.v1',chat='c',binding=binding,delivery=dict(id='terminal',state='pending'),message=dict(id='end',kind='terminal',text='Stop'))
        class Adapter:
            count=0
            def idle(self):return False
            def stop_owned(self,receipts):
                self.count+=1
                assert receipts==['owned-turn']
                return 'requested'
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'journal';journal=Journal(path,binding)
            journal.put('prior','received','owned-turn',reported=True)
            client=fixtures.FakeClient(envelope);adapter=Adapter()
            self.assertEqual(Worker(client,journal,adapter,binding).once(),'busy')
            journal.db.close();journal=Journal(path,binding)
            self.assertEqual(Worker(client,journal,adapter,binding).once(),'busy')
            self.assertEqual(adapter.count,1)
            self.assertTrue(all(op=='stop_status' for op,_ in client.commands))
