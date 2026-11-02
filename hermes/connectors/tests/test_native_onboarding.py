import copy
import json
import os
import pathlib
import queue
import tempfile
import time
import unittest
from unittest.mock import patch
from pantheon_connectors.codex import CodexAdapter, AttachedAppServer
from pantheon_connectors.hermes_plugin import NativePlugin, NOTICE
from pantheon_connectors.native_cli import binding_for
from pantheon_connectors.onboarding import Onboarding, HERMES_COMMIT, dispatch, invitation
from pantheon_connectors.state import save_private, load_private
from pantheon_connectors.worker import Blocked
from test_conformance import FakeClient, test_brief


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name).resolve()
        self.config = dict(url="http://127.0.0.1:8080", state_dir=str(self.root / "state"),
                           allow_runtime_turns=True, trial_expires_at=time.time() + 300)
        self.binding = dict(runtime="hermes", version="fixture", surface="native-adapter", profile="default", session="native", return_route="hermes:native")
        self.link = self.config["url"] + "/join#invite=" + "a" * 40
        self.posts = []
        outer = self
        class Public:
            def request(self, path, payload=None):
                if path == "/v1/manifest": return dict(protocol="pyros.v1", local_test=True)
                if path == "/v1/preview": return dict(purpose="untrusted")
                if path == "/v1/redeem":
                    outer.posts.append(copy.deepcopy(payload))
                    if len(outer.posts) == 1 and getattr(outer, "lose_redemption", False): raise TimeoutError()
                    return {"id": "member"}
                raise AssertionError(path)
        self.service = Onboarding(self.config, client_factory=lambda *_: Public())

    def join(self):
        return self.service.join(self.link, self.binding, deliberate=True, preflight=lambda _: None)["connection"]

    def test_connect_retries_read_after_committed_redemption_with_exact_intent(self):
        import hashlib
        from pantheon_connectors.broker import connect
        from pantheon_connectors.client import APIError
        from unittest.mock import Mock
        self.config.update(mcp={'contract': 'pantheon.broker.v1'}, hosted_onboarding={})
        identity = hashlib.sha256((self.config['url']+'\0'+'a'*40).encode()).hexdigest()[:32]
        state = dict(previous=True, active=True, connection=identity,
                     lifetime={'expires_at': self.config['trial_expires_at']}, provider={'model': 'unchanged'})
        client = Mock()
        client.request.side_effect = [APIError(503), {'chat': {'id': 'b'*32, 'state': 'waiting'}}]
        with patch.object(self.service, 'preview', return_value={'local_test': True, 'scope': {'mcp_available': True}}), patch(
                'pantheon_connectors.hosted_discovery.validate_hosted_admission'), patch.object(
                self.service, 'member', return_value=(None, {}, client)), patch('pantheon_connectors.broker.connect.time.sleep'):
            value = connect.run(dict(link=self.link, deliberate=True), inspect=lambda: state,
                prepare=lambda _: {}, authorize=Mock(side_effect=AssertionError('No new grant')),
                preview=Mock(), join=lambda: self.service.join(self.link, self.binding,
                    deliberate=True, preflight=lambda _: None))
        self.assertEqual(value['onboarding_status'], 'connected')
        self.assertEqual(value['chat_url'], self.config['url']+'/app?chat='+'b'*32)
        self.assertEqual(value['collaboration_status'], 'connected')
        self.assertIn('end the native turn', value['progress_guidance'])
        self.assertEqual(value['connection'], identity)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.posts[0], self.posts[1])
        self.assertEqual(len(list((self.root/'state').glob('*.json'))), 1)
        self.assertTrue(load_private(self.root/'state'/(identity+'.json'))['redeemed'])

    def test_preview_has_no_membership_or_files(self):
        self.service.preview(self.link)
        self.assertEqual(self.posts, [])
        self.assertFalse(pathlib.Path(self.config["state_dir"]).exists())
        for link in (self.link.replace("127.0.0.1", "localhost"), self.link + "&invite=b", self.link.replace("/join#", "/other#")):
            with self.assertRaises(Blocked): invitation(link, self.config["url"])

    def test_broker_nonpilot_preview_cannot_consume_invitation(self):
        self.config['mcp'] = {'contract': 'pantheon.broker.v1'}
        with self.assertRaisesRegex(Blocked, 'not admitting this invitation'):
            self.join()
        self.assertEqual(self.posts, [])
        self.assertFalse(pathlib.Path(self.config['state_dir']).exists())

    def test_retry_preserves_redemption_and_native_binding(self):
        self.lose_redemption = True
        with self.assertRaises(TimeoutError): self.join()
        handle = self.join()
        self.assertEqual(self.posts[0], self.posts[1])
        self.service.member(handle, self.binding)
        with self.assertRaises(Blocked): self.service.member(handle, dict(self.binding, session="other"))
        with self.assertRaises(Blocked): self.service.join(self.link, dict(self.binding, profile="other"), deliberate=True, preflight=lambda _: None)
        self.assertEqual(len(self.posts), 2)

    def test_trial_expiry_and_schema_fail_closed(self):
        handle = self.join()
        self.config["trial_expires_at"] = time.time() - 1
        with self.assertRaises(Blocked): self.service.member(handle, self.binding)
        for args in ({"link":self.link,"deliberate":"yes"}, {"link":self.link,"deliberate":True,"session_id":"other"}):
            with self.assertRaises(Blocked): dispatch(self.service, "pantheon_join", args, self.binding, lambda _: None, lambda: {})

    def test_omitted_name_requests_generated_name_and_upgrade_preserves_old_payload(self):
        handle = self.join()
        self.assertEqual(self.posts[-1]['name'], '')
        path = self.root/'state'/(handle+'.json')
        saved = load_private(path)
        saved['name'] = 'My agent'
        save_private(path, saved)
        self.join()
        self.assertEqual(self.posts[-1]['name'], 'My agent')

    def test_connector_supplied_name_is_preserved_on_initial_join_and_retry(self):
        self.service.join(self.link, self.binding, deliberate=True, preflight=lambda _:None, name='Muse Pathfinder')
        self.join()
        self.assertEqual([p['name'] for p in self.posts], ['Muse Pathfinder', 'Muse Pathfinder'])

    def test_receive_replays_exact_claim_and_never_acknowledges(self):
        handle = self.join()
        envelope = dict(protocol="pyros.v1", binding=self.binding, chat="chat", brief=test_brief(),
            message=dict(id="m1", kind="message", text="peer content"), delivery=dict(id="d1", state="pending"))
        client = FakeClient(envelope)
        self.service.client_factory = lambda *_: client
        self.assertEqual(len(self.service.receive(handle, self.binding)["messages"]), 1)
        self.assertEqual(len(self.service.receive(handle, self.binding)["messages"]), 1)
        self.assertNotIn("ack", [op for op, _ in client.commands])
        self.assertEqual([op for op, _ in client.commands], ["claim", "receipt", "claim", "receipt"])

    def test_native_notice_is_not_peer_delivery_and_is_not_retried(self):
        handle = self.join()
        envelope = dict(protocol="pyros.v1", binding=self.binding, message=dict(id="m1",text="SECRET PEER CONTENT"), delivery=dict(id="d1",state="pending"))
        client = FakeClient(envelope)
        self.service.client_factory = lambda *_: client
        setup = self.root / "setup.json"
        save_private(setup,self.config)
        class Context:
            profile_name = "default"
            def __init__(self): self.calls=[]
            def inject_message(self, content, **kwargs): self.calls.append((content,kwargs)); return True
        ctx = Context()
        plugin = NativePlugin(ctx, setup, "fixture", HERMES_COMMIT, service_factory=lambda _: self.service, incoming_probe=lambda _: True)
        plugin.observe(session_id="native",platform="tui")
        self.assertEqual(plugin.tick(handle,self.binding),"scheduled_not_received")
        self.assertEqual(plugin.tick(handle,self.binding),"idle_or_notice_unresolved")
        self.assertEqual(len(ctx.calls),1)
        self.assertNotIn("SECRET PEER CONTENT",ctx.calls[0][0])
        self.assertEqual(ctx.calls[0][1],{"session_key":"native"})
        self.assertEqual(client.commands,[])
        restarted = NativePlugin(ctx,setup,"fixture",HERMES_COMMIT,service_factory=lambda _: self.service, incoming_probe=lambda _: True)
        restarted.observe(session_id="native",platform="tui")
        self.assertEqual(restarted.tick(handle,self.binding),"idle_or_notice_unresolved")
        restarted.observe(session_id="native",platform="cli")
        with self.assertRaises(Blocked): restarted.tick(handle,self.binding)

    def test_private_file_rejects_symlink(self):
        original = self.root / "original.json"; save_private(original,{"safe":True})
        link = self.root / "link.json"; link.symlink_to(original)
        with self.assertRaises(OSError): load_private(link)


