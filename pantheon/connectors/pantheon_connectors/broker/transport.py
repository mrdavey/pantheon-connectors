"""Bounded first-party stateless MCP HTTP. No redirects or ambient credentials."""
import json
import ssl
import urllib.error
import urllib.request

from ..client import APIError, Client
from ..tls import client_context
from ..worker import Blocked

PROTOCOL = '2025-11-25'
MAX_RESPONSE = 2 * 1024 * 1024


def strict_json(raw):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON member')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (ValueError, RecursionError, UnicodeError):
        raise Blocked('Invalid Pantheon JSON response') from None


class HTTP:
    def __init__(self, origin, timeout=15):
        Client(origin, '')  # Reuse HTTPS/loopback and URL credential validation.
        self.origin = origin.rstrip('/')
        self.timeout = timeout

    def request(self, path, *, payload=None, credential='', headers=None):
        if path not in ('/mcp', '/v1/tool-catalog', '/v1/mcp/token', '/v1/mcp/revoke', '/v1/onboarding/v3'):
            raise Blocked('Unsupported broker resource')
        request_headers = {'Accept': 'application/json, text/event-stream'}
        request_headers.update(headers or {})
        if credential:
            request_headers['Authorization'] = 'Bearer ' + credential
        data = None if payload is None else json.dumps(payload, allow_nan=False, separators=(',', ':')).encode()
        if data is not None:
            if len(data) > 64 * 1024:
                raise Blocked('Pantheon request exceeds its byte limit')
            request_headers['Content-Type'] = 'application/json'

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        request = urllib.request.Request(self.origin + path, data=data, headers=request_headers)
        try:
            opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=client_context()))
            with opener.open(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() != 'application/json':
                    raise Blocked('Pantheon returned an unsupported response type')
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise Blocked('Pantheon response exceeds its byte limit')
                return strict_json(raw), response.headers.get('ETag', '')
        except urllib.error.HTTPError as error:
            status = error.code
            reason = ''
            try:
                # A single allowlisted recovery code, never arbitrary server
                # text, may influence a journal transition.
                if path == '/v1/mcp/token' and status == 409:
                    raw = error.read(1025)
                    if len(raw) <= 1024 and strict_json(raw) == {'error': 'mcp_exchange_expired_uncommitted'}:
                        reason = 'mcp_exchange_expired_uncommitted'
            except (Blocked, OSError):
                pass
            finally:
                error.close()
            if status == 304 and path == '/v1/tool-catalog':
                return None, ''
            raise APIError(status, reason) from None
        except ssl.SSLCertVerificationError:
            raise APIError(0, 'tls_verification_failed') from None
        except urllib.error.URLError as error:
            reason = 'tls_verification_failed' if isinstance(error.reason, ssl.SSLCertVerificationError) else 'network_unavailable'
            raise APIError(0, reason) from None
        except TimeoutError:
            raise APIError(0, 'network_timeout') from None
        except ssl.SSLError:
            raise APIError(0, 'tls_verification_failed') from None

    def rpc(self, method, params, token):
        value, _ = self.request('/mcp', credential=token,
                                headers={'MCP-Protocol-Version': PROTOCOL},
                                payload={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != 1:
            raise Blocked('Invalid MCP response envelope')
        if 'error' in value:
            raise Blocked('Pantheon MCP rejected the request; preserve its original operation key')
        result = value.get('result')
        if not isinstance(result, dict):
            raise Blocked('Invalid MCP result')
        return result

    def initialize(self, token):
        result = self.rpc('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                         'clientInfo': {'name': 'pantheon-broker', 'version': '1'}}, token)
        if result.get('protocolVersion') != PROTOCOL or 'tools' not in result.get('capabilities', {}):
            raise Blocked('Incompatible Pantheon MCP service')
        # Stateless HTTP has no protocol session to retain; every call supplies
        # the selected revision and freshly checked resource credential.
        return result
