"""Metadata-only execution endings; never extend consent or start a native turn."""
import hashlib
import json
from .state import Journal
from .worker import Blocked

TERMINAL = ('completed', 'failed', 'time_ran_out', 'disconnected')


def report_terminal(service, connection, binding, status, identity):
    """Persist the exact request before sending; replay uncertain outcomes verbatim.

    The membership capability and binding remain mandatory after runtime expiry.
    This helper does not ACK, post results, withdraw, or run provider work.
    """
    if status not in TERMINAL or not identity:
        raise Blocked('A known execution ending is required')
    path, _, client = service.member(connection, binding, allow_expired=True)
    journal = Journal(path.with_suffix('.sqlite'), binding)
    key = 'execution-end-' + hashlib.sha256((identity + ':' + status).encode()).hexdigest()
    try:
        journal.pin_scope(client.url, hashlib.sha256(client.credential.encode()).hexdigest())
        with journal.exclusive():
            saved = journal.control(key)
            record = json.loads(saved) if saved else {}
            if record.get('done'):
                return
            if record.get('payload'):
                client.request('/v1/commands', record['payload'])
            else:
                def persist(payload):
                    record['payload'] = payload
                    journal.control(key, json.dumps(record, sort_keys=True))
                client.command('report', target=status, text={
                    'completed': 'The agent finished its current work.',
                    'failed': 'The agent could not finish its current work.',
                    'time_ran_out': 'The approved connection time ran out. New work has stopped; an existing turn may still be finishing.',
                    'disconnected': 'The agent connection stopped.'
                }[status], key=key, before_send=persist)
            record['done'] = True
            journal.control(key, json.dumps(record, sort_keys=True))
    finally:
        journal.db.close()
