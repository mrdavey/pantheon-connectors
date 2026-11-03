import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pantheon_connectors import hosted_v3, runtime_policy
from pantheon_connectors.broker.catalog import LOCAL
from pantheon_connectors.hosted_discovery import validate_discovery, validate_hosted_admission
from pantheon_connectors.worker import Blocked

BASE = 'https://pantheon.talk'


def fixture():
    package = {'runtime': 'codex', 'version': '0.6.1', 'format': 'tar.gz', 'sha256': 'a' * 64,
               'source_revision': 'b' * 40, 'artifact_url': BASE + '/plugins/' + 'a' * 64 + '/codex.tar.gz',
               'host': {'compatibility': 'native-capabilities', 'tested_version': '0.162.1'}}
    return {'schema': hosted_v3.SCHEMA, 'service_origin': BASE, 'expires_at': time.time() + 3600,
            'admission': {'enabled': True, 'runtimes': ['codex']}, 'packages': {'codex': package},
            'mcp_resource': BASE + '/mcp', 'catalog_url': BASE + '/v1/tool-catalog',
            'guide_url': BASE + '/v1/onboarding/v3/guide', 'broker_contract': hosted_v3.CONTRACT,
            'protocol_versions': ['2025-11-25', '2025-06-18'], 'primitive_contracts': list(LOCAL.values()),
            'compatibility': {'codex': {'minimum_version': '0.6.0', 'maximum_exclusive': '0.7.0',
                                       'revoked_versions': [], 'host_modes': ['app-server-tool-output']}}}


