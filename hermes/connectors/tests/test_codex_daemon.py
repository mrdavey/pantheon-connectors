import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import types
import unittest
from unittest.mock import patch
from pantheon_connectors import codex_daemon as daemon
from pantheon_connectors.codex_delivery import DEFAULT_DAEMON, selected
from pantheon_connectors.codex_output import Owner, socket_identity
from pantheon_connectors.worker import Blocked

THREAD = '00000000-0000-4000-8000-000000000111'


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cd-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'h'; self.home.mkdir(mode=0o700)
        parent = self.home / 'app-server-control'; parent.mkdir(mode=0o700)
        self.alias = parent / 'app-server-control.sock'
        self.protected = self.root / 's'; self.protected.mkdir(mode=0o700)
        self.target = self.protected / hashlib.sha256(os.fsencode(str(self.alias))).hexdigest()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.target)); self.target.chmod(0o600)
        self.addCleanup(self.sock.close)
        self.alias.symlink_to(self.target)
        self.mock_root = patch.object(daemon, '_protected_root', return_value=self.protected)
        self.mock_root.start(); self.addCleanup(self.mock_root.stop)
        capabilities = patch.object(daemon, 'require_capabilities', return_value=True)
        self.capabilities = capabilities.start(); self.addCleanup(capabilities.stop)

    def test_exact_alias_allowed_but_old_owner_remains_strict(self):
        value = daemon.validate_endpoint(self.home, self.alias)
        self.assertEqual(value['target'], str(self.target))
        with self.assertRaises(Blocked): socket_identity(self.alias)
        (self.protected / 'another-daemon').write_text('unrelated')
        self.assertEqual(value, daemon.validate_endpoint(self.home, self.alias))
        self.alias.unlink(); self.alias.symlink_to(self.target)
        self.assertNotEqual(value, daemon.validate_endpoint(self.home, self.alias))

    def test_arbitrary_alias_modes_target_replacement_and_ancestors_rejected(self):
        with self.assertRaises(Blocked): daemon.validate_endpoint(self.home, self.target)
        self.target.chmod(0o660)
        with self.assertRaises(Blocked): daemon.validate_endpoint(self.home, self.alias)
        self.target.chmod(0o600)
        self.protected.chmod(0o755)
        with self.assertRaises(Blocked): daemon.validate_endpoint(self.home, self.alias)
        self.protected.chmod(0o700)
        self.alias.unlink(); self.alias.symlink_to(self.root / 'other')
        with self.assertRaises(Blocked): daemon.validate_endpoint(self.home, self.alias)
        self.alias.unlink(); self.alias.symlink_to(self.target)
        self.root.chmod(0o777)
        with self.assertRaises(Blocked): daemon.validate_endpoint(self.home, self.alias)
        self.root.chmod(0o700)
        old = daemon.validate_endpoint(self.home, self.alias)
        self.sock.close(); self.target.unlink()
        self.sock = socket.socket(socket.AF_UNIX); self.sock.bind(str(self.target)); self.target.chmod(0o600)
        self.addCleanup(self.sock.close)
        self.assertNotEqual(old, daemon.validate_endpoint(self.home, self.alias))

    def test_official_discovery_version_home_and_path_validation(self):
        value = dict(status='running', cliVersion=daemon.SUPPORTED_VERSION,
                     appServerVersion=daemon.SUPPORTED_VERSION, managedCodexVersion=daemon.SUPPORTED_VERSION,
                     managedCodexPath=str(self.home / 'packages/app-server-daemon/current/bin/codex'),
                     socketPath=str(self.alias))
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return types.SimpleNamespace(stdout='codex-cli '+value['cliVersion'] if args[-1]=='--version' else json.dumps(value))
        with patch.dict(os.environ, CODEX_HOME=str(self.home), FAKE_SECRET='never-forward'), patch.object(daemon.shutil, 'which', return_value='/fixture/codex'), patch.object(daemon.subprocess, 'run', side_effect=run):
            self.assertEqual(daemon.discover('/fixture/codex', self.home)['target'], str(self.target))
            self.assertEqual(calls[1][0], ['/fixture/codex', 'app-server', 'daemon', 'version'])
            self.assertNotIn('FAKE_SECRET', calls[1][1]['env'])
            for key, wrong in [('socketPath', str(self.root/'elsewhere')), ('appServerVersion','different'), ('managedCodexPath','/other/codex'), ('status','stopped')]:
                previous = value[key]; value[key] = wrong
                with self.assertRaises(Blocked): daemon.discover('/fixture/codex', self.home)
                value[key] = previous
            with self.assertRaises(Blocked): daemon.discover('/fixture/codex', self.root)
            for version in ('0.162.2', '0.200.0', '1.0.0-alpha.2'):
                value.update(cliVersion=version, appServerVersion=version, managedCodexVersion=version)
                self.assertEqual(daemon.discover('/fixture/codex', self.home)['runtime_version'], version)
            value['cliVersion'] = '2.0.0'
            self.assertEqual(daemon.discover('/fixture/codex', self.home)['runtime_version'], '1.0.0-alpha.2')
            self.capabilities.assert_any_call('/fixture/codex')
            self.capabilities.assert_any_call(value['managedCodexPath'])
            self.capabilities.side_effect = Blocked('missing capability')
            with self.assertRaises(Blocked): daemon.discover('/fixture/codex', self.home)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.setup = str(Path(self.temp.name).resolve()/'private'/'setup.json')

    def test_per_call_metadata_cannot_be_replaced_by_arguments_or_root_session(self):
        self.assertEqual(daemon.native_identity({'threadId': THREAD, 'sessionId': 'different-root'}), THREAD)
        for meta in (None, {}, {'sessionId': THREAD}, {'threadId': 'bad'},
                     {'threadId': THREAD, 'x-codex-turn-metadata': {'thread_id': 'different'}}):
            with self.assertRaises(Blocked): daemon.native_identity(meta)

    def test_new_mode_requires_separate_owner_and_finite_access(self):
        config = dict(delivery=DEFAULT_DAEMON, binding=dict(runtime='codex', surface='app-server-tool-output',
                       version=daemon.SUPPORTED_VERSION, profile='default', session=THREAD))
        self.assertEqual(selected(config), DEFAULT_DAEMON)
        with self.assertRaises(Blocked): Owner(config)
        with patch.object(daemon, 'discover', side_effect=AssertionError('must not connect')):
            with self.assertRaises(Blocked): daemon.DefaultDaemonOwner(config)
            config['allow_daemon_access'] = True
            with self.assertRaises(Blocked): daemon.DefaultDaemonOwner(config)

    def test_mcp_identity_validation_and_inert_startup(self):
        from pantheon_connectors.codex_mcp import Server
        server = Server(self.setup)
        try:
            self.assertTrue(server.handle('initialize', {})['capabilities'])
            self.assertFalse(server.workers)
            tools = server.handle('tools/list', {})['tools']
            self.assertNotIn('threadId', str([x['inputSchema'] for x in tools]))
            with patch('pantheon_connectors.codex_mcp.load_private', side_effect=AssertionError('no state read')):
                with self.assertRaises(Blocked): server.handle('tools/call', {'name':'pantheon_doctor','arguments':{'threadId':THREAD}})
        finally: server.close()

class DefaultScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.setup = str(Path(self.temp.name).resolve()/'private'/'setup.json')

    def test_wrong_native_thread_never_reaches_operation(self):
        from pantheon_connectors.codex_mcp import Server
        config=dict(delivery=DEFAULT_DAEMON,codex_home=str(Path.home()/'.codex'),
                    binding=dict(runtime='codex',surface='app-server-tool-output',session=THREAD))
        server=Server(self.setup)
        try:
            with patch('pantheon_connectors.codex_mcp.load_private',return_value=config), patch('pantheon_connectors.codex_mcp.execute') as execute:
                with self.assertRaises(Blocked):
                    server.handle('tools/call',dict(name='pantheon_doctor',arguments={},_meta={'threadId':'00000000-0000-4000-8000-000000000222'}))
                execute.assert_not_called()
        finally:server.close()

    def test_replaced_discovery_and_profile_rejected_before_connecting(self):
        import time
        config=dict(delivery=DEFAULT_DAEMON,allow_daemon_access=True,allow_runtime_turns=True,trial_expires_at=time.time()+60,
                    codex_executable='/fixture/codex',codex_home='/fixture/home',daemon_endpoint={'pinned':'original'},
                    binding=dict(runtime='codex',surface='app-server-tool-output',version=daemon.SUPPORTED_VERSION,profile='default',session=THREAD))
        with patch.object(daemon,'discover',return_value={'pinned':'replacement'}), patch('pantheon_connectors.codex_output.AttachedAppServer') as client:
            with self.assertRaises(Blocked):daemon.DefaultDaemonOwner(config)
            config['binding']['profile']='another'
            with self.assertRaises(Blocked):daemon.DefaultDaemonOwner(config)
            config['binding']['profile']='default';config['binding']['version']='future'
            with self.assertRaises(Blocked):daemon.DefaultDaemonOwner(config)
            client.assert_not_called()


    def test_capable_versions_keep_exact_prepared_runtime_and_old_endpoint_pins(self):
        config=dict(delivery=DEFAULT_DAEMON,codex_executable='/fixture/codex',codex_home='/fixture/home',
                    daemon_endpoint={'target':'original','runtime_version':'0.200.0'},
                    binding=dict(runtime='codex',surface='app-server-tool-output',version='0.200.0',profile='default',session=THREAD))
        with patch.object(daemon,'discover',return_value=dict(config['daemon_endpoint'])) as discover, patch.object(Owner,'__init__',return_value=None) as attach:
            daemon.DefaultDaemonOwner(config,inspection=True);attach.assert_called_once()
            config['daemon_endpoint'].pop('runtime_version')
            daemon.DefaultDaemonOwner(config,inspection=True)
            discover.return_value['runtime_version']='0.200.1'
            with self.assertRaises(Blocked):daemon.DefaultDaemonOwner(config,inspection=True)
            self.assertEqual(attach.call_count,2)


