"""Audit regressions: synthetic callbacks/temp state, no native host or provider."""
import copy
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pantheon_connectors import codex_hosted, hosted_discovery, codex_output_watch
from pantheon_connectors.client import APIError
from pantheon_connectors.codex_daemon import account_metadata, DefaultDaemonOwner
from pantheon_connectors.codex_audit import Audit, failure
from pantheon_connectors.codex_output import Owner
from pantheon_connectors.native_cli import execute
from pantheon_connectors.onboarding import Onboarding
from pantheon_connectors.read_retry import ReadRetry
from pantheon_connectors.runtime_policy import same_scope
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked
import test_codex_hosted as codex
import test_hermes_hosted_setup as hermes
import test_hermes_notification_contract as notifications


class RecoveryTests(unittest.TestCase):
    def fixture(self, cls=notifications.NotificationBoundaries):
        value = cls(); value.setUp()
        self.addCleanup(value.doCleanups)
        if hasattr(value, 'plugin'):
            self.addCleanup(value.plugin.close)
        return value

    def test_retry_policy_bounds_and_permanent_failures(self):
        for error in (APIError(503), APIError(429), APIError(0, 'network_timeout')):
            retry = ReadRetry()
            self.assertEqual([retry.delay(error) for _ in range(4)], [2, 4, 8, 16])
            with self.assertRaisesRegex(Blocked, 'budget exhausted'): retry.delay(error)
            retry.reset(); self.assertEqual(retry.delay(error), 2)
        for error in (APIError(401), APIError(403), APIError(409), APIError(410), APIError(0, 'tls_verification_failed')):
            with self.assertRaises(APIError): ReadRetry().delay(error)

    def test_hermes_recovers_transient_read_and_never_retries_uncertain_injection(self):
        f = self.fixture(); stop = threading.Event(); f.plugin.receiver_stops[f.handle] = stop
        original = f.client.request; calls = []
        def request(route, payload=None):
            calls.append(route)
            if len(calls) == 1: raise APIError(503)
            return original(route, payload)
        delays = []
        def wait(delay):
            delays.append(delay)
            if len(delays) == 2: stop.set()
        with patch.object(f.client, 'request', side_effect=request), patch.object(stop, 'wait', side_effect=wait):
            f.plugin.run(f.handle, f.binding)
        self.assertEqual(delays, [2, 5]); f.ctx.inject_message.assert_called_once()
        self.assertEqual(f.plugin.outcomes[f.handle], 'scheduled_not_received')
        # A native exception of the same APIError type is uncertain, not a read.
        other = self.fixture(); other.ctx.inject_message.side_effect = APIError(503)
        other.plugin.receiver_stops[other.handle] = threading.Event()
        other.plugin.run(other.handle, other.binding)
        other.ctx.inject_message.assert_called_once()
        self.assertEqual(other.new_plugin().tick(other.handle, other.binding), 'idle_or_notice_unresolved')

    def test_hermes_retry_stops_on_cancel_disable_and_exhaustion(self):
        for action in ('cancel', 'disable', 'exhaust'):
            f = self.fixture(); stop = threading.Event(); f.plugin.receiver_stops[f.handle] = stop
            delays = []
            def wait(delay):
                delays.append(delay)
                if action == 'cancel': stop.set()
                if action == 'disable': save_private(f.setup, dict(f.config, allow_runtime_turns=False))
            with patch.object(f.client, 'request', side_effect=APIError(503)) as request, patch.object(stop, 'wait', side_effect=wait):
                f.plugin.run(f.handle, f.binding)
            self.assertEqual(request.call_count, 5 if action == 'exhaust' else 1)
            self.assertEqual(delays, [2, 4, 8, 16] if action == 'exhaust' else [2])
            f.ctx.inject_message.assert_not_called()

    def test_codex_retries_before_attachment_and_in_loop_then_closes_own_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp)/'setup.json'; save_private(setup, {'delivery':'default-daemon-tool-output'})
            stop = threading.Event(); owner = SimpleNamespace(rpc=SimpleNamespace(pending=queue.Queue()), close=Mock())
            notices = SimpleNamespace(allowed=Mock(), pending=Mock(side_effect=[APIError(503), []]),
                tick=Mock(side_effect=[APIError(429), {'status':'idle'}]),
                fence_closed_idle=Mock(return_value=False))
            delays = []; reports = []
            def wait(delay):
                delays.append(delay)
                if delay == 1: stop.set()
            with patch.object(codex_output_watch, 'OutputNotices', return_value=notices), patch.object(codex_output_watch, 'owner_for', return_value=owner) as attach, patch.object(stop, 'wait', side_effect=wait):
                codex_output_watch.run(setup, 'fixture', stop, report=reports.append)
            self.assertEqual(delays, [2, 4, 1]); attach.assert_called_once(); owner.close.assert_called_once()
            self.assertEqual([v['status'] for v in reports], ['transport_retry', 'transport_retry', 'idle'])

    def test_codex_retry_checks_current_scope_and_exhausts_without_attachment(self):
        for action in ('disable', 'exhaust', 'cancel', 'unauthorized'):
            with tempfile.TemporaryDirectory() as tmp:
                setup = Path(tmp)/'setup.json'; save_private(setup, {'delivery':'default-daemon-tool-output'})
                stop = threading.Event(); delays = []
                def construct(config, unchanged, **_):
                    def allowed():
                        if not unchanged(): raise Blocked('scope changed')
                    return SimpleNamespace(allowed=allowed, pending=Mock(side_effect=APIError(403 if action == 'unauthorized' else 503)))
                def wait(delay):
                    delays.append(delay)
                    if action == 'disable': save_private(setup, {'delivery':'default-daemon-tool-output', 'allow_runtime_turns':False})
                    if action == 'cancel': stop.set()
                with patch.object(codex_output_watch, 'OutputNotices', side_effect=construct), patch.object(codex_output_watch, 'owner_for') as attach, patch.object(stop, 'wait', side_effect=wait):
                    if action == 'cancel': codex_output_watch.run(setup, 'fixture', stop, report=lambda _:None)
                    else:
                        with self.assertRaises((Blocked, APIError)): codex_output_watch.run(setup, 'fixture', stop, report=lambda _:None)
                    attach.assert_not_called()
                self.assertEqual(delays, [] if action == 'unauthorized' else [2,4,8,16] if action == 'exhaust' else [2])

    def test_lost_hermes_receive_result_retries_same_token_after_reload(self):
        f = self.fixture(); f.plugin.tick(f.handle, f.binding)
        token = f.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
        first = f.receive_notice(token)
        f.plugin = f.new_plugin(); self.addCleanup(f.plugin.close)
        again = f.receive_notice(token)
        self.assertEqual(first['messages'], again['messages'])
        self.assertEqual(f.plugin.tick(f.handle, f.binding), 'idle_or_notice_unresolved')
        self.assertNotIn('ack', [op for op, _ in f.client.commands])
        f.service.command(f.handle, f.binding, 'ack', ids=['message-1'], key='ack-original')
        # Server now returns a new delivery, never the ACKed original.
        f.envelope['delivery'] = {'id':'delivery-2','state':'pending'}
        f.envelope['message']['id'] = 'message-2'
        self.assertEqual(f.receive_notice(token)['status'], 'stale_notice_noop')
        self.assertEqual(f.plugin.tick(f.handle, f.binding), 'scheduled_not_received')
        self.assertEqual(f.receive_notice(token)['status'], 'stale_notice_noop')
        new_token = f.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
        self.assertNotEqual(token, new_token)
        self.assertEqual(f.receive_notice(new_token)['messages'][0]['untrusted_member_content']['message']['id'], 'message-2')

    def test_hermes_token_never_fetches_unrelated_new_arrival(self):
        f = self.fixture(); f.plugin.tick(f.handle, f.binding)
        token = f.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
        extra = copy.deepcopy(f.envelope); extra['delivery']['id']='unrelated'; extra['message']['id']='unrelated'
        original = f.client.request
        def request(route, payload=None):
            result = original(route, payload)
            return result+[extra] if route == '/v1/pending' else result
        with patch.object(f.client, 'request', side_effect=request):
            messages = f.receive_notice(token)['messages']
        self.assertEqual([row['untrusted_member_content']['message']['id'] for row in messages], ['message-1'])

    def test_hermes_partial_ack_drains_large_notice_without_another_injection(self):
        f = self.fixture()
        rows = []
        for index in range(25):
            row = copy.deepcopy(f.envelope)
            row['delivery']['id'] = f'delivery-{index}'
            row['message']['id'] = f'message-{index}'
            rows.append(row)
        def command(op, **args):
            before = args.pop('before_send', None)
            if before: before(dict(op=op, **args))
            if op == 'claim':
                row = next(r for r in rows if r['delivery']['id'] == args['target'])
                row['delivery'].update(state='dispatching', worker=args['worker'], attempt='claim-'+args['target'])
                return {'id': row['delivery']['attempt']}
            if op == 'receipt':
                for row in rows:
                    if row['message']['id'] in args['ids']: row['delivery']['state'] = 'received'
            if op == 'ack': rows[:] = [r for r in rows if r['message']['id'] not in args['ids']]
            return {'id':'fixture-command'}
        def request(route, payload=None):
            if route == '/v1/pending': return copy.deepcopy(rows)
            if route == '/v1/commands': return command(**payload)
            raise AssertionError(route)
        with patch.object(f.client, 'request', side_effect=request), patch.object(f.client, 'command', side_effect=command):
            f.plugin.tick(f.handle, f.binding)
            token = f.ctx.inject_message.call_args.args[0].split(' notice_token: ')[1]
            first = f.receive_notice(token)
            self.assertEqual(len(first['messages']), 20); self.assertTrue(first['has_more'])
            self.assertEqual(f.receive_notice(token)['messages'], first['messages'])
            ids = [r['untrusted_member_content']['message']['id'] for r in first['messages']]
            f.service.command(f.handle, f.binding, 'ack', ids=ids[:10], key='partial-ack')
            self.assertEqual(f.plugin.tick(f.handle, f.binding), 'idle_or_notice_unresolved')
            rest = f.receive_notice(token)
            self.assertEqual(len(rest['messages']), 15); self.assertFalse(rest['has_more'])
            ids = [r['untrusted_member_content']['message']['id'] for r in rest['messages']]
            f.service.command(f.handle, f.binding, 'ack', ids=ids, key='remaining-ack')
            self.assertEqual(f.receive_notice(token)['status'], 'stale_notice_noop')
            f.ctx.inject_message.assert_called_once()

    def test_codex_disable_during_pending_or_claim_refresh_stops_new_actions(self):
        for blocked_read in (1, 2):
            f = self.fixture()
            binding = dict(runtime='codex', surface='app-server-tool-output', profile='default', session=codex.THREAD, version='fixture')
            f.config.update(binding=binding, delivery='app-server-tool-output', codex_home=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
            save_private(f.setup, f.config)
            path=f.root/'state'/(f.handle+'.json'); member=load_private(path);member['binding']=binding;save_private(path,member)
            f.envelope['binding']=binding
            original=f.client.request; reads=[]
            def request(route,payload=None):
                result=original(route,payload)
                if route == '/v1/pending':
                    reads.append(route)
                    if len(reads)==blocked_read:save_private(f.setup,dict(f.config,allow_runtime_turns=False))
                return result
            with patch('pantheon_connectors.native_cli.Onboarding',side_effect=lambda config:Onboarding(config,client_factory=lambda *_:f.client)), patch('pantheon_connectors.native_cli.preflight'), patch.object(f.client,'request',side_effect=request):
                with self.assertRaises(Blocked):execute(load_private(f.setup),'pantheon_receive',dict(connection=f.handle),native_thread=binding['session'],setup_file=f.setup)
            self.assertEqual([op for op,_ in f.client.commands], [] if blocked_read==1 else ['claim'])

    def test_codex_same_package_discovery_refresh_preserves_grants_and_journal(self):
        f=self.fixture(codex.HostedCodexTests);f.prepare_scope();path=f.server.location(codex.THREAD,codex.CONNECTION)
        f.call('pantheon_authorize',dict(connection=codex.CONNECTION,deliberate=True,runtime_policy={'mode':'connection'}))
        config=load_private(path);config['hosted_onboarding']['expires_at']=1;save_private(path,config)
        journal=path.parent/'evidence.sqlite';journal.write_bytes(b'preserved')
        with patch.object(codex_hosted,'validate_hosted_admission',side_effect=hosted_discovery.validate_hosted_admission), patch.object(hosted_discovery,'fetch_discovery',return_value=copy.deepcopy(f.discovery)):
            result=f.call('pantheon_prepare',dict(link=codex.LINK,deliberate=True))
        refreshed=load_private(path)
        self.assertEqual(result['next_action'],'join_with_existing_checks')
        self.assertEqual(refreshed,dict(config,hosted_onboarding=f.discovery));self.assertEqual(journal.read_bytes(),b'preserved')
        self.assertTrue(same_scope(refreshed,config))
        self.assertFalse(same_scope(dict(refreshed,allow_runtime_turns=False),config))

    def test_hermes_provider_change_blocks_prepare_authorize_and_foreground(self):
        f=self.fixture(hermes.HostedHermesTests);prepared=f.prepare();setup=Path(prepared['setup_file']);before=setup.read_bytes()
        f.plugin.observe(session_id='chosen',platform='desktop',model='changed-model',provider='fixture-provider')
        self.assertEqual(f.prepare()['status'],'blocked')
        self.assertEqual(f.authorize(prepared['connection'])['status'],'blocked')
        self.assertEqual(setup.read_bytes(),before)
        child=f.plugin.prepared[setup]
        with self.assertRaises(Blocked):child.check_usage(load_private(setup),f.binding)
        f.plugin.observe(session_id='chosen',platform='desktop',model='fixture-model',provider='fixture-provider')
        self.assertEqual(f.authorize(prepared['connection'])['status'],'runtime_authorized')
        f.plugin.observe(session_id='chosen',platform='desktop',provider='different-provider')
        with self.assertRaises(Blocked):child.tick(prepared['connection'],f.binding)

    def test_missing_hermes_provider_stays_disabled_and_account_limit_is_explicit(self):
        f=self.fixture(hermes.HostedHermesTests);f.plugin.observed_usage.clear()
        self.assertEqual(f.prepare()['status'],'blocked');self.assertFalse((f.root/'hosted').exists())
        f.plugin.observe(session_id='chosen',platform='desktop',model='fixture-model',provider='fixture-provider')
        result=f.prepare();self.assertFalse(result['account_verified'])
        self.assertIn('stable account identity',result['account_limitation'])

    def test_codex_account_pin_detects_login_change_without_refresh_or_raw_identity(self):
        response=dict(account=dict(type='chatgpt',email='fixture@example.invalid'))
        pinned=account_metadata(response)
        self.assertNotIn('fixture@example.invalid',json.dumps(pinned));self.assertFalse(pinned['exact_account_verified'])
        owner=object.__new__(DefaultDaemonOwner)
        owner.config=dict(binding=dict(session=codex.THREAD),reviewed_account=pinned)
        def rpc(method,args):
            if method=='account/read':
                self.assertEqual(args,{'refreshToken':False});return response
            return {'thread':dict(model='fixture',modelProvider='fixture')}
        owner.rpc=Mock();owner.rpc.call.side_effect=rpc
        with patch.object(Owner,'check',return_value='idle'):
            self.assertEqual(owner.check(),'idle')
            response['account']['email']='changed@example.invalid'
            with self.assertRaises(Blocked):owner.check()
        self.assertEqual(account_metadata({'account':{'type':'apiKey'}}),{'type':'apiKey','exact_account_verified':False})

    def test_hermes_network_errors_are_actionable_and_sanitized(self):
        f=self.fixture();f.client.request=Mock(side_effect=APIError(0,'tls_verification_failed'))
        result=json.loads(f.plugin.handle('pantheon_receive',dict(connection=f.handle),session_id='chosen'))
        self.assertIn('certificate verification',result['reason']);self.assertNotIn('HTTP 0',result['reason'])
        self.assertNotIn(f.handle,result['reason'])

    def test_codex_retry_audit_distinguishes_transport_from_notice_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Audit(Path(tmp).resolve()/'setup.json')
            audit.emit('receiver_status', status='transport_retry')
            self.assertEqual(json.loads(audit.path.read_text())['status'], 'transport_retry')
        self.assertEqual(failure(Blocked('Read retry budget exhausted'))['reason'], 'read_retry_exhausted')
