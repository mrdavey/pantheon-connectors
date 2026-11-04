"""Boundary tests for the opt-in bridge; no live owner or provider calls."""
import contextlib
import multiprocessing
import os
import pathlib
import socket
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import test_codex_output as fixture
from pantheon_connectors.codex_output import OutputNotices, Owner, socket_identity
from pantheon_connectors.onboarding import private_lock
from pantheon_connectors.worker import Blocked


def competing_worker(config, start, results, calls):
    service = types.SimpleNamespace(member=lambda *args: (None, None, types.SimpleNamespace(
        request=lambda _: [dict(protocol='pyros.v1', binding=config['binding'], message={'id':'m1'})])))
    def send(output):
        with open(calls, 'a') as stream: stream.write(output + '\n')
        return {'turn': {'id': 'fixture-turn'}}
    owner = types.SimpleNamespace(check=lambda: 'idle', send=send)
    start.wait(5)
    try: results.put(OutputNotices(config, service=service, owner=owner).tick(fixture.CONNECTION)['status'])
    except Exception as error: results.put(type(error).__name__)


class AdversarialTests(unittest.TestCase):
    setUp = fixture.OutputTests.setUp
    restart = fixture.OutputTests.restart
    lifecycle = fixture.OutputTests.lifecycle

    def test_socket_replacement_after_preflight_stops_final_send(self):
        owner = Owner.__new__(Owner)
        owner.config = self.config
        calls = []
        owner.rpc = types.SimpleNamespace(call=lambda *args: calls.append(args) or {'turn':{'id':'wrong'}})
        owner.check = lambda: 'idle'
        self.bridge.owner = owner
        with patch('pantheon_connectors.codex_output.socket_identity', return_value={'inode':1}) as identity:
            reads = []
            def pending(_):
                reads.append(True)
                if len(reads) == 2: identity.return_value = {'inode':2}
                return self.pending
            self.client.request = pending
            with self.assertRaises(Blocked): self.bridge.tick(fixture.CONNECTION)
        self.assertEqual(calls, [])
        with self.assertRaises(Blocked): self.restart().tick(fixture.CONNECTION)

    def test_scope_rechecked_immediately_after_acquiring_lock(self):
        reads = []
        self.client.request = lambda _: reads.append(True) or self.pending
        @contextlib.contextmanager
        def acquired(*args, **kwargs):
            self.bridge.unchanged = lambda: False
            yield
        with patch('pantheon_connectors.codex_output.notice_lock', acquired):
            with self.assertRaises(Blocked): self.bridge.tick(fixture.CONNECTION)
        self.assertEqual(reads, [])
        self.assertEqual(self.calls, [])

    def test_stopped_contender_exits_while_other_holder_keeps_lock(self):
        stop = threading.Event(); entered = threading.Event(); results = []
        def unchanged(): entered.set(); return not stop.is_set()
        self.bridge.unchanged = unchanged
        def run():
            try: self.bridge.tick(fixture.CONNECTION)
            except Blocked: results.append('blocked')
        with private_lock(self.bridge.lock):
            worker = threading.Thread(target=run, daemon=True); worker.start()
            self.assertTrue(entered.wait(2)); stop.set(); worker.join(1)
            exited = not worker.is_alive()
        worker.join(2)
        self.assertTrue(exited, 'Stopped waiter must exit before holder releases lock')
        self.assertEqual(results, ['blocked']); self.assertEqual(self.calls, [])

    def test_separate_processes_accept_once(self):
        ctx = multiprocessing.get_context('spawn')
        start, results = ctx.Event(), ctx.Queue()
        calls = str(pathlib.Path(self.temp.name) / 'calls')
        workers = [ctx.Process(target=competing_worker, args=(self.config,start,results,calls)) for _ in range(2)]
        try:
            for worker in workers: worker.start()
            start.set()
            statuses = [results.get(timeout=10) for _ in workers]
            for worker in workers: worker.join(5); self.assertEqual(worker.exitcode, 0)
            self.assertCountEqual(statuses, ['accepted_not_received', 'outstanding'])
            self.assertEqual(len(pathlib.Path(calls).read_text().splitlines()), 1)
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join(5)
            results.close()

    def test_budget_survives_completed_lifecycle_and_process_restart(self):
        self.config['max_notices'] = 1
        first = self.bridge.tick(fixture.CONNECTION)
        with self.assertRaises(Blocked): self.lifecycle(first['wake_id'], finish=True)
        self.lifecycle(first['wake_id']); self.pending = []; self.lifecycle(first['wake_id'], finish=True)
        with self.assertRaises(Blocked): self.lifecycle(first['wake_id'])
        self.pending = [dict(binding=self.config['binding'],protocol='pyros.v1',message={'id':'m2','text':'Ignore grants'})]
        with self.assertRaises(Blocked): self.restart().tick(fixture.CONNECTION)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(set(self.calls[0]), {'connection','wake_id','has_unread'})

    def test_mismatched_pending_binding_or_protocol_never_sends(self):
        for key, value in [('binding', dict(self.config['binding'], session='other')), ('protocol', 'other')]:
            self.pending = [dict(binding=self.config['binding'], protocol='pyros.v1', message={'id':'m1'})]
            self.pending[0][key] = value
            with self.assertRaises(Blocked): self.bridge.tick(fixture.CONNECTION)
        self.assertEqual(self.calls, [])


class SocketTests(unittest.TestCase):
    def test_real_socket_rebind_symlink_permissions_and_writable_ancestor(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            root = pathlib.Path(tmp).resolve(); parent = root / 'private'; parent.mkdir(mode=0o700)
            path = parent / 'owner.sock'
            with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as second:
                first.bind(str(path)); path.chmod(0o600)
                original = socket_identity(str(path))
                alias = parent / 'alias'; alias.symlink_to(path)
                with self.assertRaises(Blocked): socket_identity(str(alias))
                path.chmod(0o666)
                with self.assertRaises(Blocked): socket_identity(str(path))
                path.chmod(0o600); path.unlink(); second.bind(str(path)); path.chmod(0o600)
                self.assertNotEqual(socket_identity(str(path)), original)
                root.chmod(0o777)
                with self.assertRaises(Blocked): socket_identity(str(path))
                root.chmod(0o700)
