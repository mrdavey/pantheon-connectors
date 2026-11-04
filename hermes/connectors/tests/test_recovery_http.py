"""Real HTTP ownership/recovery tests; provider delivery is explicitly synthetic."""
import concurrent.futures
import os
import pathlib
import secrets
import tempfile
import threading
import unittest
from pantheon_connectors.client import Client, APIError
from pantheon_connectors.state import Journal
from pantheon_connectors.worker import Worker, Blocked, prompt

from pantheon_connectors.environment import get as product_env

@unittest.skipUnless(product_env('HTTP_TEST_URL'), 'requires running loopback Go test server')
class RecoveryHTTP(unittest.TestCase):
    def setUp(self):
        self.url=product_env('HTTP_TEST_URL')
        self.assertTrue(self.url.startswith('http://127.0.0.1:'))
        admin_secret=secrets.token_urlsafe(32)
        self.admin=Client(self.url,admin_secret)
        self.admin.request('/v1/chats',dict(secret=admin_secret,key=secrets.token_urlsafe(16),title='Recovery',purpose='Synthetic test',hours=4,slots=[dict(id='a',runtime='codex',required=True),dict(id='b',runtime='hermes',required=False)]))
        invitation=secrets.token_urlsafe(32)
        self.admin.command('invite',target='a',secret=invitation,hours=1)
        self.binding=dict(runtime='codex',version='synthetic',surface='app-server',profile='test',session='test')
        self.credential=secrets.token_urlsafe(32)
        Client(self.url,'').request('/v1/redeem',dict(invitation=invitation,secret=self.credential,name='a',binding=self.binding,deliberate=True))
        self.client=Client(self.url,self.credential)
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.calls=[]
        class Runtime:
            def idle(inner):return True
            def deliver(inner,envelope):self.calls.append(envelope['message']['id']);return 'synthetic-receipt'
        self.runtime=Runtime()

    def test_explicit_terminal_ack_uses_existing_receipt_after_closure(self):
        import hashlib
        from types import SimpleNamespace
        from pantheon_connectors import terminal_cleanup
        path=pathlib.Path(self.directory.name)/'member.json'
        journal=Journal(path.with_suffix('.sqlite'),self.binding)
        try:
            journal.pin_scope(self.url,hashlib.sha256(self.credential.encode()).hexdigest())
            pending=self.client.request('/v1/pending')[0]
            self.assertEqual(Worker(self.client,journal,self.runtime,self.binding).once(),'received')
            message=pending['message']['id']
            delivery=next(identity for identity, in journal.db.execute("SELECT id FROM deliveries WHERE state='received'"))
            record=journal.record(delivery)
            journal.put(delivery,'received',record['receipt'],message=message,reported=True)
        finally:
            journal.db.close()
        self.admin.command('end',key='close-before-explicit-ack')
        service=SimpleNamespace(member=lambda *args,**kwargs:(path,{},self.client))
        self.assertFalse(self.client.request('/v1/messages/'+message+'/receipts')['a']['ack'])
        terminal_cleanup.ack(service,'member',self.binding,[message],'terminal-explicit')
        self.assertTrue(self.client.request('/v1/messages/'+message+'/receipts')['a']['ack'])
        terminal_cleanup.ack(service,'member',self.binding,[message],'terminal-explicit')
        self.assertEqual(terminal_cleanup.status(service,'member',self.binding)['untrusted_member_content']['chat']['state'],'closed')
        with self.assertRaises(Blocked):
            terminal_cleanup.ack(service,'member',self.binding,['unfetched'],'no')

    def test_legacy_client_cannot_bypass_criteria_after_upgrade(self):
        for entry in self.client.request('/v1/pending'):
            self.client.ack([entry['message']['id']],key='ack-'+entry['message']['id'])
            if entry['message']['kind']=='probe':self.client.command('ready',text=entry['message']['text'])
        view=self.client.request('/v1/chat')
        self.assertEqual(view['completion_protocol'],'pantheon.definition.v1')
        # Exact old-client wire shape: no protocol guard or definition target.
        with self.assertRaises(APIError) as error:
            self.client.request('/v1/commands',dict(op='result',key='legacy-result',epoch=view['chat']['epoch'],config=view['chat']['config'],text='Do not accept'))
        self.assertEqual(error.exception.status,409)
        with self.assertRaises(APIError) as error:
            self.client.request('/v1/commands',dict(op='result',key='wrong-protocol',epoch=view['chat']['epoch'],config=view['chat']['config'],completion_protocol='unknown'))
        self.assertEqual(error.exception.status,400)

    def test_two_workers_same_claim_key_have_one_exclusive_owner(self):
        barrier=threading.Barrier(2);keys=[]
        class Contender(Client):
            def command(inner,op,**args):
                if op=='claim':
                    keys.append(args['key']);barrier.wait(timeout=5)
                return super().command(op,**args)
        def consume(index):
            journal=Journal(pathlib.Path(self.directory.name)/str(index),self.binding)
            try:
                return Worker(Contender(self.url,self.credential),journal,self.runtime,self.binding).once()
            except APIError as error:return error.status
            finally:journal.db.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes=list(pool.map(consume,range(2)))
        self.assertEqual(keys[0],keys[1])
        self.assertCountEqual(outcomes,['received',409])
        self.assertEqual(len(self.calls),1)

    def test_receipt_outbox_recovers_without_second_provider_call(self):
        for response_lost in (False,True):
            with self.subTest(response_lost=response_lost):
                # Each iteration consumes a fresh scoped probe from its own test fixture.
                if response_lost:self.setUp()
                class FlakyReceipt(Client):
                    def command(inner,op,**args):
                        if op=='receipt':
                            if response_lost:super().command(op,**args)
                            raise APIError(0)
                        return super().command(op,**args)
                path=pathlib.Path(self.directory.name)/'journal'
                journal=Journal(path,self.binding)
                worker=Worker(FlakyReceipt(self.url,self.credential),journal,self.runtime,self.binding)
                with self.assertRaises(APIError):worker.once()
                self.assertEqual(len(self.calls),1)
                journal.db.close()
                journal=Journal(path,self.binding)
                recovered=Worker(self.client,journal,self.runtime,self.binding)
                self.assertEqual(recovered.once(),'awaiting_agent_ack')
                self.assertEqual(len(self.calls),1)
                pending=self.client.request('/v1/pending')
                self.assertTrue(pending[0]['delivery']['received'])
                self.assertFalse(pending[0]['delivery']['ack'])
                journal.db.close()

    def test_lost_claim_response_recovers_exact_intent_in_process_and_after_restart(self):
        for restart in (False, True):
            for committed in (False, True):
                with self.subTest(restart=restart, committed=committed):
                    self.setUp()
                    path=pathlib.Path(self.directory.name)/'claim-journal'
                    journal=Journal(path,self.binding)
                    requests=[]; drop=[True]
                    class FlakyClaim(Client):
                        def request(inner,path,payload=None):
                            if path=='/v1/commands' and payload.get('op')=='claim':
                                requests.append(payload.copy())
                                # The exact request is durable before bytes go out.
                                self.assertEqual(journal.record(payload['target'])['claim'],payload)
                                self.assertEqual(journal.state(payload['target']),'claiming')
                                if drop[0]:
                                    drop[0]=False
                                    if committed:super().request(path,payload)
                                    raise APIError(0)
                            return super().request(path,payload)
                    worker=Worker(FlakyClaim(self.url,self.credential),journal,self.runtime,self.binding)
                    with self.assertRaises(APIError):worker.once()
                    self.assertEqual(self.calls,[])
                    if restart:
                        journal.db.close();journal=Journal(path,self.binding)
                        worker=Worker(FlakyClaim(self.url,self.credential),journal,self.runtime,self.binding)
                        self.assertNotEqual(worker.owner,requests[0]['worker'])
                    self.assertEqual(worker.once(),'received')
                    self.assertEqual(requests[0],requests[1])
                    self.assertEqual(len(self.calls),1)
                    self.assertEqual(worker.once(),'awaiting_agent_ack')
                    self.assertEqual(len(self.calls),1)
                    journal.db.close()

    def test_refreshed_brief_and_exact_prompt_fingerprint_after_claim(self):
        import hashlib
        for entry in self.client.request('/v1/pending'):
            self.client.ack([entry['message']['id']],key='ack-'+entry['message']['id'])
            if entry['message']['kind']=='probe':self.client.command('ready',text=entry['message']['text'])
        original=self.client.request('/v1/pending')[0]
        captured=[]
        class Runtime:
            owner_instruction='Private owner constraint'
            def idle(inner):return True
            def deliver(inner,envelope):captured.append(envelope);return 'synthetic-receipt'
        class EditAfterClaim(Client):
            def command(inner,op,**args):
                result=super().command(op,**args)
                if op=='claim':
                    self.admin.command('objective',expected=1,text='Current revised objective')
                    self.admin.command('context',expected=1,text='Current revised context')
                    self.admin.command('extend',hours=1)
                return result
        journal=Journal(pathlib.Path(self.directory.name)/'fresh-journal',self.binding)
        self.assertEqual(Worker(EditAfterClaim(self.url,self.credential),journal,Runtime(),self.binding).once(),'received')
        sent=captured[0]
        self.assertEqual(sent['brief']['objective'],'Current revised objective')
        self.assertEqual(sent['brief']['shared_context'],'Current revised context')
        self.assertNotEqual(sent['brief']['expiry'],original['brief']['expiry'])
        self.assertEqual(journal.record(sent['delivery']['id'])['fingerprint'],hashlib.sha256(prompt(sent,Runtime.owner_instruction).encode()).hexdigest())
        journal.db.close()

    def test_live_shared_journal_owner_blocks_recovery_consumer(self):
        path=pathlib.Path(self.directory.name)/'shared-journal'
        first,second=Journal(path,self.binding),Journal(path,self.binding)
        with first.exclusive():
            with self.assertRaises(Blocked):Worker(self.client,second,self.runtime,self.binding).once()
        self.assertEqual(self.calls,[])
        self.assertEqual(Worker(self.client,second,self.runtime,self.binding).once(),'received')
        self.assertEqual(len(self.calls),1)
        first.db.close();second.db.close()

    def test_prejoin_canaries_are_hidden_on_every_read_endpoint(self):
        import json
        for entry in self.client.request('/v1/pending'):
            self.client.ack([entry['message']['id']],key='ack-'+entry['message']['id'])
            if entry['message']['kind']=='probe':self.client.command('ready',text=entry['message']['text'])
        report=self.client.command('report',target='needs_input',text='hidden-report-canary')
        # Readiness alone is not criteria agreement. Keep this gate explicit while
        # constructing a valid result for the history/privacy regression below.
        with self.assertRaises(APIError) as error:
            self.client.command('result',text='hidden-result-canary')
        self.assertEqual(error.exception.status,409)
        chat=self.client.request('/v1/chat')['chat']
        self.client.command('definition',expected=0,
                            objective_version=chat['objective_version'],
                            context_version=chat['context_version'],
                            text='Return a synthetic result with supporting evidence',
                            evidence='Synthetic fixture only; no external action',source='derived')
        definition=self.client.request('/v1/chat')['chat']['definition']
        self.client.command('confirm',target=definition['id'],expected=definition['version'],
                            text=definition['digest'])
        result=self.client.command('result',target=definition['id'],
                                   text='hidden-result-canary',evidence='hidden-evidence-canary')
        self.assertEqual(self.client.request('/v1/chat')['chat']['result']['evidence'],
                         'hidden-evidence-canary')
        invitation=secrets.token_urlsafe(32)
        self.admin.command('invite',target='b',secret=invitation,hours=1,history=False)
        credential=secrets.token_urlsafe(32)
        binding=dict(runtime='hermes',version='synthetic',surface='sessions-api',profile='b',session='b')
        Client(self.url,'').request('/v1/redeem',dict(invitation=invitation,secret=credential,name='b',binding=binding,deliberate=True))
        late=Client(self.url,credential)
        late_definition=late.request('/v1/chat')['chat']['definition']
        self.assertEqual(late_definition['id'],definition['id'])
        self.assertNotIn('b',late_definition['confirmations'])
        for endpoint in ('/v1/chat','/v1/history','/v1/pending'):
            self.assertNotIn('canary',json.dumps(late.request(endpoint)))
        for message in (report,result):
            for suffix in ('','/receipts'):
                with self.assertRaises(APIError) as error:late.request('/v1/messages/'+message['id']+suffix)
                self.assertEqual(error.exception.status,401)
