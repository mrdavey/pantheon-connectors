"""Plugin-owned stock CLI tools and bounded receiver; inert until approved setup.

Native identity comes only from tools/call _meta, never model arguments or a
process-global thread env. EOF/uninstall stops receivers without interrupting
accepted native turns. No account, owner launch, resume or implicit approval.
"""
import json
import sys
import threading
import secrets
import time
from .codex_audit import Audit, failure
from .version import CODEX_VERSION
from .codex_daemon import native_identity
from .codex_delivery import DEFAULT_DAEMON, preparation, selected
from .codex_setup import prepare
from .codex_output_watch import run
from .native_cli import execute, binding_for
from .onboarding import TOOLS
from .state import load_private
from .worker import Blocked

PREPARE = dict(name='pantheon_prepare', description=(
    'Prepare DISABLED setup after explicit approval to inspect this stock CLI daemon. '
    'Daemon API access is broader than one thread; this plugin binds the calling conversation. '
    'No invitation redemption, runtime grant or model turn.'), inputSchema=dict(type='object',
    properties={'url': {'type': 'string'}, 'inspect_default_daemon': {'type': 'boolean'}},
    required=['url', 'inspect_default_daemon'], additionalProperties=False))
WAKE_TOOLS = [dict(name=name, description='Use only connection/wake_id from actual native tool-output arrival.' +
    (' After processing input and finishing this wake, end the native turn if waiting for a peer. Do not sleep or poll; the next incoming notice starts another turn.' if name == 'pantheon_finish_wake' else ''),
    inputSchema=dict(type='object', properties={key: {'type': 'string'} for key in ('connection', 'wake_id')},
    required=['connection', 'wake_id'], additionalProperties=False)) for name in ('pantheon_begin_wake', 'pantheon_finish_wake')]


class Server:
    def __init__(self, setup_file):
        self.setup_file = setup_file
        self.workers = {}
        self.status = {}
        self.lock = threading.Lock()
        self.closed = False
        self.audit = Audit(setup_file)

    def start(self, connection):
        with self.lock:
            if self.closed:
                raise Blocked('Plugin is closing')
            previous = self.workers.get(connection)
            if previous:
                # Failed/expired workers require a new explicit join/recovery,
                # but an uncertain journal is never reset or replayed.
                if previous[1].is_alive():
                    return
            self.audit.emit('receiver_started', connection=connection)
            stop = threading.Event()
            def report(value):
                with self.lock:
                    changed = self.status.get(connection) != value
                    self.status[connection] = value
                if changed:
                    self.audit.emit('receiver_status', connection=connection, **{k:v for k,v in value.items() if k in ('status','wake_id','turn_id')})
            def work():
                try:
                    run(self.setup_file, connection, stop, report=report, audit=self.audit)
                    with self.lock:
                        self.status[connection] = dict(status='stopped')
                    self.audit.emit('receiver_stopped', connection=connection, status='stopped')
                except Exception as error:
                    with self.lock:
                        self.status[connection] = dict(status='stopped', **failure(error))
                    try:
                        self.audit.emit('receiver_stopped', connection=connection, **failure(error))
                    except Blocked:
                        with self.lock:
                            self.status[connection] = dict(status='stopped', reason='audit_unavailable')
            worker = threading.Thread(target=work, daemon=True)
            self.workers[connection] = (stop, worker)
            worker.start()

    def handle(self, method, params):
        if method != 'tools/call':
            return self._handle(method, params)
        params = params if isinstance(params, dict) else {}
        args = params.get('arguments')
        args = args if isinstance(args, dict) else {}
        name = params.get('name')
        from .codex_audit import TOOLS as AUDIT_TOOLS
        metadata = params.get('_meta')
        context = dict(request_id=secrets.token_hex(16),
                       tool=name if isinstance(name, str) and name in AUDIT_TOOLS else 'unknown',
                       thread=metadata.get('threadId') if isinstance(metadata, dict) else None,
                       connection=args.get('connection'), wake_id=args.get('wake_id'),
                       message_ids=args.get('message_ids') if name=='pantheon_ack' else None)
        started = time.monotonic()
        self.audit.emit('tool_requested', **context)
        try:
            result = self._handle(method, params)
        except Exception as error:
            self.audit.emit('tool_failed', **context, duration_ms=int((time.monotonic()-started)*1000), **failure(error))
            raise
        value = result.get('structuredContent', {})
        if name == 'pantheon_receive':
            context['message_ids'] = [item['untrusted_member_content']['message']['id'] for item in value.get('messages', [])]
        self.audit.emit('tool_completed', **{**context, 'connection': value.get('connection', context['connection'])},
                        duration_ms=int((time.monotonic()-started)*1000),
                        message_id=value.get('id'), message_count=len(value.get('messages', [])),
                        ack_count=len(args.get('message_ids', [])) if name=='pantheon_ack' else 0)
        return result

    def _handle(self, method, params):
        if method == 'initialize':
            return dict(protocolVersion=params.get('protocolVersion', '2025-06-18'),
                        serverInfo=dict(name='pantheon', version=CODEX_VERSION), capabilities={'tools': {}})
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': [PREPARE] + TOOLS + WAKE_TOOLS}
        if method != 'tools/call':
            raise Blocked('Unsupported plugin method')
        thread = native_identity(params.get('_meta'))
        name, args = params.get('name'), params.get('arguments', {})
        preparation(DEFAULT_DAEMON, self.setup_file)
        if name == 'pantheon_prepare':
            if (not isinstance(args, dict) or set(args) != {'url', 'inspect_default_daemon'}
                    or type(args['url']) is not str or args['inspect_default_daemon'] is not True):
                raise Blocked('Explicit daemon inspection consent is required')
            value = prepare(self.setup_file, args['url'], 'default', delivery=DEFAULT_DAEMON,
                            native_thread=thread, inspect_default_daemon=True)
        else:
            config = load_private(self.setup_file)
            if selected(config) != DEFAULT_DAEMON:
                raise Blocked('Plugin refuses another delivery mode')
            binding_for(config, thread)
            value = execute(config, name, args, native_thread=thread, setup_file=self.setup_file)
            if name == 'pantheon_join' and value.get('connection'):
                self.start(value['connection'])
            if name == 'pantheon_command' and args.get('op') == 'withdraw':
                worker = self.workers.get(args['connection'])
                if worker:
                    worker[0].set()
            if name == 'pantheon_doctor':
                with self.lock:
                    value['receivers'] = dict(self.status)
                value['audit'] = dict(path=str(self.audit.path), format='jsonl', retention='current plus one rotated file, 1 MiB each; local only')
                value['daemon_access_scope'] = 'Local daemon API is broader than this approved thread; plugin enforces its binding and lifetime.'
        return dict(content=[dict(type='text', text=json.dumps(value))], structuredContent=value)

    def close(self):
        with self.lock:
            self.closed = True
            workers = list(self.workers.values())
            for stop, _ in workers:
                stop.set()
        for _, worker in workers:
            worker.join(timeout=2)
        if self.audit.has_written:
            try: self.audit.emit('plugin_closed')
            except Blocked: print('Pantheon audit unavailable during shutdown.', file=sys.stderr)


