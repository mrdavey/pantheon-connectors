"""Opt-in tool-output bridge for an explicitly selected existing Unix owner.

Never launches/resumes an owner, changes models, sends user input, or interrupts a
turn. An accepted turn may already contain work owned by another client.
"""
import fcntl
import json
import os
import pathlib
import secrets
import stat
import time
import uuid
from contextlib import contextmanager
from .codex import AttachedAppServer
from .onboarding import Onboarding, private_directory, trial_allowed
from .state import load_private, save_private
from . import notification_policy
from .worker import Blocked
from .codex_delivery import selected, DEFAULT_DAEMON

SURFACE = 'app-server-tool-output'


@contextmanager
def notice_lock(path, check=lambda: None):
    """Bound contention and let a stopped supervisor leave without the holder."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Blocked('Notice lock must be an owner-only regular file')
        deadline = time.monotonic() + 5
        while True:
            check()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Blocked('Notice journal is busy; no delivery attempted') from None
                time.sleep(0.05)
        check()
        yield
    finally:
        os.close(fd)


def socket_identity(value):
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise Blocked('Select an absolute non-symlink owner socket')
    info, parent = path.lstat(), path.parent.stat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise Blocked('The selected socket and its immediate directory must be owner-only')
    for ancestor in path.parents:
        directory = ancestor.stat()
        if (directory.st_uid not in (0, os.getuid()) or
                directory.st_mode & 0o022 and not directory.st_mode & stat.S_ISVTX):
            raise Blocked('Owner socket ancestors must protect directories from replacement')
    return dict(device=info.st_dev, inode=info.st_ino, created_ns=info.st_ctime_ns)


class Owner:
    def __init__(self, config):
        self.config = config
        if selected(config) == DEFAULT_DAEMON and type(self) is Owner:
            raise Blocked('Default-daemon mode requires its validated discovery path')
        if config.get('binding', {}).get('surface') != SURFACE or config['binding'].get('runtime') != 'codex':
            raise Blocked('An explicit tool-output owner binding is required')
        self.rpc = None
        self.verify_socket()
        self.rpc = AttachedAppServer(config['app_server_socket'], config['binding']['version'],
                                     config['binding']['profile'], config['codex_executable'])
        try:
            self.check()
        except Exception:
            self.close()
            raise

    def verify_socket(self):
        if socket_identity(self.config['app_server_socket']) != self.config['owner_socket_identity']:
            raise Blocked('Selected owner socket changed; do not reconnect to a replacement')

    def check(self):
        binding = self.config['binding']
        self.verify_socket()
        self.rpc.require_profile(binding['profile'])
        layers = self.rpc.call('config/read', {'includeLayers': True}).get('layers', [])
        homes = {str(pathlib.Path(layer['name']['file']).parent.resolve()) for layer in layers
                 if layer.get('name', {}).get('type') == 'user' and layer['name'].get('file')
                 and pathlib.Path(layer['name']['file']).is_absolute() and not layer.get('disabledReason')}
        if str(pathlib.Path(self.config['codex_home']).resolve()) not in homes:
            raise Blocked('Selected owner config provenance differs from the pinned Codex home')
        self.rpc.require_loaded(binding['session'])
        thread = self.rpc.call('thread/read', {'threadId': binding['session'], 'includeTurns': False})['thread']
        if thread.get('id') != binding['session'] or thread.get('status', {}).get('type') not in ('idle', 'active'):
            raise Blocked('Exact loaded thread is unavailable in the selected owner')
        self.rpc.thread = binding['session']
        self.verify_socket()
        return thread['status']['type']

    def send(self, output):
        # No permissions, model or provider overrides, fake call ID, user input,
        # or destructive cleanup. Output is the real authenticated pending check.
        self.verify_socket()
        return self.rpc.call('turn/start', {'threadId': self.config['binding']['session'], 'input': [],
            'toolOutput': {'name': 'pantheon_pending_notification', 'namespace': 'pantheon', 'output': output}})

    def close(self):
        if self.rpc is not None:
            self.rpc.close()


class OutputNotices:
    def __init__(self, config, *, service=None, owner=None, unchanged=lambda: True, audit=None):
        self.config, self.binding, self.owner = config, config['binding'], owner
        self.audit = audit
        if self.binding.get('runtime') != 'codex' or self.binding.get('surface') != SURFACE:
            raise Blocked('Explicit app-server-tool-output binding required')
        uuid.UUID(self.binding['session'])
        self.service = service or Onboarding(config)
        self.unchanged = unchanged
        self.directory = private_directory(config['state_dir'])
        self.path = self.directory / 'codex-output-notices.json'
        self.lock = self.directory / 'codex-output-notices.lock'

    def allowed(self):
        selected(self.config)
        if (not self.unchanged() or not trial_allowed(self.config)
                or self.config.get('allow_tool_output') is not True
                or self.config.get('allow_ui_fallback') is True
                or self.config.get('allow_active_wait') is True):
            raise Blocked('Explicit unchanged tool-output scope required; no fallback')
        if selected(self.config) == DEFAULT_DAEMON and self.config.get("allow_daemon_access") is not True:
            raise Blocked("Default-daemon access is not approved")
        notification_policy.policy(self.config, 'codex')

    def read(self):
        scope = {key:self.config.get(key) for key in ('url', 'codex_home', 'codex_executable',
                'app_server_socket', 'owner_socket_identity', 'trial_expires_at', 'max_notices')}
        if selected(self.config) == DEFAULT_DAEMON:
            scope.update(delivery=DEFAULT_DAEMON, daemon_endpoint=self.config.get('daemon_endpoint'),
                         reviewed_provider=self.config.get('reviewed_provider'))
            if 'reviewed_account' in self.config:
                scope['reviewed_account'] = self.config['reviewed_account']
        if 'runtime_policy' in self.config:
            scope.update(runtime_policy=self.config['runtime_policy'], runtime_started_at=self.config.get('runtime_started_at'), connection_runtime_supported=self.config.get('connection_runtime_supported'))
        normalized = notification_policy.policy(self.config, 'codex')
        scope.pop('max_notices')
        scope['notification_policy'] = normalized
        data = load_private(self.path) if self.path.exists() else dict(binding=self.binding, scope=scope, notices={}, withdrawn=[])
        saved_scope = dict(data.get('scope', {}))
        if 'notification_policy' not in saved_scope:
            saved_scope['notification_policy'] = notification_policy.policy(saved_scope, 'codex')
            saved_scope.pop('max_notices', None)
        if data.get('binding') != self.binding or saved_scope != scope:
            raise Blocked('Tool-output journal scope changed; preserve it')
        return data

    def pending(self, connection):
        _, _, client = self.service.member(connection, self.binding)
        if self.config.get('runtime_policy') == {'mode':'connection'}:
            view = client.request('/v1/chat')
            chat = view.get('chat', {})
            member = chat.get('members', {}).get(view.get('member'), {})
            if (chat.get('state') not in ('waiting', 'active') or member.get('revoked') is not False
                    or member.get('binding') != self.binding):
                raise Blocked('Connection ended, revoked or rebound; no runtime delivery')
        envelopes = client.request('/v1/pending')
        if any(e.get('protocol') != 'pyros.v1' or e.get('binding') != self.binding for e in envelopes):
            raise Blocked('Unread input changed membership binding or protocol')
        return [e['message']['id'] for e in envelopes]

    def fence_closed_idle(self, connection):
        """Fence a drained closed membership without interrupting accepted work.

        An empty pending queue alone does not prove closure. Recheck the native
        journal under its delivery lock and use the authenticated room view;
        never turn a transient read failure into terminal evidence.
        """
        self.allowed()
        with notice_lock(self.lock, self.allowed):
            data = self.read()
            if any(n['connection'] == connection and n['state'] != 'completed'
                   for n in data['notices'].values()):
                return False
            _, _, client = self.service.member(connection, self.binding)
            view = client.request('/v1/chat')
            self.allowed()
            chat = view.get('chat', {})
            if chat.get('state') != 'closed':
                return False
            member = chat.get('members', {}).get(view.get('member'), {})
            if (view.get('role') != 'agent' or member.get('revoked') is not False
                    or member.get('binding') != self.binding):
                raise Blocked('Closed room does not match the approved membership')
            # Closure can commit after tick's empty-inbox read. Its final
            # delivery must still drain before this local receiver is fenced.
            if self.pending(connection):
                return False
            self.allowed()
            if connection not in data['withdrawn']:
                data['withdrawn'].append(connection)
                save_private(self.path, data)
            return True

    def tick(self, connection):
        self.allowed()
        with notice_lock(self.lock, self.allowed):
            self.allowed()
            data = self.read()
            approved_policy = notification_policy.policy(self.config, 'codex')
            if connection in data['withdrawn']:
                raise Blocked('Membership fenced for withdrawal')
            pending = self.pending(connection)
            active = [(key,n) for key,n in data['notices'].items() if n['connection'] == connection and n['state'] != 'completed']
            if active:
                identity, entry = active[0]
                if entry['state'] in ('attempting', 'uncertain'):
                    raise Blocked('Uncertain output: no replay; native begin/finish can reconcile actual arrival')
                entry['messages'] = list(dict.fromkeys(entry['messages'] + pending))
                save_private(self.path, data)
                return dict(status='outstanding', wake_id=identity, turn_id=entry.get('turn_id'))
            seen = {m for n in data['notices'].values() if n['connection']==connection for m in n['messages']}
            if not set(pending)-seen:
                return dict(status='idle')
            if notification_policy.exhausted(self.config, 'codex', len(data['notices'])):
                raise Blocked('Tool-output attempt budget exhausted')
            if self.owner is None:
                raise Blocked('No explicit owner client; no automatic launch')
            before = self.owner.check()
            self.allowed()
            self.read()
            pending = self.pending(connection)  # Revocation/consumption during preflight.
            self.allowed()
            if not set(pending)-seen:
                return dict(status='idle')
            if notification_policy.policy(self.config, 'codex') != approved_policy:
                raise Blocked('Notification policy changed during delivery preflight')
            identity = secrets.token_hex(16)
            output = json.dumps(dict(connection=connection, wake_id=identity, has_unread=True), sort_keys=True)
            entry = dict(connection=connection, messages=pending, state='attempting', attempted_at=time.time(),
                         owner_state_before=before, output=output)
            data['notices'][identity] = entry
            save_private(self.path, data)  # Crash/timeout fence BEFORE any invocation.
            try:
                self.allowed()
                if self.audit:
                    self.audit.emit('delivery_attempt', thread=self.binding['session'], connection=connection,
                                    wake_id=identity, owner_state=before, notification_mode=approved_policy['mode'], notices_used=len(data['notices']))
                response = self.owner.send(output)
                turn_id = response['turn']['id']
                if not isinstance(turn_id, str) or not turn_id:
                    raise ValueError('Missing native turn acceptance')
            except Exception as error:
                entry['state'] = 'uncertain'
                save_private(self.path, data)
                if self.audit:
                    from .codex_audit import failure
                    self.audit.emit('delivery_uncertain', thread=self.binding['session'], connection=connection,
                                    wake_id=identity, **failure(error))
                raise Blocked('Tool-output outcome uncertain; no retry or user-role fallback') from None
            entry.update(state='accepted', turn_id=turn_id, accepted_at=time.time())
            save_private(self.path, data)
            if self.audit:
                self.audit.emit('delivery_accepted', thread=self.binding['session'], connection=connection,
                                wake_id=identity, turn_id=turn_id)
            return dict(status='accepted_not_received', wake_id=identity, turn_id=turn_id,
                        notification_usage=notification_policy.describe(self.config, 'codex', len(data['notices'])))

    def wake(self, connection, identity, *, finish=False, native_thread=None):
        self.allowed()
        if (native_thread if native_thread is not None else os.environ.get('CODEX_THREAD_ID')) != self.binding['session']:
            raise Blocked('Lifecycle call is outside the selected native thread')
        with notice_lock(self.lock, self.allowed):
            self.allowed()
            data = self.read()
            if connection in data['withdrawn']:
                raise Blocked('Membership fenced for withdrawal')
            entry = data['notices'].get(identity)
            if not entry or entry['connection'] != connection:
                raise Blocked('Unknown tool-output wake')
            pending = self.pending(connection)
            if finish and entry['state'] == 'completed':
                return dict(status='completed', owns_turn=False)
            if finish and entry['state'] != 'draining':
                raise Blocked('Begin the actually received wake before finishing')
            if not finish and entry['state'] == 'completed':
                raise Blocked('Completed wake cannot be replayed')
            entry['messages'] = list(dict.fromkeys(entry['messages'] + pending))
            if finish:
                if pending:
                    save_private(self.path, data)
                    return dict(status='drain_required', pending_count=len(pending), owns_turn=False)
                entry.update(state='completed', finished_at=time.time())
            else:
                entry.update(state='draining', native_begin_at=time.time())
            save_private(self.path, data)
            return dict(status=entry['state'], owns_turn=False)

    def fenced_wake_status(self, connection, identity, *, native_thread=None, scope_check=lambda: None):
        """Report an existing local fence without access, ACK or turn claims."""
        if (native_thread if native_thread is not None else os.environ.get('CODEX_THREAD_ID')) != self.binding['session']:
            raise Blocked('Lifecycle call is outside the selected native thread')
        grant = self.config.get('hosted_runtime_grant')
        if (selected(self.config) != DEFAULT_DAEMON
                or self.config.get('hosted_consent') is not True
                or self.config.get('invitation_connection') != connection
                or not isinstance(grant, dict) or grant.get('connection') != connection
                or any(self.config.get(flag) is not False for flag in
                       ('allow_runtime_turns', 'allow_daemon_access', 'allow_tool_output'))):
            raise Blocked('No disabled hosted scope matches this wake')
        with notice_lock(self.lock, scope_check):
            data = self.read()
            entry = data['notices'].get(identity)
            if (connection not in data['withdrawn'] or not entry
                    or entry['connection'] != connection):
                raise Blocked('No matching fenced tool-output wake')
            scope_check()
            return dict(status='connection_fenced', owns_turn=False, runtime_authorized=False,
                        daemon_inspected=False, acknowledgement_inferred=False,
                        native_turn_interrupted=False, next_action='stop_connection',
                        notice='This wake belongs to a locally fenced connection. No input was fetched, acknowledged, replayed or marked completed. Do not renew access to finish it.')

    def withdraw(self, connection, action):
        with notice_lock(self.lock):
            data = self.read()
            if connection not in data['withdrawn']:
                data['withdrawn'].append(connection)
            save_private(self.path, data)
            result = action()
            return dict(result, wake_cleanup=dict(new_wakes_fenced=True, native_turn_interrupted=False,
                next_action='stop_connection',
                limitation='Accepted input may still execute in a shared turn; no exclusive turn ownership is claimed. Return the retained result to the human and stop this connection; no further wake-finish or daemon inspection is needed.'))
