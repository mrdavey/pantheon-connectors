import copy
import hashlib
import json
import pathlib
import queue
import tempfile
import unittest
from pantheon_connectors.codex import CodexAdapter
from pantheon_connectors.hermes import HermesAdapter, HermesSessionsAdapter
from pantheon_connectors.state import Journal, save_private, load_private
from pantheon_connectors.worker import Worker, Blocked

def test_brief():
    return dict(version="pyros.joining.v1",state="active",epoch=1,expiry="2026-10-14T00:00:00Z",discovery=False,objective_version=1,context_version=1,purpose="Synthetic",objective="Work with peers",shared_context="Authorized")

class FakeRPC:
    def __init__(self, binding):
        self.binding, self.busy, self.pending, self.calls = binding, False, queue.Queue(), []
    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": self.binding["session"], "status": {"type":"idle"}}}
        return {"turn": {"id": "turn-1"}}

class FakeGateway:
    supports_atomic_pinned_dispatch = True
    def __init__(self, binding): self.selected, self.active, self.calls = binding, False, []
    def binding(self): return self.selected
    def busy(self): return self.active
    def dispatch_selected(self, binding, message, text):
        self.calls.append((binding, message, text))
        return {"session": binding["session"], "accepted": True, "message_id": message}

class FakeHermesHTTP:
    def __init__(self,binding): self.binding,self.active,self.calls=binding,False,[]
    def request(self,path,payload=None):
        self.calls.append((path,payload))
        if path.endswith('/v1/capabilities'):return {"object":"hermes.api_server.capabilities","features":{"session_chat":True}}
        if path.endswith('/health/detailed'):return {"platform":"hermes-agent","version":self.binding["version"],"gateway_busy":self.active}
        if path.endswith('/chat'):return {"object":"hermes.session.chat.completion","session_id":self.binding["session"]}
        return {"session":{"id":self.binding["session"]}}

class FakeClient:
    url, credential = "http://127.0.0.1:8080", "synthetic-scoped-credential"
    def __init__(self, envelope): self.envelope, self.commands = envelope, []
    def request(self, path, payload=None):
        if payload: return self.command(**payload)
        return [copy.deepcopy(self.envelope)]
    def command(self, op, **args):
        before_send = args.pop("before_send", None)
        if before_send: before_send(dict(op=op, epoch=1, config=1, **args))
        self.commands.append((op,args))
        if op == "claim": self.envelope["delivery"].update(state="dispatching", worker=args["worker"], attempt="claim-1")
        elif op == "receipt": self.envelope["delivery"]["state"]="received"
        return {"id":"claim-1"}

