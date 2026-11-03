#!/usr/bin/env python3
"""Offline contract checks and explicitly scripted mocks; NEVER connects to Codex.

Usage: python3 scripts/codex_tool_output_contract.py EXPORTED_SCHEMA_DIR [...]
Exports must come from the installed binary's generate-json-schema command.
No network, subprocess, native state, model, live invitation or credentials.
"""
import argparse
import copy
import hashlib
import json
import pathlib
import unittest

THREAD = '00000000-0000-4000-8000-000000000031'
def notification(connection, wake_id, has_unread):
    """Result of a pending-status check; no peer text, credentials or authority."""
    return {'connection': connection, 'wake_id': wake_id, 'has_unread': bool(has_unread),
            'notice': 'Fetch this bound Pantheon chat with pantheon_receive; explicitly ACK consumed messages.'}


RESULT = notification('synthetic-connection', 'synthetic-wake-1', True)


def request(result):
    return {'threadId': THREAD, 'input': [], 'toolOutput': {
        'name': 'pantheon_pending_notification', 'namespace': 'pantheon',
        'output': json.dumps(result, sort_keys=True, separators=(',', ':'))}}


class ScriptedHost:
    """Model the documented outcomes, not an implementation of Codex scheduling."""
    def __init__(self, busy=False, lose_reply=False):
        self.busy, self.lose_reply = busy, lose_reply
        self.calls = []

    def call(self, method, params):
        if method != 'turn/start':
            raise AssertionError('The offline driver must not interrupt/resume/fallback/ACK')
        if params['threadId'] != THREAD or params['input'] or not params['toolOutput']['name']:
            raise ValueError('Rejected by the explicitly modeled source-level contract')
        self.calls.append(copy.deepcopy(params))
        if self.lose_reply:
            raise TimeoutError('Scripted lost response after possible execution')
        return {'turn': {'id': 'already-active-turn' if self.busy else 'mock-new-turn',
                         'status': 'inProgress'}}


class CandidateDriver:
    """In-memory mock client only. NOT a deployable adapter or durable journal."""
    def __init__(self, host):
        self.host, self.attempts = host, {}

    def send(self, result):
        params = request(result)
        fingerprint = hashlib.sha256(params['toolOutput']['output'].encode()).hexdigest()
        if fingerprint in self.attempts:
            raise RuntimeError('Already attempted; no replay')
        entry = self.attempts[fingerprint] = {'state': 'attempting', 'receipt': False, 'ack': False,
                                            'output': params['toolOutput']['output']}
        try:
            response = self.host.call('turn/start', params)
        except Exception:
            entry['state'] = 'uncertain'
            raise
        entry.update(state='accepted', turn_id=response['turn']['id'])
        return fingerprint

    def notification(self, fingerprint, event):
        entry = self.attempts[fingerprint]
        item = event.get('item', {})
        match = (event.get('threadId') == THREAD and event.get('turnId') == entry.get('turn_id')
                 and item.get('type') == 'functionCallOutput'
                 and item.get('name') == 'pantheon_pending_notification' and item.get('namespace') == 'pantheon'
                 and item.get('output') == entry['output'])
        if match:
            entry['receipt'] = True  # Model consumption and Pantheon ACK still unproved.
        return match