class SharedCodexTests(unittest.TestCase):
    def test_shared_owner_never_resumes_disk_or_starts_turn_during_preflight(self):
        binding=dict(runtime="codex",surface="app-server",profile="default",session="chosen")
        class RPC:
            shared_owner=True
            pending=queue.Queue()
            busy=False
            def __init__(self): self.calls=[]
            def require_profile(self,profile): self.calls.append(("profile",profile))
            def require_loaded(self,thread): self.calls.append(("loaded",thread))
            def call(self,method,params):
                self.calls.append((method,params))
                return {"thread":{"id":"chosen","status":{"type":"idle"}}}
        rpc=RPC(); adapter=CodexAdapter(rpc,binding)
        self.assertTrue(adapter.idle())
        self.assertNotIn("thread/resume",[method for method,_ in rpc.calls])
        self.assertNotIn("turn/start",[method for method,_ in rpc.calls])
        with patch.dict(os.environ,{"CODEX_THREAD_ID":"other"}):
            with self.assertRaises(Blocked): binding_for({"binding":binding})
        with patch.dict(os.environ,{"CODEX_THREAD_ID":"chosen"}):
            self.assertEqual(binding_for({"binding":binding}),binding)

    def test_missing_socket_never_launches_process(self):
        with patch("pantheon_connectors.codex.subprocess.Popen") as popen:
            with self.assertRaises(Blocked): AttachedAppServer("/nonexistent/pantheon/socket","version","default")
            popen.assert_not_called()

