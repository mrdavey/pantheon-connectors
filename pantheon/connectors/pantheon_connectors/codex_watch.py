"""Bounded owner-started notice watcher. Never creates/resumes a Codex thread."""
import argparse
import signal
import threading
from .codex_queue import Notices
from .state import load_private
from .worker import Blocked
from .client import APIError
from .receiver_lifecycle import ReceiverScope


def run(setup_file, connection, stop, report=None, audit=None):
    """Plugin-owned desktop receiver. EOF stops only this reader, never Codex."""
    scope = ReceiverScope(setup_file, connection, stop, audit)
    initial = scope.initial
    notices = Notices(initial, unchanged=scope.unchanged)
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
                scope.retry.reset()
                delay = 5
            except APIError as error:
                value, delay = scope.recover_read(error)
            if report:
                report(value)
            stop.wait(delay)
    finally:
        scope.report_expiry()



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