def suite(paths):
    class ContractTests(unittest.TestCase):
        def test_exported_schema_shape_in_each_installed_binary(self):
            for directory in paths:
                with self.subTest(binary=str(directory)):
                    schema = json.loads((directory / 'v2/TurnStartParams.json').read_text())
                    self.assertEqual(set(schema['required']), {'threadId', 'input'})
                    ref = schema['properties']['toolOutput']['anyOf'][0]['$ref']
                    self.assertEqual(ref, '#/definitions/TurnToolOutput')
                    output = schema['definitions']['TurnToolOutput']
                    self.assertEqual(set(output['required']), {'name', 'output'})
                    self.assertEqual(output['properties']['name']['type'], 'string')
                    self.assertEqual(set(output['properties']['namespace']['type']), {'string', 'null'})
                    self.assertEqual(output['properties']['output']['$ref'], '#/definitions/FunctionCallOutputBody')
                    body = schema['definitions']['FunctionCallOutputBody']['anyOf']
                    self.assertEqual({branch['type'] for branch in body}, {'string', 'array'})

        def test_exported_notification_has_tool_attribution(self):
            for directory in paths:
                schema = json.loads((directory / 'v2/ItemCompletedNotification.json').read_text())
                items = schema['definitions']['ThreadItem']['oneOf']
                item = next(i for i in items if i.get('properties', {}).get('type', {}).get('enum') == ['functionCallOutput'])
                self.assertEqual(set(item['required']), {'id', 'name', 'output', 'type'})
                self.assertIn('namespace', item['properties'])
                self.assertIn('threadId', schema['required'])
                self.assertIn('turnId', schema['required'])

        def test_schema_alone_does_not_enforce_semantic_restrictions(self):
            # Empty-name and mutually-exclusive-input restrictions were verified
            # in matching Rust source; these two field shapes do not encode them.
            for directory in paths:
                schema = json.loads((directory / 'v2/TurnStartParams.json').read_text())
                name = schema['definitions']['TurnToolOutput']['properties']['name']
                self.assertNotIn('minLength', name)
                self.assertNotIn('maxItems', schema['properties']['input'])

        def test_content_free_notification_has_no_peer_text_or_authority_overrides(self):
            params = request(RESULT)
            self.assertEqual(set(params), {'threadId', 'input', 'toolOutput'})
            self.assertEqual(params['input'], [])
            self.assertEqual(json.loads(params['toolOutput']['output']), RESULT)
            self.assertEqual(set(params['toolOutput']), {'name', 'namespace', 'output'})
            self.assertEqual(set(RESULT), {'connection', 'wake_id', 'has_unread', 'notice'})
            self.assertNotIn('messages', RESULT)
            self.assertNotIn('text', RESULT)

        def test_mock_idle_acceptance_is_not_receipt_or_ack(self):
            driver = CandidateDriver(ScriptedHost())
            identity = driver.send(RESULT)
            entry = driver.attempts[identity]
            self.assertEqual(entry['turn_id'], 'mock-new-turn')
            self.assertFalse(entry['receipt']); self.assertFalse(entry['ack'])

        def test_mock_busy_result_can_reference_existing_unowned_turn(self):
            host = ScriptedHost(busy=True); driver = CandidateDriver(host)
            identity = driver.send(RESULT)
            self.assertEqual(driver.attempts[identity]['turn_id'], 'already-active-turn')
            self.assertNotIn('owns_turn', driver.attempts[identity])
            self.assertEqual(len(host.calls), 1)
            # No cancellation or interruption exists in this driver.

        def test_mock_timeout_stays_uncertain_without_retry_or_fallback(self):
            host = ScriptedHost(lose_reply=True); driver = CandidateDriver(host)
            with self.assertRaises(TimeoutError): driver.send(RESULT)
            with self.assertRaises(RuntimeError): driver.send(RESULT)
            self.assertEqual(len(host.calls), 1)
            self.assertEqual(next(iter(driver.attempts.values()))['state'], 'uncertain')

        def test_mock_notification_requires_exact_thread_turn_tool_and_output(self):
            driver = CandidateDriver(ScriptedHost()); identity = driver.send(RESULT)
            event = {'threadId': THREAD, 'turnId': 'mock-new-turn', 'item': {
                'id': 'mock-item', 'type': 'functionCallOutput', 'name': 'pantheon_pending_notification',
                'namespace': 'pantheon', 'output': request(RESULT)['toolOutput']['output']}}
            for key in ('threadId', 'turnId'):
                altered = copy.deepcopy(event); altered[key] = 'other'
                self.assertFalse(driver.notification(identity, altered))
            for key in ('type', 'name', 'namespace', 'output'):
                altered = copy.deepcopy(event); altered['item'][key] = 'other'
                self.assertFalse(driver.notification(identity, altered))
            self.assertTrue(driver.notification(identity, event))
            self.assertFalse(driver.attempts[identity]['ack'])

        def test_mock_rejects_mixed_input_empty_name_and_wrong_thread(self):
            host = ScriptedHost()
            for field, value in [('input', [{'type': 'text', 'text': 'user'}]), ('threadId', 'other')]:
                params = request(RESULT); params[field] = value
                with self.assertRaises(ValueError): host.call('turn/start', params)
            params = request(RESULT); params['toolOutput']['name'] = ''
            with self.assertRaises(ValueError): host.call('turn/start', params)
            self.assertEqual(host.calls, [])
    return unittest.defaultTestLoader.loadTestsFromTestCase(ContractTests)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('schema_dirs', nargs='+', type=pathlib.Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(suite(args.schema_dirs))
    print(json.dumps({'evidence': 'installed schema structural checks + scripted mocks ONLY',
                      'schemas': [str(p.resolve()) for p in args.schema_dirs],
                      'tests': result.testsRun, 'passed': result.wasSuccessful(),
                      'native_execution_tested': False, 'idle_busy_scheduling_verified': False,
                      'desktop_integration_verified': False, 'model_calls': 0}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
