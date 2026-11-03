"""Content-free operational records on stderr; never use stdout (MCP protocol)."""
import json
import sys
import threading
import time
import uuid


class Diagnostics:
    _outcomes = frozenset(("received", "reconciled", "suppressed", "busy",
                           "awaiting_agent_ack", "blocked", "transport_failed", "failed"))

    def __init__(self, stream=None, clock=time.monotonic):
        self.stream = stream
        self.clock = clock
        self.operation_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.window = None
        self.count = 0
        self.suppressed = 0

    def emit(self, outcome):
        # Ignore idle and unknown values, including exception/provider strings.
        if outcome not in self._outcomes:
            return
        with self.lock:
            window = int(self.clock() // 60)
            if window != self.window:
                self.window, self.count = window, 0
            if self.count >= 10:
                self.suppressed += 1
                return
            self.count += 1
            record = dict(severity="WARNING" if outcome in ("blocked", "transport_failed", "failed") else "INFO",
                          message="connector.cycle.completed", event="connector.cycle.completed",
                          schema_version=1, operation_id=self.operation_id,
                          outcome=outcome, suppressed=self.suppressed)
            try:
                print(json.dumps(record), file=self.stream or sys.stderr)
            except (OSError, ValueError):
                pass  # Diagnostics cannot change delivery/receipt semantics.
            self.suppressed = 0
