"""Private bounded diagnostic audit, deliberately excluding payloads and secrets.

This is local troubleshooting evidence, not a tamper-proof audit ledger. A failed
write blocks new plugin operations; durable delivery journals remain authoritative.
"""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import traceback
from .onboarding import private_directory
from .worker import Blocked
from .version import CODEX_VERSION

EVENTS = {'tool_requested', 'tool_completed', 'tool_failed', 'receiver_started',
          'receiver_status', 'receiver_stopped', 'plugin_closed', 'delivery_attempt',
          'delivery_accepted', 'delivery_uncertain'}
TOOLS = {'pantheon_authorize', 'pantheon_prepare', 'pantheon_preview', 'pantheon_join', 'pantheon_doctor',
         'pantheon_receive', 'pantheon_ack', 'pantheon_post', 'pantheon_command',
         'pantheon_begin_wake', 'pantheon_finish_wake', 'pantheon_site_deploy', 'pantheon_site_status', 'unknown'}
TOOLS |= {'pantheon_transfer_send', 'pantheon_transfer_inspect', 'pantheon_transfer_fetch', 'pantheon_transfer_ack'}
STATES = {'idle', 'active', 'outstanding', 'accepted_not_received', 'stopped', 'unknown', 'transport_retry'}
REASONS = {'guard_failed', 'api_error', 'transport_error', 'state_error', 'unexpected',
           'expired', 'scope_changed', 'identity_mismatch', 'endpoint_changed',
           'uncertain_delivery', 'native_approval', 'budget_exhausted', 'withdrawn',
           'tls_verification_failed', 'tls_trust_unavailable', 'network_unavailable', 'network_timeout',
           'local_file_missing', 'local_file_access_denied', 'local_symlink_refused', 'read_retry_exhausted'}
MODULES = {p.name for p in Path(__file__).parent.glob('codex*.py')} | {'native_cli.py', 'onboarding.py', 'state.py', 'client.py'}


def failure(error):
    """Only stable categories and our code location; never exception text/locals."""
    reason = 'unexpected'
    if isinstance(error, Blocked):
        reason = 'guard_failed'
        message = str(error).lower()
        for needles, code in [
            (('read retry budget exhausted',), 'read_retry_exhausted'),
            (('expired',), 'expired'), (('scope changed', 'unchanged'), 'scope_changed'),
            (('thread', 'session', 'profile', 'binding'), 'identity_mismatch'),
            (('socket', 'endpoint', 'daemon alias', 'discovered owner'), 'endpoint_changed'),
            (('uncertain',), 'uncertain_delivery'), (('native request',), 'native_approval'),
            (('budget',), 'budget_exhausted'), (('withdraw',), 'withdrawn')]:
            if any(x in message for x in needles):
                reason = code
                break
    elif type(error).__name__ == 'APIError':
        reason = error.reason if getattr(error, 'reason', None) in REASONS else 'api_error'
    elif isinstance(error, FileNotFoundError): reason = 'local_file_missing'
    elif isinstance(error, PermissionError): reason = 'local_file_access_denied'
    elif isinstance(error, OSError) and error.errno == errno.ELOOP: reason = 'local_symlink_refused'
    elif isinstance(error, (OSError, TimeoutError, ConnectionError)): reason = 'transport_error'
    elif isinstance(error, (ValueError, KeyError, TypeError)): reason = 'state_error'
    result = {'reason': reason}
    status = getattr(error, 'status', None)
    if type(status) is int and 0 <= status <= 599: result['http_status'] = status
    frames = [f for f in traceback.extract_tb(error.__traceback__)
              if Path(f.filename).parent == Path(__file__).parent and Path(f.filename).name in MODULES]
    if frames:
        result['source'] = Path(frames[-1].filename).name
        result['line'] = frames[-1].lineno
    return result


class Audit:
    def __init__(self, setup_file, *, max_bytes=1024*1024, plugin_version=CODEX_VERSION):
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", plugin_version): raise ValueError("Invalid audit version")
        self.plugin_version = plugin_version
        self.directory = Path(setup_file).parent
        self.path = self.directory / 'codex-audit.jsonl'
        self.run = secrets.token_hex(16)
        self.max_bytes = max_bytes
        self.has_written = False

    @contextmanager
    def operation(self, tool, **context):
        context.update(tool=tool, request_id=secrets.token_hex(16))
        started = time.monotonic()
        self.emit('tool_requested', **context)
        try:
            yield
        except Exception as error:
            self.emit('tool_failed', **context, duration_ms=int((time.monotonic()-started)*1000), **failure(error))
            raise
        self.emit('tool_completed', **context, duration_ms=int((time.monotonic()-started)*1000))

    @staticmethod
    def check(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Blocked('Audit files must be private regular files')

    def emit(self, event, **values):
        if event not in EVENTS: raise ValueError('Unknown audit event')
        row = dict(schema=1, timestamp=time.time(), monotonic=time.monotonic(),
                   plugin_version=self.plugin_version, run_id=self.run, pid=os.getpid(), event=event)
        # Explicit field/value allowlist; no arbitrary error, argument or response strings.
        for key, value in values.items():
            if key in ('thread', 'connection', 'wake_id', 'turn_id', 'request_id', 'message_id'):
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                    row[key+'_sha256'] = hashlib.sha256(value.encode()).hexdigest()
            elif key == 'message_ids' and isinstance(value, list):
                row['message_ids_sha256'] = [hashlib.sha256(item.encode()).hexdigest() for item in value[:20]
                                            if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item)]
            elif key == 'tool' and isinstance(value, str) and value in TOOLS: row[key] = value
            elif key in ('status', 'owner_state') and isinstance(value, str) and value in STATES: row[key] = value
            elif key == 'notification_mode' and value in ('limited','unlimited'): row[key] = value
            elif key == 'reason' and isinstance(value, str) and value in REASONS: row[key] = value
            elif key == 'source' and isinstance(value, str) and value in MODULES: row[key] = value
            elif key in ('line', 'http_status', 'message_count', 'ack_count', 'duration_ms', 'notices_used'):
                if type(value) is int and 0 <= value <= 10**9: row[key] = value
        data = (json.dumps(row, sort_keys=True)+'\n').encode()
        try:
            private_directory(self.directory)
            lock = os.open(self.directory/'codex-audit.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
            try:
                self.check(lock)
                deadline = time.monotonic()+2
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline: raise Blocked('Audit log is busy')
                        time.sleep(.02)
                fd = os.open(self.path, os.O_CREAT|os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW, 0o600)
                try:
                    self.check(fd)
                    if os.fstat(fd).st_size+len(data) > self.max_bytes:
                        previous = self.path.with_suffix('.jsonl.1')
                        if previous.exists() or previous.is_symlink():
                            old = os.open(previous, os.O_RDONLY|os.O_NOFOLLOW)
                            try: self.check(old)
                            finally: os.close(old)
                        os.replace(self.path, previous)
                        os.close(fd)
                        fd = None
                        fd = os.open(self.path, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'ab', closefd=False) as output:
                        output.write(data); output.flush(); os.fsync(fd)
                finally:
                    if fd is not None: os.close(fd)
            finally:
                os.close(lock)
            self.has_written = True
        except Exception:
            raise Blocked('Private audit log unavailable; no new operation may proceed') from None
