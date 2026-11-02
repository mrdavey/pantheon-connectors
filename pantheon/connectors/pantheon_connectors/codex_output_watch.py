"""Explicit bounded shared-owner supervisor; no automatic owner launch or reconnect."""
import argparse
import json
import signal
import threading
import time
from .codex_output import OutputNotices, Owner
from .codex_daemon import owner_for
from .worker import Blocked
from .client import APIError
from .receiver_lifecycle import ReceiverScope


def run(setup_file, connection, stop, report=None, audit=None):
    scope = ReceiverScope(setup_file, connection, stop, audit)
    initial = scope.initial
    # Validate mode and grant before opening even the selected owner connection.
    notices = OutputNotices(initial, unchanged=scope.unchanged, audit=audit)
    notices.allowed()
    from .codex_delivery import DEFAULT_DAEMON
    owner = None
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
                scope.retry.reset()
                delay = 1
            except APIError as error:
                value, delay = scope.recover_read(error)
            if report is None:
                print(json.dumps(value), flush=True)
            else:
                report(value)
            stop.wait(delay)
    finally:
        try:
            if owner is not None:
                owner.close()  # Only our client, never owner/TUI/active turns.
        finally:
            scope.report_expiry()



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
