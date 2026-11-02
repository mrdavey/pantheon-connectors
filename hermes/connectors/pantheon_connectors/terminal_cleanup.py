"""Explicit terminal receipts and retained results, never execution authority.

Use the existing membership recovery transport, not a new MCP token or a retry
fallback. No receive, provider invocation, notification, or runtime renewal is
available here. A revoked or changed membership still fails server/native checks.
"""
import hashlib
import json

from .state import Journal, load_private
from .worker import Blocked


def closed_view(service, connection, binding):
    path, _, client = service.member(connection, binding, allow_expired=True)
    view = client.request('/v1/chat')
    chat = view.get('chat', {})
    own = chat.get('members', {}).get(view.get('member'), {})
    if (view.get('role') != 'agent' or chat.get('state') != 'closed'
            or own.get('revoked') is not False or own.get('binding') != binding):
        raise Blocked('Terminal cleanup requires this unchanged membership in a closed chat; no runtime was enabled')
    return path, client, view


def status(service, connection, binding):
    _, _, view = closed_view(service, connection, binding)
    # Return the authoritative retained outcome, not new peer history or work.
    chat = view['chat']
    projection = {key: view[key] for key in
                  ('role', 'member', 'result_agreed', 'result_final') if key in view}
    projection['chat'] = {key: chat[key] for key in ('id', 'state', 'reason', 'result') if key in chat}
    return dict(status='terminal_cleanup', untrusted_member_content=projection,
                runtime_enabled=False,
                notice='Retained closed-chat result only. No new work, receive, notice, runtime or provider turn is authorized.')


def ack(service, connection, binding, message_ids, key):
    if (not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 20
            or any(not isinstance(value, str) or not value for value in message_ids)
            or len(set(message_ids)) != len(message_ids)
            or not isinstance(key, str) or not 1 <= len(key) <= 256):
        raise Blocked('Terminal ACK requires one bounded batch of already received message IDs and its original key')
    path, client, _ = closed_view(service, connection, binding)
    journal_path = path.with_suffix('.sqlite')
    if not journal_path.exists():
        raise Blocked('No durable native receive evidence exists for terminal ACK')
    journal = Journal(journal_path, binding)
    try:
        journal.pin_scope(client.url, hashlib.sha256(client.credential.encode()).hexdigest())
        with journal.exclusive():
            received = {journal.record(identity).get('message') for identity, in
                        journal.db.execute("SELECT id FROM deliveries WHERE state='received'")
                        if journal.record(identity).get('reported') is True}
            if not set(message_ids) <= received:
                raise Blocked('Terminal ACK can only acknowledge messages already fetched in this exact native conversation')
            identity = 'terminal-ack-' + hashlib.sha256(key.encode()).hexdigest()
            intent = dict(message_ids=message_ids, key=key)
            saved = journal.control(identity)
            record = json.loads(saved) if saved else {'intent': intent}
            if record.get('intent') != intent:
                raise Blocked('Terminal ACK key already belongs to another batch')
            if record.get('payload'):
                return client.request('/v1/commands', record['payload'])
            def persist(payload):
                record['payload'] = payload
                journal.control(identity, json.dumps(record, sort_keys=True))
            # An ACK may have reached MCP before closure hid its response. Use
            # the exact durable command fields, never reconstruct its epoch or
            # replay a cached success. This is explicit terminal recovery, not
            # a fallback triggered by a transport error.
            from .onboarding import private_directory, private_lock
            directory = path.with_suffix('.mcp-operations')
            operation = directory / (hashlib.sha256(key.encode()).hexdigest()+'.json')
            if operation.exists() or operation.is_symlink():
                private_directory(directory)
                with private_lock(operation.with_suffix('.lock')):
                    saved = load_private(operation)
                    arguments = dict(connection=connection, message_ids=message_ids, key=key)
                    expected = {'scope': {'origin': client.url, 'binding': binding,
                                          'parent': hashlib.sha256(client.credential.encode()).hexdigest()},
                                'tool': 'pantheon_ack', 'contract': '1', 'arguments': arguments}
                    wire = saved.get('wire', {})
                    if (saved.get('intent') != expected
                            or not isinstance(wire, dict)
                            or set(wire) != set(arguments) | {'epoch', 'config'}
                            or any(wire.get(k) != v for k, v in arguments.items())
                            or any(type(wire.get(k)) is not int or wire[k] < 1 for k in ('epoch', 'config'))):
                        raise Blocked('Existing broker operation must retain its exact original scope and ACK intent')
                    payload = dict(op='ack', ids=message_ids, key=key, epoch=wire['epoch'], config=wire['config'])
                    persist(payload)
                    return client.request('/v1/commands', payload)
            return client.command('ack', ids=message_ids, key=key, before_send=persist)
    finally:
        journal.db.close()