class HostedV3Tests(unittest.TestCase):
    def test_fresh_family_requires_same_origin_content_addressed_guide(self):
        value = fixture()
        value['packages']['codex']['version'] = '0.7.3'
        value['compatibility']['codex'].update(minimum_version='0.7.3', maximum_exclusive='0.8.0')
        for url in (value['guide_url'], value['guide_url']+'/'+'a'*63, value['guide_url']+'/'+'a'*64+'?invite=secret', 'https://elsewhere.test/v1/onboarding/v3/guide/'+'a'*64):
            with self.subTest(url=url), self.assertRaises(Blocked):
                hosted_v3.validate({**value, 'guide_url': url}, BASE, 'codex')
        value['guide_url'] += '/'+'a'*64
        self.assertEqual(hosted_v3.validate(value, BASE, 'codex'), value)

    def setUp(self):
        self.discovery = fixture()
        self.binding = {'runtime': 'codex', 'version': '0.162.1', 'surface': 'app-server-tool-output',
                        'profile': 'default', 'session': 'native-original'}
        self.installed = {'version': '0.6.0', 'source_revision': 'c' * 40, 'broker_contract': hosted_v3.CONTRACT}

    def test_compatible_older_package_survives_new_server_release(self):
        hosted_v3.validate(self.discovery, BASE, 'codex')
        hosted_v3.check_installed(self.discovery, 'codex', self.installed)
        config = hosted_v3.prepare_config({'url': BASE, 'hosted_consent': True, 'trial_expires_at': 123,
                                           'allow_runtime_turns': False}, self.discovery, self.installed, self.binding)
        fresh = copy.deepcopy(self.discovery)
        fresh['packages']['codex'].update(version='0.6.2', source_revision='d' * 40)
        config['hosted_onboarding']['expires_at'] = 1
        class Client:
            def __init__(self, *_): pass
            def request(self, path):
                self_path = '/v1/onboarding/v3'
                assert path == self_path
                return fresh
        result = validate_hosted_admission(config, self.binding, client_factory=Client)
        self.assertEqual(result, fresh)
        refreshed = hosted_v3.prepare_config(config, fresh, self.installed, self.binding)
        self.assertTrue(runtime_policy.same_scope(refreshed, config))
        self.assertEqual(refreshed['trial_expires_at'], 123)
        self.assertFalse(refreshed['allow_runtime_turns'])

    def test_revocations_minimum_and_same_version_replacement_fail_closed(self):
        for mutate in (
            lambda d: d['compatibility']['codex']['revoked_versions'].append('0.6.0'),
            lambda d: d['compatibility']['codex'].update(minimum_version='0.6.1'),
            lambda d: d['packages']['codex'].update(version='0.6.0', source_revision='f' * 40),
        ):
            value = copy.deepcopy(self.discovery)
            mutate(value)
            with self.assertRaises(Blocked):
                hosted_v3.check_installed(value, 'codex', self.installed)

    def test_v2_strict_decoder_still_rejects_v3(self):
        with self.assertRaises(Blocked):
            validate_discovery(self.discovery, BASE, 'codex')

    def test_foreign_urls_primitives_host_modes_and_protocols_rejected(self):
        for field, value in [('mcp_resource', 'https://other.test/mcp'), ('catalog_url', BASE + '/redirect'),
                             ('primitive_contracts', ['execute.shell.v1']), ('protocol_versions', ['2026-07-28'])]:
            discovery = copy.deepcopy(self.discovery)
            discovery[field] = value
            with self.assertRaises(Blocked):
                hosted_v3.validate(discovery, BASE, 'codex')

    def test_codex_returning_setup_uses_v3_without_reinspection_or_reconsent(self):
        from test_codex_hosted import HostedCodexTests, LINK, CONNECTION, THREAD
        case = HostedCodexTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.metadata.update(package_version='0.6.0', source_revision='c' * 40, broker_contract=hosted_v3.CONTRACT)
        with patch('pantheon_connectors.hosted_v3.fetch', return_value=self.discovery):
            case.prepare_scope()
            path = case.server.location(THREAD, CONNECTION)
            from pantheon_connectors.state import load_private, save_private
            saved = load_private(path)
            self.assertEqual(saved['mcp'], {'contract': hosted_v3.CONTRACT})
            saved.update(allow_runtime_turns=False, trial_expires_at=123)
            save_private(path, saved)
            self.discovery['packages']['codex'].update(version='0.6.2', source_revision='d' * 40)
            case.call('pantheon_prepare', {'link': LINK, 'deliberate': True})
            self.assertEqual(case.prepare.call_count, 1)
            current = load_private(path)
            self.assertEqual(current['trial_expires_at'], 123)
            self.assertFalse(current['allow_runtime_turns'])
            self.assertEqual(current['installed_connector'], self.installed)
            # A separately loaded and verified compatible upgrade keeps the
            # original expired grant; it does not demand another redemption.
            case.metadata.update(package_version='0.6.2', source_revision='d' * 40)
            case.call('pantheon_prepare', {'link': LINK, 'deliberate': True})
            upgraded = load_private(path)
            self.assertEqual(upgraded['installed_connector']['version'], '0.6.2')
            self.assertEqual(upgraded['trial_expires_at'], 123)
            self.assertFalse(upgraded['allow_runtime_turns'])
            self.assertEqual(case.prepare.call_count, 1)
            case.metadata.update(package_version='0.6.0', source_revision='c' * 40)
            with self.assertRaises(Blocked):
                case.call('pantheon_prepare', {'link': LINK, 'deliberate': True})

    def test_upgrade_rejects_same_version_replacement_and_contract_changes(self):
        config = hosted_v3.prepare_config({'url': BASE, 'hosted_consent': True},
                                          self.discovery, self.installed, self.binding)
        for installed in ({**self.installed, 'source_revision': 'f' * 40},
                          {**self.installed, 'version': '0.6.2', 'broker_contract': 'unknown'}):
            with self.assertRaises(Blocked):
                hosted_v3.reprepare_config(config, self.discovery, installed, self.binding)

    def test_hermes_v3_preparation_preserves_approved_scope_on_catalogue_release(self):
        from test_hermes_hosted_setup import HostedHermesTests
        from pantheon_connectors.state import load_private
        case = HostedHermesTests()
        case.setUp(); self.addCleanup(case.doCleanups)
        value = fixture()
        package = value['packages'].pop('codex')
        package.update(runtime='hermes', version='0.4.1', host={'runtime_version': case.binding['version']})
        value['packages']['hermes'] = package
        value['admission']['runtimes'] = ['hermes']
        value['compatibility'] = {'hermes': {'minimum_version': '0.4.0', 'maximum_exclusive': '0.5.0',
                                            'revoked_versions': [], 'host_modes': ['native-adapter']}}
        installed = {**self.installed, 'version': '0.4.0'}
        case.d = value
        with patch('pantheon_connectors.hosted_v3.installed_hermes', return_value=installed):
            result = case.prepare()
            self.assertEqual(result['status'], 'prepared_disabled')
            setup = Path(result['setup_file'])
            self.assertEqual(load_private(setup)['mcp'], {'contract': hosted_v3.CONTRACT})
            approved = case.authorize(result['connection'])
            self.assertEqual(approved['status'], 'runtime_authorized')
            before = load_private(setup)
            case.d['packages']['hermes'].update(version='0.4.2', source_revision='d' * 40)
            self.assertEqual(case.prepare()['status'], 'prepared_existing')
            after = load_private(setup)
            self.assertEqual(after['hermes_runtime_grant'], before['hermes_runtime_grant'])
            self.assertEqual(after['runtime_started_at'], before['runtime_started_at'])
            self.assertEqual(after['installed_connector'], installed)
            installed.update(version='0.4.2', source_revision='d' * 40)
            self.assertEqual(case.prepare()['status'], 'prepared_existing')
            upgraded = load_private(setup)
            self.assertEqual(upgraded['installed_connector'], installed)
            self.assertEqual(upgraded['hermes_runtime_grant'], before['hermes_runtime_grant'])
            self.assertEqual(upgraded['runtime_started_at'], before['runtime_started_at'])


