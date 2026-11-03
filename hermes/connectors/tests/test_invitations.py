import pathlib
import tempfile
import unittest
from pantheon_connectors.invitations import invite
from pantheon_connectors.state import load_private

class Invitations(unittest.TestCase):
    def test_lost_response_reuses_exact_saved_scope_and_secret(self):
        class Client:
            url,credential='http://127.0.0.1:8080','synthetic'
            calls=[]
            def request(self,path,payload=None):
                if path=='/v1/chat':return {'chat':{'epoch':1,'config':2}}
                self.calls.append(dict(payload))
                if len(self.calls)==1:raise TimeoutError('response lost after simulated commit')
                return {'id':'member'}
        with tempfile.TemporaryDirectory() as directory:
            file=pathlib.Path(directory)/'binding.json';client=Client()
            with self.assertRaises(TimeoutError):invite(client,file,'same-key','member')
            result=invite(client,file,'same-key','member')
            self.assertEqual(client.calls[0],client.calls[1])
            self.assertTrue(result['link'].endswith(client.calls[0]['secret']))
            self.assertIsNotNone(load_private(file.with_suffix('.invites.json')))
            with self.assertRaises(ValueError):invite(client,file,'same-key','other-member')
            client.credential='another-membership'
            with self.assertRaises(ValueError):invite(client,file,'new-key','member')
