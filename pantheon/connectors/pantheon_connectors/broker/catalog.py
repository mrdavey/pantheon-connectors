"""Atomic shared catalogue snapshots; fixed local primitives cannot be expanded."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time

from ..worker import Blocked
from .transport import HTTP

RESERVED = {'pantheon_prepare', 'pantheon_authorize', 'pantheon_begin_wake', 'pantheon_finish_wake',
            'pantheon_status', 'pantheon_resume', 'pantheon_disconnect', 'pantheon_setup', 'pantheon_connect'}
LOCAL = {
    'pantheon_preview': 'invitation.preview.v1', 'pantheon_join': 'invitation.join.v1',
    'pantheon_doctor': 'connection.doctor.v1', 'pantheon_receive': 'delivery.receive.v1',
    'pantheon_transfer_send': 'transfer.send.v1', 'pantheon_transfer_fetch': 'transfer.fetch.v1',
    'pantheon_transfer_ack': 'transfer.ack.v1', 'pantheon_site_deploy': 'site.deploy.v1',
}
FIELDS = {'name', 'description', 'contract', 'execution', 'operation', 'primitive', 'authority',
          'mutating', 'inputSchema', 'wireInputSchema', 'outputSchema'}


def _bounded(value, depth=0):
    if depth > 24:
        raise Blocked('Catalogue schema nesting exceeds its limit')
    if isinstance(value, dict):
        if len(value) > 100 or any(not isinstance(key, str) for key in value):
            raise Blocked('Invalid catalogue schema')
        for key, item in value.items():
            if key in ('$ref', '$dynamicRef') and (not isinstance(item, str) or not item.startswith('#/')):
                raise Blocked('External schema references are not supported')
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 200:
            raise Blocked('Catalogue array exceeds its limit')
        for item in value:
            _bounded(item, depth + 1)
    elif isinstance(value, str) and len(value) > 16000:
        raise Blocked('Catalogue text exceeds its limit')
    elif isinstance(value, float) and not math.isfinite(value):
        raise Blocked('Nonfinite catalogue number')


def validate(payload):
    from ..onboarding import TOOLS
    installed = {tool['name']: tool['inputSchema'] for tool in TOOLS}
    if (not isinstance(payload, dict) or set(payload) != {'schema', 'revision', 'tools'}
            or payload['schema'] != 'pantheon.tool-catalog.v1'
            or not isinstance(payload['revision'], str)
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', payload['revision'])
            or not isinstance(payload['tools'], list) or not 1 <= len(payload['tools']) <= 100):
        raise Blocked('Incompatible Pantheon tool catalogue')
    _bounded(payload)
    seen = set()
    compatible = []
    for tool in payload['tools']:
        if not isinstance(tool, dict) or set(tool) != FIELDS:
            raise Blocked('Invalid tool descriptor')
        name = tool['name']
        if (not isinstance(name, str) or not re.fullmatch(r'pantheon_[a-z][a-z0-9_]{0,63}', name)
                or name in seen or name in RESERVED or tool['authority'] != 'chat.member'
                or type(tool['mutating']) is not bool or not isinstance(tool['description'], str)
                or not tool['description'] or not isinstance(tool['contract'], str)):
            raise Blocked('Invalid tool identity or authority')
        seen.add(name)
        for field in ('inputSchema', 'wireInputSchema', 'outputSchema'):
            if not isinstance(tool[field], dict) or tool[field].get('type') != 'object':
                raise Blocked('Invalid tool schema')
        if tool['contract'] != '1':
            continue  # The incompatible contract is never executed under v1.
        if tool['execution'] == 'local':
            if (LOCAL.get(name) != tool['primitive'] or tool['operation']
                    or tool['inputSchema'] != installed.get(name)
                    or tool['wireInputSchema'] != tool['inputSchema']):
                raise Blocked('Catalogue cannot change an installed local primitive')
        elif tool['execution'] == 'remote':
            if name in LOCAL or tool['primitive'] or not isinstance(tool['operation'], str) or not tool['operation']:
                raise Blocked('Invalid remote tool route')
            schema = tool['inputSchema']
            if (schema.get('additionalProperties') is not False
                    or schema.get('properties', {}).get('connection') != {'type': 'string'}
                    or 'connection' not in schema.get('required', [])):
                raise Blocked('Remote tools require an explicit scoped connection')
        else:
            raise Blocked('Unknown tool execution kind')
        compatible.append(copy.deepcopy(tool))
    return {'schema': payload['schema'], 'revision': payload['revision'],
            'tools': sorted(compatible, key=lambda tool: tool['name'])}


class Catalog:
    def __init__(self, origin, *, http=None, clock=time.monotonic, cache_dir=None):
        self.origin = origin
        self.http = http or HTTP(origin)
        self.clock = clock
        self.lock = threading.RLock()
        self.fetch_lock = threading.Lock()
        self.snapshot = None
        self.etag = ''
        self.checked_at = None
        self.status = 'not_fetched'
        self.digest = ''
        self.cache = None
        if cache_dir is not None:
            from ..onboarding import private_directory
            from ..state import load_private
            self.cache = private_directory(cache_dir) / (hashlib.sha256(origin.encode()).hexdigest() + '.json')
            if self.cache.exists() or self.cache.is_symlink():
                saved = load_private(self.cache)
                if saved.get('origin') != origin:
                    raise Blocked('Catalogue cache belongs to another origin')
                self.snapshot = validate(saved['catalog'])
                self.digest = self._digest(saved['catalog'])
                self.etag = saved['etag']
                self.status = 'cached_unverified'

    @staticmethod
    def _digest(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def refresh(self, *, force=False):
        with self.fetch_lock:
            with self.lock:
                if not force and self.checked_at is not None and self.clock() - self.checked_at < 240:
                    return False
                etag = self.etag
            try:
                payload, received_etag = self.http.request('/v1/tool-catalog', headers={'If-None-Match': etag} if etag else {})
            except Exception:
                with self.lock:
                    self.status = 'offline_cached' if self.snapshot else 'unavailable'
                raise
            validated = validate(payload) if payload is not None else None
            if not isinstance(received_etag, str) or len(received_etag) > 128:
                raise Blocked('Invalid catalogue cache validator')
            with self.lock:
                if validated is None:
                    if self.snapshot is None:
                        raise Blocked('Catalogue revalidation has no saved snapshot')
                    self.checked_at = self.clock()
                    self.status = 'current'
                    return False
                if (self.snapshot is not None and validated['revision'] == self.snapshot['revision']
                        and self._digest(payload) != self.digest):
                    raise Blocked('Catalogue changed without a new revision')
                changed = validated != self.snapshot
                self.snapshot = validated
                self.digest = self._digest(payload)
                self.etag = received_etag
                self.checked_at = self.clock()
                self.status = 'current'
                if self.cache:
                    from ..state import save_private
                    save_private(self.cache, {'origin': self.origin, 'catalog': payload, 'etag': received_etag})
                return changed

    def tools(self):
        with self.lock:
            return copy.deepcopy(self.snapshot['tools']) if self.snapshot else []

    def read_snapshot(self):
        """Read tools, revision and diagnostic status at one atomic boundary."""
        with self.lock:
            return copy.deepcopy(self.snapshot), self.status

    def lookup(self, name):
        for tool in self.tools():
            if tool['name'] == name:
                return tool
        raise Blocked('Tool is not present in the compatible Pantheon catalogue')


_catalogs = {}
_catalogs_lock = threading.Lock()


def for_origin(origin):
    # The packaged origin is fixed; synthetic loopback origins are explicit tests.
    with _catalogs_lock:
        if origin not in _catalogs:
            if len(_catalogs) >= 8:
                raise Blocked('Too many broker origins')
            # Public descriptors only. Credentials and runtime consent remain in
            # the native connection's separate owner-only state directory.
            _catalogs[origin] = Catalog(origin, cache_dir=Path.home() / '.pantheon' / 'catalogs')
        return _catalogs[origin]
