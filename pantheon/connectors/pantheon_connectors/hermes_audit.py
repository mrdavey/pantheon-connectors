"""Bounded, local metadata diagnostics. Never a substitute for delivery receipts."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import uuid

EVENTS = {"tool_started", "tool_finished", "tool_error", "notice_attempt", "notice_result", "limits_observed",
          "receiver_started", "receiver_state", "receiver_stopped", "runtime_fenced", "plugin_unloaded"}
OPERATIONS = {"pantheon_prepare", "pantheon_preview", "pantheon_join", "pantheon_doctor", "pantheon_receive", "pantheon_ack",
              "pantheon_post", "pantheon_command", "pantheon_status", "pantheon_history", "pantheon_receipts",
              "pantheon_resume", "ready", "withdraw", "handback", "report", "result", "confirm"}
OPERATIONS |= {'pantheon_transfer_send', 'pantheon_transfer_inspect', 'pantheon_transfer_fetch', 'pantheon_transfer_ack'}
OUTCOMES = {"completed", "blocked", "error", "stale_notice_noop", "scheduled_not_received",
            "outcome_unknown", "native_host_unavailable", "idle_or_notice_unresolved", "legacy_notice_unresolved",
            "trial_expired", "notice_budget_exhausted", "receiver_stopped", "plugin_unloaded",
            "blocked_check_setup_binding_and_native_host", "runtime_fenced_host_cleanup_required", "runtime_not_authorized",
            "journal_busy_retry", "transport_retry", "terminal_notice_unavailable",
            "terminal_notice_already_attempted", "terminal_notice_scheduled_not_received",
            "terminal_notice_native_host_unavailable"}


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


class Audit:
    """Best-effort append with nonblocking locks; failures remain visible locally.

    Two 1 MiB segments at most. No directory creation, network, raw arguments,
    exception messages, capabilities or host session IDs are written.
    """
    def __init__(self, setup_file, *, max_bytes=1024*1024):
        self.setup = Path(setup_file)
        self.path = self.setup.with_suffix(".audit.jsonl")
        self.max_bytes = max_bytes
        self.instance = uuid.uuid4().hex
        self.sequence = self.written = self.failed = self.rotations = 0
        self.last_failure = None
        self.lock = threading.Lock()

    def health(self):
        return dict(format="pantheon.hermes-audit.v1", file=str(self.path), instance=self.instance,
                    written=self.written, failed=self.failed, rotations=self.rotations,
                    max_segment_bytes=self.max_bytes, retained_segments=2,
                    last_failure=self.last_failure,
                    status="degraded" if self.failed else "available" if self.written else "not_written")

    @staticmethod
    def open_private(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("Audit target is not a private regular file")
        return fd

    def record(self, event, *, operation=None, command=None, outcome=None, connection=None, session=None,
               request=None, key=None, notice_token=None, message_ids=(), duration_ms=None,
               count=None, http_status=None, error=None, limits=None):
        try:
            if not self.setup.exists():
                if self.sequence:
                    self.failed += 1
                    self.last_failure = dict(kind="setup_missing")
                return  # Registration/preflight without setup must remain side-effect free.
        except OSError as error:
            self.failed += 1
            self.last_failure = dict(kind="setup_io", errno=error.errno)
            return
        if not self.lock.acquire(blocking=False):
            self.failed += 1
            self.last_failure = dict(kind="writer_busy")
            return
        try:
            self.sequence += 1
            if event not in EVENTS:
                raise ValueError("Unknown audit event")
            row = dict(schema="pantheon.hermes-audit.v1", at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       instance=self.instance, sequence=self.sequence, event=event, connector_version="0.3.18")
            if isinstance(operation, str) and operation in OPERATIONS: row["operation"] = operation
            if isinstance(command, str) and command in OPERATIONS: row["command"] = command
            if isinstance(outcome, str) and outcome in OUTCOMES: row["outcome"] = outcome
            for name, value in (("connection", connection), ("session", session), ("request", request),
                                ("key", key), ("notice", notice_token)):
                if isinstance(value, str): row[name+"_sha256"] = fingerprint(value)
            if isinstance(message_ids, (list, tuple)):
                row["message_ids_sha256"] = [fingerprint(x) for x in message_ids[:32] if isinstance(x, str)]
                row["message_id_count"] = len(message_ids)
            for name, value in (("duration_ms", duration_ms), ("count", count), ("http_status", http_status)):
                if type(value) is int and 0 <= value <= 10**12: row[name] = value
            if isinstance(limits, dict):
                policy = limits.get("notification_policy", {})
                if isinstance(policy, dict) and policy.get("mode") in ("limited", "unlimited"):
                    row["notification_mode"] = policy["mode"]
                for key in ("duration_seconds", "notice_limit", "trial_expires_at"):
                    value = limits.get(key)
                    if type(value) in (int, float) and 0 <= value <= 10**12: row[key] = value
                if type(limits.get("runtime_authorized")) is bool:
                    row["runtime_authorized"] = limits["runtime_authorized"]
            if error is not None:
                # Fingerprints permit matching repeated errors without retaining
                # exception text (which can contain URLs, tokens or peer content).
                row["error_sha256"] = fingerprint(str(error))
                row["error_kind"] = type(error).__name__ if type(error).__name__ in {
                    "APIError", "Blocked", "SessionObservationPending", "OSError", "TimeoutError",
                    "ValueError", "KeyError", "TypeError", "JournalBusy", "RuntimeError", "PermissionError",
                    "FileNotFoundError", "ConnectionError", "JSONDecodeError", "OperationalError", "IntegrityError"} else "unexpected"
                trace = error.__traceback__
                while trace:
                    source = Path(trace.tb_frame.f_code.co_filename)
                    if source.parent == Path(__file__).parent:
                        row["error_site"] = dict(module=source.name, line=trace.tb_lineno)
                    trace = trace.tb_next
            data = (json.dumps(row, sort_keys=True)+"\n").encode()
            parent = self.path.parent
            if any(p.is_symlink() for p in (parent, *parent.parents)):
                raise ValueError("Audit directory must not contain symlinks")
            info = parent.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or len(data) > self.max_bytes:
                raise ValueError("Audit directory or event is unsuitable")
            lockfd = self.open_private(str(self.path)+".lock")
            try:
                fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = self.open_private(self.path)
                try:
                    if os.fstat(fd).st_size + len(data) > self.max_bytes:
                        backup = Path(str(self.path)+".1")
                        if backup.exists() or backup.is_symlink():
                            previous = self.open_private(backup); os.close(previous)
                        os.replace(self.path, backup)
                        os.close(fd); fd = None
                        fd = self.open_private(self.path)
                        self.rotations += 1
                    if os.write(fd, data) != len(data): raise OSError("Incomplete audit write")
                    os.fsync(fd)
                finally:
                    if fd is not None: os.close(fd)
            finally:
                os.close(lockfd)
            self.written += 1
            return True
        except Exception as error:
            # Audit failure must not prevent expiry, withdrawal or cancellation.
            self.failed += 1
            kind = "writer_busy" if isinstance(error, BlockingIOError) else "io_error" if isinstance(error, OSError) else "invalid_target_or_event"
            self.last_failure = dict(kind=kind, errno=error.errno if isinstance(error, OSError) else None)
        finally:
            self.lock.release()