def serve(setup_file=None):
    if setup_file is None:
        from .codex_hosted import HostedServer
        server = HostedServer()
    else:
        server = Server(setup_file)
    output_lock = threading.Lock()

    def write(value):
        with output_lock:
            print(json.dumps(value), flush=True)

    from .mcp_elicitation import Input
    incoming = Input(sys.stdin, write)
    server.elicit = incoming.elicit

    if hasattr(server, 'start_notifications'):
        server.start_notifications(lambda: write({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}))
    try:
        for line in incoming:
            request = {}
            try:
                request = json.loads(line)
                if 'id' not in request or 'method' not in request:
                    continue
                incoming.request_id = request['id']
                if request.get('method') == 'initialize':
                    incoming.initialize(request.get('params', {}))
                result = server.handle(request.get('method'), request.get('params', {}))
                response = dict(jsonrpc='2.0', id=request['id'], result=result)
            except Blocked as error:
                response = dict(jsonrpc='2.0', id=request.get('id'), error=dict(code=-32602, message=str(error)))
            except Exception as error:
                details = failure(error)
                from .codex_audit import TOOLS as DIAGNOSTIC_TOOLS
                params = request.get('params')
                if isinstance(params, dict) and isinstance(params.get('name'), str) and params['name'] in DIAGNOSTIC_TOOLS:
                    details['tool'] = params['name']
                reason = details['reason']
                messages = {
                    'local_file_missing': 'Required local Pantheon state is missing. Check preparation for this exact invitation and native conversation; do not copy another connection or retry joining blindly.',
                    'local_file_access_denied': 'The native plugin cannot read its local Pantheon state. Preserve the files and inspect native filesystem access; do not broaden permissions automatically.',
                    'local_symlink_refused': 'Pantheon refused a symlink where a private state file was required. Preserve state and inspect the installation; do not bypass this check.',
                    'tls_verification_failed': 'HTTPS certificate verification failed. Check the native Python CA trust configuration or update the Pantheon plugin; do not disable TLS verification.',
                    'tls_trust_unavailable': 'The native Python CA trust configuration could not be loaded. Repair that configuration before retrying.',
                    'network_unavailable': 'The Pantheon service could not be reached. Check network access before retrying.',
                    'network_timeout': 'The Pantheon request timed out. Check operation status before deliberately retrying.',
                    'api_error': 'Pantheon returned an HTTP error. Check the reported status before retrying.',
                }
                message = messages.get(reason, 'Pantheon encountered a local plugin error. Report this reason and source location; do not change identity or permissions to work around it.')
                response = dict(jsonrpc='2.0', id=request.get('id'), error=dict(code=-32602,
                    message=message, data=details))
            write(response)
    finally:
        server.close()


if __name__ == '__main__':
    serve(sys.argv[1])
