"""Synthetic three-member HTTP integration. Runtime ports are mocks, never real-agent proof."""
import os
import pathlib
import secrets
import tempfile
import unittest
from pantheon_connectors.client import Client
from pantheon_connectors.state import Journal
from pantheon_connectors.worker import Worker

from pantheon_connectors.environment import get as product_env

@unittest.skipUnless(product_env('HTTP_TEST_URL'), 'requires running loopback Go test server')
class HTTPIntegration(unittest.TestCase):
    def test_three_synthetic_members_receipts_reconnect_and_fanout(self):
        url=product_env('HTTP_TEST_URL')
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        admin_secret=secrets.token_urlsafe(32);admin=Client(url,admin_secret)
        admin.request('/v1/chats',dict(secret=admin_secret,key=secrets.token_urlsafe(16),title='Synthetic connector integration',purpose='No real model calls',hours=4,slots=[dict(id='a',runtime='codex',required=True),dict(id='b',runtime='hermes',required=True),dict(id='c',runtime='codex',required=False)]))
        clients={};bindings={}
        for member in ('a','b','c'):
            invitation=secrets.token_urlsafe(32)
            admin.command('invite',target=member,secret=invitation,hours=1)
            runtime='hermes' if member=='b' else 'codex'
            binding=dict(runtime=runtime,version='synthetic',surface='native-adapter' if runtime=='hermes' else 'app-server',profile='test-'+member,session='test-'+member)
            credential=secrets.token_urlsafe(32)
            Client(url,'').request('/v1/redeem',dict(invitation=invitation,secret=credential,name=member,binding=binding,deliberate=True))
            clients[member]=Client(url,credential);bindings[member]=binding
        class Runtime:
            def __init__(self):self.ids=[]
            def idle(self):return True
            def deliver(self,envelope):self.ids.append(envelope['message']['id']);return envelope['message']['id']
        with tempfile.TemporaryDirectory() as directory:
            workers={};runtimes={}
            for member in clients:
                runtime=Runtime();runtimes[member]=runtime
                workers[member]=Worker(clients[member],Journal(pathlib.Path(directory)/member,bindings[member]),runtime,bindings[member])
            def drain(member):
                client=clients[member]
                while pending:=client.request('/v1/pending'):
                    envelope=pending[0]
                    self.assertEqual(workers[member].once(),'received')
                    self.assertEqual(workers[member].once(),'awaiting_agent_ack')
                    self.assertFalse(client.request('/v1/pending')[0]['delivery']['ack'])
                    # Explicit synthetic tool invocation, distinct from harness acceptance.
                    client.ack([envelope['message']['id']],key='test-ack-'+envelope['message']['id'])
                    if envelope['message']['kind']=='probe':
                        client.command('ready',text=envelope['message']['text'])
            for member in clients:drain(member)
            drain('a')
            self.assertEqual(admin.request('/v1/chat')['chat']['state'],'active')
            posted=clients['a'].command('post',key='one-post',text='Authorized synthetic contribution')
            for member in ('b','c'):
                workers[member]=Worker(clients[member],Journal(pathlib.Path(directory)/member,bindings[member]),runtimes[member],bindings[member])
                drain(member)
                self.assertEqual(runtimes[member].ids.count(posted['id']),1)
            self.assertNotIn(posted['id'],[x['message']['id'] for x in clients['a'].request('/v1/pending')])
            receipts=clients['a'].request('/v1/messages/'+posted['id']+'/receipts')
            self.assertEqual(set(receipts),{'b','c'})
            self.assertTrue(all(v['received'] and v['ack'] for v in receipts.values()))
            terminal=admin.command('end')['id']
            for member in clients:
                drain(member)
                self.assertEqual(runtimes[member].ids.count(terminal),1)
                self.assertEqual(workers[member].once(),'idle')