if __name__ == '__main__':
    unittest.main()

class DesktopAdmissionTests(unittest.TestCase):
    def test_shared_descriptor_requires_compatible_hermes_reader(self):
        from test_codex_desktop import discovery
        value = discovery()
        value['packages']['hermes'] = {**value['packages']['codex'], 'runtime': 'hermes', 'version': '0.5.6',
                                      'host': {'runtime_version': 'hermes-fixture'}}
        value['admission']['runtimes'].append('hermes')
        value['compatibility']['hermes'] = dict(minimum_version='0.5.5', maximum_exclusive='0.6.0',
                                              revoked_versions=[], host_modes=['native-adapter'])
        with self.assertRaisesRegex(Blocked, 'Hermes 0.5.6'):
            hosted_v3.validate(value, BASE, 'hermes')
        value['compatibility']['hermes']['minimum_version'] = '0.5.6'
        self.assertEqual(hosted_v3.validate(value, BASE, 'hermes'), value)
        old = dict(version='0.5.5', source_revision='c'*40, broker_contract=hosted_v3.CONTRACT)
        with self.assertRaises(Blocked): hosted_v3.check_installed(value, 'hermes', old)

    def test_gui_is_explicit_new_family_only_and_never_v2(self):
        from test_codex_desktop import discovery
        from pantheon_connectors.hosted_discovery import matches_native_host
        value = discovery()
        binding = dict(runtime='codex', version='0.162.1', surface='native-queue', profile='default', session='thread')
        installed = hosted_v3.installed_codex(dict(package_version='0.9.0', source_revision='b'*40, broker_contract=hosted_v3.CONTRACT))
        config = hosted_v3.prepare_config({'url': BASE}, value, installed, binding)
        self.assertEqual(config['mcp'], {'contract': hosted_v3.CONTRACT})
        self.assertFalse(matches_native_host(value['packages']['codex'], binding))
        for modes in (['app-server-tool-output'], ['native-queue'], ['app-server-tool-output', 'native-queue', 'native-queue']):
            bad = copy.deepcopy(value)
            bad['compatibility']['codex']['host_modes'] = modes
            with self.assertRaises(Blocked): hosted_v3.prepare_config({'url': BASE}, bad, installed, binding)
        bad = copy.deepcopy(value)
        bad['compatibility']['codex']['minimum_version'] = '0.8.3'
        with self.assertRaises(Blocked): hosted_v3.validate(bad, BASE, 'codex')
        bad = copy.deepcopy(value)
        bad['compatibility']['codex']['revoked_versions'] = ['0.9.0']
        with self.assertRaises(Blocked): hosted_v3.prepare_config({'url': BASE}, bad, installed, binding)