class UsageTests(unittest.TestCase):
    def test_thread_usage_must_be_known_main_thread(self):
        valid=dict(source='cli',parentThreadId=None,model='configured',modelProvider='owner-provider')
        self.assertEqual(daemon.thread_usage(valid),dict(model='configured',model_provider='owner-provider'))
        self.assertEqual(daemon.thread_usage(dict(valid,source='appServer')),daemon.thread_usage(valid))
        for change in ({'parentThreadId':THREAD},{'model':None},{'modelProvider':''}):
            with self.assertRaises(Blocked):daemon.thread_usage(dict(valid,**change))


class ReceiverLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.setup = str(Path(self.temp.name).resolve()/'private'/'setup.json')
        from pantheon_connectors.state import save_private
        save_private(self.setup, {'delivery': 'default-daemon-tool-output',
                                 'binding': {'surface': 'app-server-tool-output'}})

    def test_plugin_shutdown_stops_only_its_worker_and_never_restarts_implicitly(self):
        import threading
        from pantheon_connectors.codex_mcp import Server
        entered, stopped = threading.Event(), threading.Event()
        def run(setup, connection, stop, report, audit):
            entered.set()
            stop.wait(2)
            stopped.set()
        server = Server(self.setup)
        with patch('pantheon_connectors.codex_mcp.run', side_effect=run) as receiver:
            server.start('fixture')
            self.assertTrue(entered.wait(1))
            server.start('fixture')
            self.assertEqual(receiver.call_count, 1)
            server.close()
            self.assertTrue(stopped.is_set())
            with self.assertRaises(Blocked): server.start('fixture')
            self.assertEqual(receiver.call_count, 1)

    def test_failed_receiver_requires_explicit_recovery_and_preserves_reason(self):
        from pantheon_connectors.codex_mcp import Server
        server = Server(self.setup)
        with patch('pantheon_connectors.codex_mcp.run', side_effect=Blocked('fixture')) as receiver:
            server.start('fixture')
            server.workers['fixture'][1].join(1)
            self.assertEqual(server.status['fixture']['status'], 'stopped')
            self.assertEqual(receiver.call_count, 1)
            server.handle('initialize', {})
            server.handle('tools/list', {})
            self.assertEqual(receiver.call_count, 1)
            server.close()
