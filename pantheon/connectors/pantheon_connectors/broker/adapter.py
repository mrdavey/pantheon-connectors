"""Host-neutral tool snapshots. Host identity and wake delivery stay in adapters."""
import copy
import random
import threading

from ..client import APIError
from ..worker import Blocked
from .catalog import LOCAL, for_origin


class ToolSnapshot:
    def __init__(self, origin, fallback, *, catalog=None):
        self.catalog = catalog or for_origin(origin)
        self.fallback = copy.deepcopy(fallback)
        self.lock = threading.Lock()
        self.revision = None
        self.current = None
        self.stop = threading.Event()
        self.worker = None
        self.status = 'not_fetched'

    def refresh(self, *, force=False):
        try:
            self.catalog.refresh(force=force)
        except (APIError, Blocked, OSError, ValueError):
            pass  # Retain the last complete snapshot, with its current status.
        snapshot, status = self.catalog.read_snapshot()
        if not snapshot:
            with self.lock:
                self.status = status
            return False
        tools = [{key: copy.deepcopy(item[key]) for key in ('name', 'description', 'inputSchema')}
                 for item in snapshot['tools']]
        with self.lock:
            changed = tools != self.current
            self.current = tools
            self.revision = snapshot['revision']
            self.status = status
            return changed

    def tools(self):
        with self.lock:
            if self.current is not None:
                return copy.deepcopy(self.current)
            # On first offline launch, expose only installed local primitives.
            # Never claim remote tools are usable from an absent snapshot.
            return [copy.deepcopy(tool) for tool in self.fallback if tool['name'] in LOCAL]

    def watch(self, active, changed):
        if self.worker is not None:
            return

        def run():
            while not self.stop.wait(random.uniform(180, 240)):
                if active() and self.refresh(force=True):
                    changed()
        self.worker = threading.Thread(target=run, daemon=True, name='pantheon-catalog')
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=2)