from pantheon_connectors.environment import get as product_env

@unittest.skipUnless(product_env('HTTP_TEST_URL'), "requires loopback Go test server")
class NativeHTTPTests(unittest.TestCase):
    def test_real_link_redemption_receive_explicit_ack_and_ready(self):
        import secrets
        from pantheon_connectors.client import Client
        url=product_env('HTTP_TEST_URL')
        admin_secret=secrets.token_urlsafe(32)
        admin=Client(url,admin_secret)
        admin.request("/v1/chats",dict(secret=admin_secret,key=secrets.token_urlsafe(24),title="Native onboarding fixture",
            purpose="Synthetic tests; no native runtime or model",hours=4,slots=[dict(id="hermes",runtime="hermes",required=True)]))
        token=secrets.token_urlsafe(32)
        admin.command("invite",target="hermes",secret=token,hours=1)
        with tempfile.TemporaryDirectory() as temp:
            config=dict(url=url,state_dir=str(pathlib.Path(temp).resolve()/"state"),allow_runtime_turns=True,trial_expires_at=time.time()+300)
            binding=dict(runtime="hermes",version="synthetic",surface="native-adapter",profile="fixture",session="fixture")
            service=Onboarding(config)
            handle=service.join(url+"/join#invite="+token,binding,deliberate=True,preflight=lambda _: None)["connection"]
            result=service.receive(handle,binding)
            self.assertGreaterEqual(len(result["messages"]),1)
            _,_,client=service.member(handle,binding)
            pending=client.request("/v1/pending")
            self.assertEqual(pending[0]["delivery"]["state"],"received")
            self.assertEqual(len(service.receive(handle,binding)["messages"]),len(result["messages"]))
            message=next(e["message"] for e in pending if e["message"]["kind"]=="probe")
            service.command(handle,binding,"ack",ids=[e["message"]["id"] for e in pending],key="fixture-ack")
            service.command(handle,binding,"ready",text=message["text"],key="fixture-ready")
            self.assertEqual(client.request("/v1/chat")["chat"]["state"],"active")
        admin.command("end",key="fixture-end")

class UnixTransportTests(unittest.TestCase):
    def test_shared_transport_uses_uncompressed_websocket_and_preserves_owner(self):
        import stat
        import types
        class Socket:
            def __init__(self): self.events=queue.Queue(); self.closed=False; self.sent=[]
            def __iter__(self):
                while True:
                    event=self.events.get()
                    if event is None:return
                    yield json.dumps(event)
            def send(self, raw):
                request=json.loads(raw);self.sent.append(request)
                if "id" not in request:return
                if request["method"]=="initialize":result={"userAgent":"codex/fixture OS"}
                elif request["method"]=="config/read":result={"layers":[{"name":{"type":"user","profile":"owner"}}]}
                elif request["method"]=="thread/loaded/list":result={"data":["chosen"]}
                else:raise AssertionError(request["method"])
                self.events.put({"id":request["id"],"result":result})
            def close(self):self.closed=True;self.events.put(None)
        socket=Socket();connect=__import__('unittest.mock',fromlist=['Mock']).Mock(return_value=socket)
        module=types.ModuleType('websockets.sync.client');module.unix_connect=connect
        info=types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o600,st_uid=os.getuid())
        with patch.dict('sys.modules',{'websockets.sync.client':module}), patch('pathlib.Path.lstat',return_value=info), patch('pantheon_connectors.codex.subprocess.Popen') as popen:
            rpc=AttachedAppServer('/owner.sock','fixture','owner')
            rpc.require_loaded('chosen')
            with self.assertRaises(Blocked):rpc.require_profile('different')
            rpc.close()
            popen.assert_not_called()
        self.assertIsNone(connect.call_args.kwargs['compression'])
        self.assertIsNone(connect.call_args.kwargs['proxy'])
        self.assertTrue(socket.closed)
        self.assertNotIn('thread/resume',[x['method'] for x in socket.sent])
