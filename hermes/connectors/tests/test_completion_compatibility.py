"""Upgrade and rollback behavior never silently uses legacy completion semantics."""
import copy
import unittest
from unittest.mock import Mock
from pantheon_connectors.client import Client, APIError, COMPLETION_PROTOCOL

class CompletionCompatibility(unittest.TestCase):
    def client(self, protocol):
        client=Client('http://127.0.0.1:8080','existing-credential')
        view={'chat':{'epoch':2,'config':3}}
        if protocol is not None: view['completion_protocol']=protocol
        client.request=Mock(side_effect=[view,{'status':'ok'}])
        return client

    def test_legacy_unknown_and_missing_protocol_pause_before_mutation(self):
        for protocol in (None,'legacy','pantheon.definition.v2'):
            for op in ('definition','result','confirm'):
                with self.subTest(protocol=protocol,op=op):
                    client=self.client(protocol);before=Mock()
                    with self.assertRaises(APIError) as caught:
                        client.command(op,key='same-key',before_send=before)
                    self.assertEqual(caught.exception.reason,'completion_protocol_unavailable')
                    self.assertIn('Do not downgrade',caught.exception.guidance())
                    client.request.assert_called_once_with('/v1/chat');before.assert_not_called()
                    self.assertEqual(client.credential,'existing-credential')

    def test_new_server_uses_wire_guard_and_preserves_intent(self):
        client=self.client(COMPLETION_PROTOCOL);before=Mock()
        fields=dict(target='definition-1',evidence='criterion evidence',objective_version=2,context_version=4)
        client.command('result',key='same-key',before_send=before,**fields)
        payload=client.request.call_args.args[1]
        self.assertEqual(payload,dict(op='result',key='same-key',epoch=2,config=3,completion_protocol=COMPLETION_PROTOCOL,**fields))
        before.assert_called_once_with(payload)

    def test_legacy_read_receipt_post_and_withdraw_remain_available(self):
        for op in ('ack','post','receipt','withdraw','report'):
            client=self.client(None);client.command(op,key='same-key')
            self.assertNotIn('completion_protocol',client.request.call_args.args[1])

    def test_no_cached_protocol_after_rollback_or_restore(self):
        client=self.client(COMPLETION_PROTOCOL)
        modern={'completion_protocol':COMPLETION_PROTOCOL,'chat':{'epoch':2,'config':3}}
        legacy=copy.deepcopy(modern);legacy.pop('completion_protocol')
        client.request=Mock(side_effect=[modern,{},legacy,modern,{}])
        client.command('definition',key='first')
        with self.assertRaises(APIError): client.command('result',key='second')
        client.command('result',key='second')
        posts=[c for c in client.request.call_args_list if c.args[0]=='/v1/commands']
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[-1].args[1]['key'],'second')


# Optional genuine legacy Go server, never a production URL.
import os
import secrets
@unittest.skipUnless(os.getenv('PANTHEON_LEGACY_HTTP_TEST_URL'),'requires isolated legacy Go server')
class LegacyServerCompatibility(unittest.TestCase):
    def test_legacy_server_rejects_wire_guard_even_when_view_races_rollback(self):
        url=os.environ['PANTHEON_LEGACY_HTTP_TEST_URL']
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        secret=secrets.token_urlsafe(32);client=Client(url,secret)
        client.request('/v1/chats',dict(secret=secret,key=secrets.token_urlsafe(16),title='Compatibility',purpose='Synthetic',hours=4,slots=[dict(id='a',runtime='codex',required=True)]))
        view=client.request('/v1/chat');self.assertNotIn('completion_protocol',view)
        for op in ('definition','result','confirm'):
            with self.assertRaises(APIError) as caught:client.command(op,key='same-key')
            self.assertEqual(caught.exception.reason,'completion_protocol_unavailable')
            # New view obtained before a rollback, POST arriving at legacy runtime.
            with self.assertRaises(APIError) as caught:
                client.request('/v1/commands',dict(op=op,key='guarded-'+op,epoch=view['chat']['epoch'],config=view['chat']['config'],completion_protocol=COMPLETION_PROTOCOL))
            self.assertEqual(caught.exception.status,400)
        self.assertEqual(client.request('/v1/chat')['chat'],view['chat'])