class CommonConformance(unittest.TestCase):
    """Same contract suite applied to both runtime adapters; all provider ports here are mocks."""
    def fixtures(self):
        for runtime, surface in [("codex","app-server"),("hermes","native-adapter"),("hermes","sessions-api")]:
            binding = dict(runtime=runtime, surface=surface, profile="owner-profile",session="pinned-session",version="fixture")
            port = FakeRPC(binding) if runtime == "codex" else FakeHermesHTTP(binding) if surface == "sessions-api" else FakeGateway(binding)
            adapter = CodexAdapter(port,binding) if runtime == "codex" else HermesSessionsAdapter(port,binding) if surface == "sessions-api" else HermesAdapter(port,binding)
            envelope = dict(brief=test_brief(),protocol="pyros.v1",chat="chat",binding=binding,delivery=dict(id="member-1",state="pending"),message=dict(id="message-1",kind="message",text="private reasoning must not be copied"))
            yield runtime,binding,port,adapter,envelope

    def test_shared_receipt_contract_no_automatic_ack(self):
        for runtime,binding,port,adapter,envelope in self.fixtures():
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                client=FakeClient(envelope);journal=Journal(pathlib.Path(directory)/"journal",binding)
                worker=Worker(client,journal,adapter,binding)
                self.assertEqual(worker.once(),"received")
                self.assertEqual([x[0] for x in client.commands],["claim","receipt"])
                self.assertEqual(client.commands[-1][1]["ids"],["message-1"])
                self.assertEqual(worker.once(),"awaiting_agent_ack")
                client.envelope["delivery"]["state"]="received"
                self.assertEqual(worker.once(),"awaiting_agent_ack")
                self.assertEqual(len(client.commands),2)
                client.envelope["delivery"]["state"]="pending"
                restarted=Journal(pathlib.Path(directory)/"journal",binding)
                with self.assertRaises(Blocked):Worker(client,restarted,adapter,binding).once()
                changed=dict(binding,session="new")
                with self.assertRaises(ValueError):Journal(pathlib.Path(directory)/"journal",changed)

    def test_busy_and_mismatched_binding_never_dispatch(self):
        for runtime,binding,port,adapter,envelope in self.fixtures():
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                if runtime=="codex":port.busy=True
                else:port.active=True
                client=FakeClient(envelope);worker=Worker(client,Journal(pathlib.Path(directory)/"journal",binding),adapter,binding)
                self.assertEqual(worker.once(),"busy");self.assertEqual(client.commands,[])
                envelope["binding"]=dict(binding,profile="wrong-owner")
                with self.assertRaises(Blocked):worker.once()
                self.assertEqual(client.commands,[])

    def test_refreshed_prompt_matches_journal_for_every_adapter(self):
        for runtime,binding,port,adapter,envelope in self.fixtures():
            with self.subTest(runtime=runtime,surface=binding['surface']), tempfile.TemporaryDirectory() as directory:
                class ChangingClient(FakeClient):
                    def command(inner,op,**args):
                        result=super().command(op,**args)
                        if op=='claim':
                            inner.envelope['brief'].update(objective='New objective',shared_context='New context',expiry='2026-10-15T00:00:00Z',objective_version=2,context_version=2)
                        return result
                client=ChangingClient(envelope);journal=Journal(pathlib.Path(directory)/'journal',binding)
                self.assertEqual(Worker(client,journal,adapter,binding).once(),'received')
                if runtime=='codex':
                    sent=next(params['input'][0]['text'] for method,params in port.calls if method=='turn/start')
                elif binding['surface']=='sessions-api':
                    sent=next(payload['input'] for path,payload in port.calls if path.endswith('/chat'))
                else:sent=port.calls[-1][2]
                parsed=json.loads(sent)
                self.assertEqual(parsed['untrusted_member_content']['objective'],'New objective')
                self.assertEqual(parsed['untrusted_member_content']['shared_context'],'New context')
                self.assertEqual(parsed['server_controls']['expiry'],'2026-10-15T00:00:00Z')
                self.assertEqual(journal.record('member-1')['fingerprint'],hashlib.sha256(sent.encode()).hexdigest())
                journal.db.close()

    def test_crash_after_provider_call_is_not_replayed(self):
        for runtime,binding,port,adapter,envelope in self.fixtures():
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                def crash(_):raise TimeoutError("ambiguous")
                adapter.deliver=crash;client=FakeClient(envelope);journal=Journal(pathlib.Path(directory)/"journal",binding)
                with self.assertRaises(Blocked):Worker(client,journal,adapter,binding).once()
                self.assertEqual(journal.state("member-1"),"uncertain")
                self.assertEqual(client.commands[-1][0],"dispatch_failed")
                self.assertNotIn("ack",[x[0] for x in client.commands])

    def test_private_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/"binding.json";save_private(path,{"credential":"synthetic"})
            self.assertEqual(load_private(path),{"credential":"synthetic"})
            path.chmod(0o644)
            with self.assertRaises(ValueError):load_private(path)

    def test_hermes_unverified_hook_fails_closed(self):
        binding=dict(runtime="hermes",surface="native-adapter")
        with self.assertRaises(Blocked):HermesAdapter(object(),binding)

if __name__=="__main__":unittest.main()

class HermesSessionsTests(unittest.TestCase):
    def test_explicit_profile_session_and_receipt_boundary(self):
        from pantheon_connectors.hermes import HermesSessionsAdapter
        binding=dict(runtime="hermes",surface="sessions-api",profile="owner",session="exact",version="pinned")
        class API:
            def __init__(self):self.calls=[];self.busy=False;self.actual="exact"
            def request(self,path,payload=None):
                self.calls.append((path,payload))
                if path.endswith('/v1/capabilities'):return {"object":"hermes.api_server.capabilities","features":{"session_chat":True}}
                if path.endswith('/health/detailed'):return {"platform":"hermes-agent","version":"pinned","gateway_busy":self.busy}
                if path.endswith('/chat'):return {"object":"hermes.session.chat.completion","session_id":self.actual,"message":{"content":"private output, never mirrored"}}
                return {"session":{"id":self.actual,"is_internal_child":False}}
        api=API();adapter=HermesSessionsAdapter(api,binding)
        envelope=dict(brief=test_brief(),binding=binding,message=dict(id="m",kind="message",text="hello"))
        self.assertEqual(adapter.deliver(envelope),"m")
        self.assertTrue(all(p.startswith('/p/owner/') for p,_ in api.calls))
        self.assertEqual([p for p,data in api.calls if data],['/p/owner/api/sessions/exact/chat'])
        api.busy=True;self.assertFalse(adapter.idle())
        api.actual="replacement"
        with self.assertRaises(Blocked):adapter.idle()

class CodexEventOrdering(unittest.TestCase):
    def test_completion_before_call_returns_does_not_leave_runtime_busy(self):
        import io,json,types
        from pantheon_connectors.codex import AppServer
        rpc=AppServer.__new__(AppServer);waiter=queue.Queue()
        rpc.responses={1:(waiter,'turn/start')};rpc.pending=queue.Queue();rpc.thread='selected';rpc.busy=False
        rpc.process=types.SimpleNamespace(stdout=io.StringIO('\n'.join(json.dumps(e) for e in [
            {'id':1,'result':{'turn':{'id':'turn','status':'inProgress'}}},
            {'method':'turn/completed','params':{'threadId':'selected'}},
            {'method':'turn/started','params':{'threadId':'another-thread'}}])))
        rpc._read()
        self.assertFalse(rpc.busy)
        self.assertEqual(waiter.get_nowait()['result']['turn']['id'],'turn')
