"""Shared HTTP operations. No secrets in URLs, exceptions, or logs."""
import json
import secrets
import ssl
from .tls import client_context
import urllib.error
import urllib.parse
import urllib.request

COMPLETION_PROTOCOL = "pantheon.definition.v1"

class APIError(RuntimeError):
    def __init__(self, status, reason="http_error"):
        self.status = status
        self.reason = reason
        super().__init__(f"Pantheon request failed (HTTP {status})")

    def retryable(self):
        """Transient transport failure, never an authority or TLS workaround."""
        return (self.status in (429, 500, 502, 503, 504)
                or (self.status == 0 and self.reason in ('network_unavailable', 'network_timeout')))

    def guidance(self):
        messages = {
            'completion_protocol_unavailable': 'This server cannot safely accept the installed connector’s definition/completion protocol. Pause substantive work and completion; preserve this connection and its consent. Ask the operator to restore compatible service. Do not downgrade, rejoin or submit a legacy result.',
            'tls_verification_failed': 'HTTPS certificate verification failed. Check the native Python trust configuration; do not disable certificate verification.',
            'tls_trust_unavailable': 'The native Python trust configuration is unavailable. Repair it before retrying.',
            'network_unavailable': 'Pantheon could not be reached. Check connectivity, then resume this same connection without renewing consent.',
            'network_timeout': 'The Pantheon request timed out. For a changed operation, preserve its original key and arguments when checking or retrying the outcome.',
        }
        if self.reason in messages:
            return messages[self.reason]
        if self.status in (401, 403, 410):
            return 'Pantheon access is no longer available for this scope. Check membership or room closure; do not rejoin or renew automatically.'
        if self.status == 429 or 500 <= self.status <= 599 or self.status == 0:
            return 'Pantheon is temporarily unavailable. Preserve the current connection and operation evidence; check receiver status before resuming.'
        return f'Pantheon rejected the request (HTTP {self.status}). Check this operation and its scope; preserve existing state.'

class Client:
    def __init__(self, url, credential, timeout=20):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
            raise ValueError("HTTPS required outside loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Unexpected URL credentials/query/fragment")
        self.url, self.credential = url.rstrip("/"), credential
        self.timeout = timeout

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        # Redirects could leak a bearer credential or redemption body to another origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=client_context())).open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise APIError(status) from None
        except ssl.SSLCertVerificationError:
            raise APIError(0, 'tls_verification_failed') from None
        except urllib.error.URLError as exc:
            reason = 'tls_verification_failed' if isinstance(exc.reason, ssl.SSLCertVerificationError) else 'network_unavailable'
            raise APIError(0, reason) from None
        except TimeoutError:
            raise APIError(0, 'network_timeout') from None
        except ssl.SSLError:
            raise APIError(0, 'tls_trust_unavailable') from None

    def transfer_bytes(self, path, content=None):
        """Private bounded octets; never return payloads through model tools."""
        from .transfers import MAX_BYTES
        from .worker import Blocked
        if content is not None and not 1 <= len(content) <= MAX_BYTES:
            raise Blocked("Transfer exceeds the file byte limit")
        headers = {"Content-Type": "application/octet-stream"}
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential
        request = urllib.request.Request(self.url + path, data=content, headers=headers,
                                         method="GET" if content is None else "PUT")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=client_context())).open(request, timeout=self.timeout) as response:
                result = response.read(MAX_BYTES + 1)
                if len(result) > MAX_BYTES:
                    raise Blocked("Transfer response exceeds the byte limit")
                return result if content is None else json.loads(result)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise APIError(status) from None
        except ssl.SSLCertVerificationError:
            raise APIError(0, 'tls_verification_failed') from None
        except urllib.error.URLError as exc:
            reason = 'tls_verification_failed' if isinstance(exc.reason, ssl.SSLCertVerificationError) else 'network_unavailable'
            raise APIError(0, reason) from None
        except TimeoutError:
            raise APIError(0, 'network_timeout') from None
        except ssl.SSLError:
            raise APIError(0, 'tls_trust_unavailable') from None

    def command(self, op, *, key=None, before_send=None, **fields):
        view = self.request("/v1/chat")
        payload = {"op": op, "key": key or secrets.token_urlsafe(24),
            "epoch": view["chat"]["epoch"], "config": view["chat"]["config"], **fields}
        if op in ("definition", "result", "confirm"):
            if view.get("completion_protocol") != COMPLETION_PROTOCOL:
                raise APIError(409, "completion_protocol_unavailable")
            payload["completion_protocol"] = COMPLETION_PROTOCOL
        if before_send is not None:
            before_send(payload)
        return self.request("/v1/commands", payload)

    def ack(self, message_ids, *, key):
        """Only an explicit model tool invocation calls this; never the delivery pump."""
        return self.command("ack", ids=message_ids, key=key)
