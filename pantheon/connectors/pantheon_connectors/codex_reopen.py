"""Native SessionStart handoff to the plugin, including MCP startup races.

The hook records only an exact conversation ID. It never launches a receiver,
reads a transcript, grants permission or contacts Pantheon. The already-running
MCP plugin consumes recent opens and revalidates the saved connection.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .codex_daemon import native_identity
from .onboarding import private_directory
from .state import load_private, save_private
from .worker import Blocked


def receiver_record(config, connection):
    """Nonsecret proof that this exact grant had a running daemon receiver."""
    from .codex_output import OutputNotices
    scope = OutputNotices(config).scope()
    for key in ('daemon_endpoint', 'app_server_socket', 'owner_socket_identity'):
        scope.pop(key, None)
    return dict(connection=connection, binding=config['binding'], scope=scope,
                setup_policy=config.get('codex_setup_policy'),
                grant=config.get('hosted_runtime_grant'))


def record_receiver(setup_file, config, connection):
    from .codex_delivery import DEFAULT_DAEMON, selected
    if (config.get('invitation_connection') != connection
            or selected(config) != DEFAULT_DAEMON):
        return
    save_private(Path(setup_file).with_name('receiver-recovery.json'),
                 dict(receiver_record(config, connection), endpoint=config['daemon_endpoint']))


def root():
    return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'pantheon' / 'hosted'


def record(event, directory=None):
    if event.get('hook_event_name') != 'SessionStart' or event.get('source') not in ('startup', 'resume'):
        return False  # Forks, compaction and fresh identities cannot inherit membership.
    thread = native_identity({'threadId': event.get('session_id')})
    folder = (directory or root()) / hashlib.sha256(thread.encode()).hexdigest()
    if not folder.is_dir():
        return False  # Inert for conversations that never joined.
    private_directory(folder)
    save_private(folder / 'reopen.json', dict(thread=thread, opened=time.time(), event=uuid.uuid4().hex))
    return True


class Opens:
    def __init__(self, directory, resume):
        self.directory, self.resume = Path(directory), resume
        self.stop = threading.Event()
        self.worker = None
        self.seen = {}
        self.restarts = {}

    def scan_restarts(self):
        """Restore recorded receivers after owner replacement, never load a thread.

        The native host must already have reloaded the exact conversation. A
        plugin restart at the same endpoint is not a native restart and does not
        revive stopped receivers. Failed recovery is bounded per new endpoint.
        """
        from .codex_daemon import validate_endpoint, native_home
        from .codex_delivery import DEFAULT_DAEMON, selected
        from .onboarding import Onboarding, trial_allowed
        from .client import APIError
        now = time.time()
        for path in itertools.islice(self.directory.glob('*/*/receiver-recovery.json'), 1000):
            if self.stop.is_set():
                return
            try:
                private_directory(path.parent.parent)
                private_directory(path.parent)
                config = load_private(path.with_name('setup.json'))
                saved = load_private(path)
                connection = path.parent.name
                thread = native_identity({'threadId': config['binding']['session']})
                if (selected(config) != DEFAULT_DAEMON or not trial_allowed(config)
                        or config.get('allow_daemon_access') is not True
                        or config.get('allow_tool_output') is not True
                        or config.get('invitation_connection') != connection
                        or Path(config['codex_home']).resolve() != native_home()
                        or path.parent.parent.name != hashlib.sha256(thread.encode()).hexdigest()
                        or {k:v for k,v in saved.items() if k != 'endpoint'} != receiver_record(config, connection)):
                    continue
                endpoint = validate_endpoint(native_home(), config['daemon_endpoint']['alias'])
                prior = {k:v for k,v in saved['endpoint'].items() if k != 'runtime_version'}
                if endpoint == prior:
                    continue
                key = str(path)
                identity = hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode()).hexdigest()
                attempt = self.restarts.get(key, {})
                if attempt.get('endpoint') != identity:
                    attempt = dict(endpoint=identity, tries=0, after=0)
                    self.restarts[key] = attempt
                if attempt['tries'] >= 5 or now < attempt['after']:
                    continue
                attempt['tries'] += 1
                attempt['after'] = now + 2 ** attempt['tries']
                # Validate live authority before contacting the replacement
                # native owner. No receive, ACK, redemption or model call.
                _, _, client = Onboarding(config).member(connection, config['binding'])
                view = client.request('/v1/chat')
                chat = view.get('chat', {})
                member = chat.get('members', {}).get(view.get('member'), {})
                if (chat.get('state') not in ('waiting', 'active') or view.get('role') != 'agent'
                        or member.get('revoked') is not False or member.get('binding') != config['binding']):
                    attempt['tries'] = 5
                    continue
                if self.resume(thread, connection=connection) is not False:
                    attempt['tries'] = 5
            except (Blocked, APIError, OSError, ValueError, KeyError, TypeError):
                continue

    def scan(self):
        if not self.directory.exists():
            return
        private_directory(self.directory)
        now = time.time()
        self.seen = {key: value for key, value in self.seen.items() if now - value['opened'] < 300}
        for path in itertools.islice(self.directory.glob('*/reopen.json'), 1000):
            if self.stop.is_set():
                return
            try:
                private_directory(path.parent)
                event = load_private(path)
                thread = native_identity({'threadId': event['thread']})
                if (path.parent.name != hashlib.sha256(thread.encode()).hexdigest()
                        or type(event['opened']) not in (int, float)
                        or not 0 <= now - event['opened'] < 300
                        or not isinstance(event['event'], str) or len(event['event']) != 32):
                    continue
                key = (thread, event['event'])
                attempt = self.seen.get(key, dict(opened=now, tries=0, after=0))
                if attempt['tries'] < 5 and now >= attempt['after']:
                    attempt['tries'] += 1
                    attempt['after'] = now + 2 ** attempt['tries']
                    self.seen[key] = attempt
                    if self.resume(thread) is not False:
                        attempt['tries'] = 5
            except (Blocked, OSError, ValueError, KeyError, TypeError):
                continue

    def start(self):
        if self.worker is not None:
            return
        def run():
            next_restart_check = 0
            while not self.stop.is_set():
                try:
                    self.scan()
                    if time.monotonic() >= next_restart_check:
                        next_restart_check = time.monotonic() + 5
                        self.scan_restarts()
                except Exception:
                    pass  # No exception text, credentials or native restarts.
                self.stop.wait(1)
        self.worker = threading.Thread(target=run, daemon=True, name='pantheon-reopen')
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=2)


def main():
    try:
        event = json.loads(__import__('sys').stdin.read(16384))
        record(event)
    except Exception:
        # Advisory lifecycle hook; explicit pantheon_resume remains available.
        pass
