"""Shared read recovery and expiry cleanup; native delivery remains in adapters.

No native invocation belongs here. An adapter must fence an uncertain native
submission before it can reach the read-only retry boundary.
"""
import time
from .read_retry import ReadRetry
from .runtime_policy import same_scope
from .state import load_private


class ReceiverScope:
    def __init__(self, setup_file, connection, stop, audit=None):
        self.setup_file, self.connection, self.stop = setup_file, connection, stop
        self.initial = load_private(setup_file)
        self.audit = audit
        self.retry = ReadRetry()

    def unchanged(self):
        return not self.stop.is_set() and same_scope(load_private(self.setup_file), self.initial)

    def recover_read(self, error):
        delay = self.retry.delay(error)
        return dict(status='transport_retry', failures=self.retry.failures, retry_seconds=delay), delay

    def report_expiry(self):
        # Use the original lifetime and membership. Cleanup cannot renew consent,
        # adopt changed setup, ACK messages or initiate a provider turn.
        end = self.initial.get('trial_expires_at')
        if type(end) not in (int, float) or not 0 < end <= time.time():
            return
        try:
            from .onboarding import Onboarding
            from .lifecycle_status import report_terminal
            report_terminal(Onboarding(self.initial), self.connection, self.initial['binding'], 'time_ran_out', str(end))
        except Exception as error:
            if self.audit is not None:
                self.audit.record('receiver_state', outcome='terminal_status_pending', error=error)
