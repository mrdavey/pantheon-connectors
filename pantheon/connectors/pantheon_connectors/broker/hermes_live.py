"""Portable MCP tools, authenticated by Hermes's public native pre-tool hook.

The MCP transport carries no trusted session metadata. Every call therefore needs
a one-use attestation minted inside the native host for that exact tool/input.
No model-provided session, inferred foreground session or transcript inspection.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import socketserver
import tempfile
import threading
import time

from ..onboarding import private_directory
from ..state import load_private, save_private
from ..worker import Blocked

PREFIX = 'mcp__pantheon_live__'
PROOF = '_pantheon_native'
LIMIT = 2 * 1024 * 1024
TTL = 60


def fingerprint(name, args):
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def definitions(catalogue):
    from .. import hermes_setup, hermes_tools
    from .connect import TOOL
    tools = catalogue.tools()
    names = {tool['name'] for tool in tools}
    return [TOOL, *tools, *(tool for tool in hermes_tools.TOOLS if tool['name'] not in names), *hermes_setup.TOOLS]


class NativeBridge:
    def __init__(self, plugin, home):
        self.plugin = plugin
        self.directory = private_directory(private_directory(Path(home) / 'pantheon') / 'bridges')
        self.identity = secrets.token_hex(16)
        self.secret = secrets.token_urlsafe(32)
        self.record = self.directory / (self.identity + '.json')
        # macOS Unix sockets have a short path limit; the private registration
        # lives in the profile, while only the short socket lives in /tmp.
        self.temporary = Path(tempfile.mkdtemp(prefix='pantheon-', dir='/tmp'))
        self.socket_path = self.temporary / 'native.sock'
        self.lock = threading.Lock()
        self.pending = {}
        self.closed = False
        bridge = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(30)
                try:
                    raw = self.rfile.readline(LIMIT+1)
                    if len(raw) > LIMIT or not raw.endswith(b'\n'):
                        raise Blocked('Native request exceeds the limit')
                    result = bridge.dispatch(json.loads(raw))
                except Exception:
                    result = {'status': 'blocked', 'reason': 'Native call could not be authenticated or completed; preserve state and do not replay an uncertain mutation'}
                self.wfile.write(json.dumps(result).encode()+b'\n')
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(str(self.socket_path), Handler)
        self.socket_path.chmod(0o600)
        save_private(self.record, {'socket': str(self.socket_path), 'secret': self.secret})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='pantheon-native-bridge')
        self.thread.start()

    def attest(self, tool_name='', args=None, session_id='', tool_call_id='', **_):
        if not tool_name.startswith(PREFIX):
            return None
        name = tool_name[len(PREFIX):]
        try:
            binding = self.plugin.binding(session_id)
            self.plugin.preflight(binding)
            if not isinstance(args, dict) or not tool_call_id:
                raise Blocked('Native tool call identity is unavailable')
            self.plugin.catalogue.refresh()
            if name not in {tool['name'] for tool in definitions(self.plugin.catalogue)}:
                return {'action': 'block', 'message': 'This Pantheon tool is not in the reviewed native catalogue'}
            clean = {key: value for key, value in args.items() if key != PROOF}
            digest = fingerprint(name, clean)
            with self.lock:
                now = time.monotonic()
                self.pending = {key: value for key, value in self.pending.items() if value['end'] > now}
                if self.closed or len(self.pending) >= 128:
                    raise Blocked('Native bridge is unloaded or busy')
                nonce = secrets.token_urlsafe(32)
                self.pending[nonce] = {'session': session_id, 'digest': digest, 'end': now+TTL}
            return {'action': 'modify', 'args': {PROOF: self.identity+'.'+nonce}}
        except (Blocked, ValueError):
            return {'action': 'block', 'message': 'Pantheon needs this conversation’s native identity observation; preserve it and retry only after the next native turn'}

    def dispatch(self, request):
        if not isinstance(request, dict) or not secrets.compare_digest(str(request.get('secret', '')), self.secret):
            raise Blocked('Native bridge authentication failed')
        if request.get('method') == 'active':
            return {'active': not self.closed and self.plugin.broker_active()}
        if request.get('method') != 'call' or not isinstance(request.get('args'), dict):
            raise Blocked('Unsupported native bridge operation')
        args = dict(request['args'])
        proof = args.pop(PROOF, '')
        prefix, _, nonce = str(proof).partition('.')
        with self.lock:
            if self.closed or prefix != self.identity:
                raise Blocked('Native bridge was replaced or unloaded')
            record = self.pending.pop(nonce, None)  # Consume before dispatch, including failures.
        if not record or record['end'] <= time.monotonic() or record['digest'] != fingerprint(request.get('name'), args):
            raise Blocked('Missing, expired, changed or replayed native call attestation')
        return json.loads(self.plugin.handle(request['name'], args, session_id=record['session']))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.pending.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.record.unlink(missing_ok=True)
        shutil.rmtree(self.temporary)


def request(home, identity, method, **values):
    if not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise Blocked('Missing native call attestation')
    record = load_private(Path(home) / 'pantheon' / 'bridges' / (identity+'.json'))
    path = Path(record['socket'])
    # Registration and socket are private, never a model-selected network URL.
    if (path.parent.parent != Path('/tmp') or not path.parent.name.startswith('pantheon-')
            or path.is_symlink() or path.parent.is_symlink() or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077):
        raise Blocked('Native socket is not private')
    wire = json.dumps({'secret': record['secret'], 'method': method, **values}, allow_nan=False).encode()+b'\n'
    if len(wire) > LIMIT:
        raise Blocked('Native request exceeds the limit')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(str(path))
        client.sendall(wire)
        with client.makefile('rb') as stream:
            data = stream.readline(LIMIT+1)
    if len(data) > LIMIT or not data.endswith(b'\n'):
        raise Blocked('Native response exceeds the limit; do not replay an uncertain operation')
    return json.loads(data)


def serve(home, source, target):
    """Normal host-managed stdio MCP process, without its own runtime/receiver."""
    from .adapter import ToolSnapshot
    from ..onboarding import TOOLS
    from ..hosted_discovery import TRUSTED_ORIGIN
    from ..version import HERMES_BROKER_VERSION
    catalogue = ToolSnapshot(TRUSTED_ORIGIN, TOOLS)
    write_lock = threading.Lock()
    last_bridge = ['']
    def write(value):
        with write_lock:
            target.write(json.dumps(value)+'\n')
            target.flush()
    def active():
        try:
            return bool(last_bridge[0] and request(home, last_bridge[0], 'active').get('active'))
        except Exception:
            return False
    catalogue.watch(active, lambda: write({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}))
    try:
        while line := source.readline(LIMIT+1):
            if len(line.encode()) > LIMIT or not line.endswith('\n'):
                break
            item = json.loads(line)
            if 'id' not in item:
                continue
            try:
                method, params = item.get('method'), item.get('params', {})
                if method == 'initialize':
                    result = {'protocolVersion': params.get('protocolVersion', '2025-06-18'),
                        'serverInfo': {'name': 'pantheon-native', 'version': HERMES_BROKER_VERSION},
                        'capabilities': {'tools': {'listChanged': True}}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    catalogue.refresh()
                    result = {'tools': definitions(catalogue)}
                elif method == 'tools/call':
                    args = params.get('arguments', {})
                    identity = str(args.get(PROOF, '')).partition('.')[0]
                    value = request(home, identity, 'call', name=params.get('name'), args=args)
                    last_bridge[0] = identity
                    result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value,
                              'isError': value.get('status') == 'blocked'}
                else:
                    raise Blocked('Unsupported MCP operation')
                write({'jsonrpc': '2.0', 'id': item['id'], 'result': result})
            except Exception:
                write({'jsonrpc': '2.0', 'id': item['id'], 'error': {'code': -32000,
                    'message': 'Native Pantheon call unavailable; preserve this conversation and do not replay an uncertain mutation'}})
    finally:
        catalogue.close()
