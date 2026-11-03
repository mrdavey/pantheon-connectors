import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pantheon_connectors.broker.bootstrap import prepared, read, record
from pantheon_connectors.onboarding import Onboarding
from pantheon_connectors.worker import Blocked


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {'mcp': {'contract': 'pantheon.broker.v1'}, 'url': 'https://pantheon.talk',
                       'state_dir': str(self.root / 'state'), 'credential': 'PRIVATE'}
        self.binding = {'runtime': 'codex', 'session': 'private-session', 'profile': 'default'}
        self.connection = 'a' * 32

    def test_preparation_never_populates_membership_or_changes_consent(self):
        original = copy.deepcopy(self.config)
        prepared(self.config, self.connection, self.binding)
        value = read(self.config, self.connection, self.binding)
        self.assertEqual([e['state'] for e in value['events']], ['discovered', 'prepared', 'host_loaded'])
        self.assertEqual(self.config, original)
        self.assertFalse((self.root / 'state').exists())
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertNotIn('private-session', json.dumps(value))
        self.assertTrue(value['historical_evidence_only'])

    def test_returning_setup_preserves_later_evidence_and_terminal_states(self):
        prepared(self.config, self.connection, self.binding)
        record(self.config, self.connection, self.binding, 'joined', 'redemption_committed')
        prepared(self.config, self.connection, self.binding)
        self.assertEqual(read(self.config, self.connection, self.binding)['state'], 'joined')
        record(self.config, self.connection, self.binding, 'revoked', 'withdraw_accepted')
        record(self.config, self.connection, self.binding, 'authorized', 'native_allowance_checked')
        self.assertEqual(read(self.config, self.connection, self.binding)['state'], 'revoked')

    def test_scope_cannot_be_changed_and_evidence_is_bounded(self):
        prepared(self.config, self.connection, self.binding)
        with self.assertRaises(Blocked):
            read(self.config, self.connection, {**self.binding, 'session': 'other'})
        for _ in range(50):
            record(self.config, self.connection, self.binding, 'blocked', 'native_check_failed')
            record(self.config, self.connection, self.binding, 'joined', 'redemption_committed')
        self.assertEqual(len(read(self.config, self.connection, self.binding)['events']), 24)
        with self.assertRaises(ValueError):
            record(self.config, self.connection, self.binding, 'joined', 'private raw exception')

    def test_readiness_record_requires_successful_native_ready(self):
        service = Onboarding(self.config)
        client = unittest.mock.Mock()
        client.command.side_effect = Blocked('No readiness')
        with patch.object(service, 'member', return_value=(None, {}, client)):
            with self.assertRaises(Blocked):
                service.command(self.connection, self.binding, 'ready', key='ready')
            self.assertEqual(read(self.config, self.connection, self.binding)['state'], 'not_recorded')
            client.command.side_effect = None
            client.command.return_value = {'status': 'ok'}
            service.command(self.connection, self.binding, 'ready', key='ready')
        self.assertEqual(read(self.config, self.connection, self.binding)['state'], 'delivery_verified')
