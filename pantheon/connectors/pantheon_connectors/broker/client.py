"""Native-bound resource credentials and durable, retry-safe remote tool calls.

The membership secret never reaches /mcp. Delivery and binary primitives retain
their independently qualified transports. A transport error never causes fallback.
"""
import copy
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from ..client import APIError, Client, COMPLETION_PROTOCOL
from ..state import load_private, save_private
from ..worker import Blocked
from .catalog import for_origin
from .transport import HTTP

CONFIG = {'contract': 'pantheon.broker.v1'}
REMOTE_COMMANDS = {'post', 'ack', 'ready', 'withdraw', 'handback', 'report', 'definition', 'result', 'confirm'}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise Blocked('Invalid resource credential expiry') from None


class BrokerClient(Client):
    def __init__(self, path, config, allowance, *, http=None, catalog=None, clock=time.time):
        super().__init__(config['url'], config['credential'])
        if config.get('mcp') != CONFIG:
            raise Blocked('Unsupported broker contract')
        self.path, self.config, self.allowance = path, copy.deepcopy(config), allowance
        self.http, self.catalog = http or HTTP(self.url), catalog or for_origin(self.url)
        self.clock = clock
        self.local_connection = path.stem
        self.scope = {'origin': self.url, 'binding': config['binding'],
                      'parent': hashlib.sha256(self.credential.encode()).hexdigest()}

    def _token(self, _recovered=False):
        from ..onboarding import private_lock
        self.allowance()
        path = self.path.with_suffix('.mcp-token.json')
        with private_lock(self.path.with_suffix('.mcp-token.lock')):
            self.allowance()
            state = load_private(path) if path.exists() or path.is_symlink() else {'scope': self.scope}
            if state.get('scope') != self.scope or state.get('revoked'):
                raise Blocked('Resource credential belongs to another scope or was revoked')
            current = state.get('current')
            now = self.clock()
            if current and timestamp(current['expires_at']) > now + 30 and not state.get('pending'):
                return current['connection'], current['connection'] + '.' + current['secret']
            pending = state.get('pending')
            if not pending:
                deadline = now + 300
                finite = self.config.get('trial_expires_at', 0)
                if finite:
                    deadline = min(deadline, finite)
                pending = {'secret': secrets.token_urlsafe(32), 'binding': self.config['binding'],
                           'expires_at': datetime.fromtimestamp(deadline, timezone.utc).isoformat()}
                if current:
                    pending['previous'] = current['secret']
                state['pending'] = pending
                save_private(path, state)
            self.allowance()
            try:
                result, _ = self.http.request('/v1/mcp/token', payload=pending, credential=self.credential)
            except APIError as error:
                if (error.status != 409 or error.reason != 'mcp_exchange_expired_uncommitted'
                        or timestamp(pending['expires_at']) > now or _recovered):
                    raise
                # The server has proved this exact expired intent did not commit.
                # Preserve the last committed secret, but discard this intent only.
                state.pop('pending')
                save_private(path, state)
                result = None
            if result is not None:
                if (not isinstance(result, dict) or set(result) != {'connection', 'expires_at', 'resource'}
                        or result['resource'] != self.url + '/mcp'
                        or not isinstance(result['connection'], str)
                        or len(result['connection']) != 32 or any(c not in '0123456789abcdef' for c in result['connection'])
                        or not timestamp(result['expires_at']) <= timestamp(pending['expires_at'])
                        or (current and current['connection'] != result['connection'])):
                    raise Blocked('Resource exchange returned a different scope')
                state['current'] = {**result, 'secret': pending['secret']}
                state.pop('pending', None)
                save_private(path, state)
                self.allowance()
                if timestamp(result['expires_at']) > now:
                    return result['connection'], result['connection'] + '.' + pending['secret']
        # The exact uncertain exchange is reconciled before attempting rotation.
        # Leaving the file lock first also permits safe multi-process recovery.
        if _recovered:
            raise Blocked('Resource exchange returned an expired grant; preserve state')
        return self._token(_recovered=True)

    def _call(self, descriptor, arguments):
        self.allowance()
        connection, token = self._token()
        wire = {**arguments, 'connection': connection}
        result = self.http.rpc('tools/call', {'name': descriptor['name'], 'arguments': wire}, token)
        self.allowance()
        if result.get('isError'):
            code = (result.get('content') or [{}])[0].get('text')
            status = {'not_authorized': 403, 'scope_expired': 410, 'conflict': 409,
                      'invalid_arguments': 400, 'capability_unavailable': 503}.get(code, 502)
            raise APIError(status)
        value = result.get('structuredContent')
        if not isinstance(value, dict):
            raise Blocked('Remote tool did not return its structured result')
        return value

    def tool(self, name, arguments, *, wire=False, before_send=None):
        """Pin a descriptor per invocation; preserve original intent across retries."""
        if name == 'pantheon_command' and arguments.get('op') == 'handback':
            raise Blocked('This connector has no verified native handback receipt. '
                          'Confirm the shared result, then return it in the native conversation.')
        from ..onboarding import private_directory, private_lock
        self.allowance()
        self.catalog.refresh()
        descriptor = self.catalog.lookup(name)
        from .permission_contract import require
        require(self.config, descriptor, arguments)
        if descriptor['execution'] != 'remote' or arguments.get('connection') != self.local_connection:
            raise Blocked('Remote tool requires this exact local connection')
        arguments = copy.deepcopy(arguments)
        try:
            encoded = json.dumps(arguments, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            raise Blocked('Invalid remote tool JSON arguments') from None
        if len(encoded) > 60 * 1024:
            raise Blocked('Remote tool arguments exceed their byte limit')
        schema = descriptor['wireInputSchema' if wire else 'inputSchema']
        if (set(arguments) - set(schema.get('properties', {}))
                or set(schema.get('required', [])) - set(arguments)):
            raise Blocked('Invalid remote tool arguments')
        if not descriptor['mutating']:
            return self._call(descriptor, arguments)
        key = arguments.get('key')
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise Blocked('A stable operation key is required')
        directory = private_directory(self.path.with_suffix('.mcp-operations'))
        identity = hashlib.sha256(key.encode()).hexdigest()
        path = directory / (identity + '.json')
        intent = {'scope': self.scope, 'tool': name, 'contract': descriptor['contract'], 'arguments': arguments}
        with private_lock(directory / (identity + '.lock')):
            self.allowance()
            if path.exists() or path.is_symlink():
                saved = load_private(path)
                if saved['intent'] != intent:
                    raise Blocked('Operation key already belongs to different arguments or contract')
                payload = saved['wire']
            else:
                payload = copy.deepcopy(arguments)
                if not wire and {'epoch', 'config'} <= set(descriptor['wireInputSchema']['properties']):
                    view = self.tool('pantheon_chat', {'connection': self.local_connection})
                    payload.update(epoch=view['chat']['epoch'], config=view['chat']['config'])
                    if arguments.get('op') in ('definition', 'result', 'confirm'):
                        if view.get('completion_protocol') != COMPLETION_PROTOCOL:
                            raise APIError(409, 'completion_protocol_unavailable')
                        payload['completion_protocol'] = COMPLETION_PROTOCOL
                saved = {'intent': intent, 'wire': payload, 'status': 'pending'}
                save_private(path, saved)  # Before the first mutating request.
            if before_send:
                before_send(payload)
            value = self._call(descriptor, payload)
            # Retry always reaches the server for current authority and its durable
            # outcome. Do not replay a stale private success after revocation.
            save_private(path, {**saved, 'status': 'confirmed'})
            return value

    def command(self, op, *, key=None, before_send=None, **fields):
        if op not in REMOTE_COMMANDS:
            return super().command(op, key=key, before_send=before_send, **fields)
        name, arguments = self._command_tool({'op': op, 'key': key or secrets.token_urlsafe(24), **fields})
        return self.tool(name, arguments, before_send=before_send)

    def _command_tool(self, payload):
        arguments = dict(payload, connection=self.local_connection)
        op = arguments['op']
        if op in ('post', 'ack'):
            arguments.pop('op')
            if op == 'ack':
                arguments['message_ids'] = arguments.pop('ids')
            return 'pantheon_' + op, arguments
        return 'pantheon_command', arguments

    def request(self, path, payload=None):
        parsed = urlsplit(path)
        args = {'connection': self.local_connection}
        if payload is None and path == '/v1/chat':
            return self.tool('pantheon_chat', args)
        if payload is None and parsed.path == '/v1/history':
            query = parse_qs(parsed.query, strict_parsing=True)
            if set(query) - {'cursor'} or any(len(v) != 1 for v in query.values()):
                raise Blocked('Invalid history cursor')
            if 'cursor' in query:
                args['cursor'] = query['cursor'][0]
            return self.tool('pantheon_history', args)
        if payload is None and parsed.path.startswith('/v1/messages/') and not parsed.query:
            suffix = parsed.path.removeprefix('/v1/messages/').split('/')
            if len(suffix) == 1 or len(suffix) == 2 and suffix[1] == 'receipts':
                return self.tool('pantheon_receipts' if len(suffix) == 2 else 'pantheon_message',
                                 {**args, 'message_id' if len(suffix) == 2 else 'id': unquote(suffix[0])})
        if parsed.path == '/v1/commands' and payload and payload.get('op') in REMOTE_COMMANDS:
            name, arguments = self._command_tool(payload)
            return self.tool(name, arguments, wire=True)
        if path == '/v1/sites/current' and payload is None or path == '/v1/sites/current/reconcile' and payload == {}:
            return self.tool('pantheon_site_status', {**args, 'reconcile': payload is not None})
        # Claims, native receipts, inspection and payload primitives retain their
        # explicit old routes. Unknown catalogue operations never enter this path.
        self.allowance()
        return super().request(path, payload)
