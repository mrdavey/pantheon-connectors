"""Bounded owner-started notice watcher. Never creates/resumes a Codex thread."""
import argparse
import signal
import threading
from .codex_queue import Notices
from .state import load_private
from .worker import Blocked


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
