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
            while not self.stop.is_set():
                try:
                    self.scan()
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
