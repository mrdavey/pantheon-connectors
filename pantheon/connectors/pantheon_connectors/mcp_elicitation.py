"""Correlated native form responses; model tool arguments cannot approve setup."""
from collections import deque
import json
import queue
import secrets
import threading
import time

from .worker import Blocked


class Input:
    def __init__(self, stream, write):
        self.write = write
        self.pending = deque()
        self.incoming = queue.Queue(maxsize=64)
        self.form_supported = False
        self.request_id = None

        def read():
            while True:
                line = stream.readline(1024 * 1024 + 1)
                self.incoming.put(line)
                if not line or len(line) > 1024 * 1024:
                    return
        threading.Thread(target=read, daemon=True, name='pantheon-mcp-input').start()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.pending.popleft() if self.pending else self.incoming.get()
        if not line:
            raise StopIteration
        if len(line) > 1024 * 1024:
            raise Blocked('Native MCP input exceeds its limit')
        return line

    def initialize(self, params):
        capability = params.get('capabilities', {}).get('elicitation')
        self.form_supported = isinstance(capability, dict) and (
            not capability or isinstance(capability.get('form'), dict))

    def elicit(self, message, schema, *, timeout=600):
        if not self.form_supported:
            raise Blocked('This host does not advertise native form review. Use a supported Codex CLI with MCP elicitation; no setup or runtime permission was granted.')
        identity = 'pantheon-setup-' + secrets.token_hex(16)
        self.write({'jsonrpc': '2.0', 'id': identity, 'method': 'elicitation/create',
                    'params': {'mode': 'form', 'message': message, 'requestedSchema': schema}})
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                raise Blocked('Setup review timed out; no approval was recorded')
            try:
                line = self.incoming.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise Blocked('Setup review timed out; no approval was recorded') from None
            if not line or len(line) > 1024 * 1024:
                raise Blocked('Native setup review ended without a response')
            try:
                response = json.loads(line)
            except (ValueError, TypeError):
                raise Blocked('Invalid native setup response') from None
            if not isinstance(response, dict):
                raise Blocked('Invalid native setup response')
            if response.get('id') == identity and 'method' not in response:
                if 'error' in response:
                    raise Blocked('Native host could not present setup review; no approval was recorded')
                return response.get('result')
            if response.get('method') == 'notifications/cancelled' and response.get('params', {}).get('requestId') == self.request_id:
                raise Blocked('Setup review was cancelled; no approval was recorded')
            if response.get('method') == 'ping' and 'id' in response:
                self.write({'jsonrpc': '2.0', 'id': response['id'], 'result': {}})
            elif 'method' in response:
                if len(self.pending) >= 32:
                    raise Blocked('Too many concurrent requests during native setup review')
                self.pending.append(line)
            # Unrelated/late response IDs never establish human consent.
