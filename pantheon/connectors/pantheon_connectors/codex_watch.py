"""Bounded owner-started notice watcher. Never creates/resumes a Codex thread."""
import argparse
import signal
import threading
import time
from .codex_queue import Notices
from .state import load_private
from .worker import Blocked
from .runtime_policy import same_scope
from .client import APIError
from .read_retry import ReadRetry


def run(setup_file, connection, stop, report=None, audit=None):
    """Plugin-owned desktop receiver. EOF stops only this reader, never Codex."""
    initial = load_private(setup_file)
    def unchanged():
        return not stop.is_set() and same_scope(load_private(setup_file), initial)
    notices = Notices(initial, unchanged=unchanged)
    retry = ReadRetry()
    try:
        while not stop.is_set():
            notices.allowed()
            try:
                status = notices.tick(connection)
                value = dict(status=status)
                if status == 'connection_closed':
                    from .native_cli import fence_hosted_runtime
                    value['private_runtime_disabled'] = fence_hosted_runtime(setup_file, initial, connection)
                    if report:
                        report(value)
                    return
                retry.reset()
                delay = 5
            except APIError as error:
                delay = retry.delay(error)
                value = dict(status='transport_retry', failures=retry.failures, retry_seconds=delay)
            if report:
                report(value)
            stop.wait(delay)
    finally:
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
    parser.add_argument('setup_file')
    parser.add_argument('connection')
    args = parser.parse_args()
    initial = load_private(args.setup_file)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        current = load_private(args.setup_file)
        if current != initial:
            raise Blocked('Setup changed; restart only after reviewing the change')
        print(Notices(current).tick(args.connection), flush=True)
        stop.wait(5)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Pantheon watcher stopped. Inspect private setup and notice state; do not replay an uncertain notice.') from None
