"""Explicit bounded shared-owner supervisor; no automatic owner launch or reconnect."""
import argparse
import json
import signal
import threading
import time
from .codex_output import OutputNotices, Owner
from .codex_daemon import owner_for
from .state import load_private
from .worker import Blocked
from .client import APIError
from .read_retry import ReadRetry
from .runtime_policy import same_scope


def run(setup_file, connection, stop, report=None, audit=None):
    initial = load_private(setup_file)
    def unchanged():
        return not stop.is_set() and same_scope(load_private(setup_file), initial)
    # Validate mode and grant before opening even the selected owner connection.
    notices = OutputNotices(initial, unchanged=unchanged, audit=audit)
    notices.allowed()
    from .codex_delivery import DEFAULT_DAEMON
    owner = None
    retry = ReadRetry()
    next_idle_close_check = 0
    try:
        while not stop.is_set():
            notices.allowed()
            try:
                if owner is None:
                    if initial.get("delivery") == DEFAULT_DAEMON:
                        notices.pending(connection)  # Validate membership before attaching.
                        notices.allowed()
                    owner = owner_for(initial) if initial.get("delivery") == DEFAULT_DAEMON else Owner(initial)
                    notices.owner = owner
                if not owner.rpc.pending.empty():
                    raise Blocked('Native request needs a supported host handler; never auto-approve')
                value = notices.tick(connection)
                # Finite grants also end on completion. Leave an accepted
                # notice alone until its explicit drain finishes, then fence
                # this scope durably. Throttle the extra idle status read.
                now = time.monotonic()
                if (not stop.is_set() and value.get('status') == 'idle'
                        and now >= next_idle_close_check):
                    next_idle_close_check = now + 5
                    if notices.fence_closed_idle(connection):
                        from .native_cli import fence_hosted_runtime
                        fenced = fence_hosted_runtime(setup_file, initial, connection)
                        value = dict(status='closed', private_runtime_disabled=fenced)
                        if report is not None:
                            report(value)
                        break
                retry.reset()
                delay = 1
            except APIError as error:
                delay = retry.delay(error)
                value = dict(status='transport_retry', failures=retry.failures, retry_seconds=delay)
            if report is None:
                print(json.dumps(value), flush=True)
            else:
                report(value)
            stop.wait(delay)
    finally:
        if owner is not None:
            owner.close()  # Only our client, never owner/TUI/active turns.
        end = initial.get('trial_expires_at')
        if type(end) in (int, float) and 0 < end <= time.time():
            try:
                from .onboarding import Onboarding
                from .lifecycle_status import report_terminal
                report_terminal(Onboarding(initial), connection, initial['binding'], 'time_ran_out', str(end))
            except Exception as error:
                if audit is not None:
                    audit.record('receiver_state', outcome='terminal_status_pending', error=error)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('setup_file'); parser.add_argument('connection')
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(args.setup_file, args.connection, stop)


if __name__ == '__main__':
    try: main()
    except Exception:
        raise SystemExit('Tool-output bridge stopped; preserve its journal. No replay, owner restart or user-role fallback.') from None
