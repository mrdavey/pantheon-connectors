"""Bounded retry policy for native receivers' read-only HTTP failures.

Native sends must translate uncertain outcomes to Blocked before this boundary.
Never use this policy to replay a provider invocation or a mutating HTTP request.
"""
from .worker import Blocked


class ReadRetry:
    def __init__(self):
        self.failures = 0

    def reset(self):
        self.failures = 0

    def delay(self, error):
        transient = (error.status in (408, 429) or 500 <= error.status <= 599
                     or error.status == 0 and error.reason in
                     ('http_error', 'network_unavailable', 'network_timeout'))
        if not transient:
            raise error
        self.failures += 1
        if self.failures >= 5:
            raise Blocked('Read retry budget exhausted; check connectivity and resume this same connection without renewing consent') from error
        return 2 ** self.failures
