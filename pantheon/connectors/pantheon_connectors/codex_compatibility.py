"""Offline native protocol checks; no daemon launch, session read or model call."""
import functools
import json
import os
from pathlib import Path
import subprocess
import tempfile
from .worker import Blocked

TESTED_VERSION = '0.162.1'
HOST_COMPATIBILITY = {'compatibility': 'native-capabilities', 'tested_version': TESTED_VERSION}


def validate_schema(schema):
    """Require the actual stable request shapes used by the adapter.

    Version labels are evidence, not a whitelist. Unknown constraints fail closed;
    this is deliberately a small contract checker, not a general JSON validator.
    """
    def accepts(node, sample, depth=0):
        if not isinstance(node, dict) or depth > 20:
            return False
        allowed = {'$ref', 'type', 'properties', 'required', 'additionalProperties',
                   'anyOf', 'oneOf', 'enum', 'items', 'minItems', 'maxItems',
                   'description', 'title', 'default'}
        if set(node) - allowed:
            return False
        if '$ref' in node:
            ref = node['$ref']
            if not ref.startswith('#/definitions/'):
                return False
            return accepts(schema.get('definitions', {}).get(ref.split('/')[-1]), sample, depth+1)
        for key in ('anyOf', 'oneOf'):
            if key in node:
                hits = sum(accepts(n, sample, depth+1) for n in node[key])
                return hits == 1 if key == 'oneOf' else hits > 0
        kind = 'object' if isinstance(sample, dict) else 'array' if isinstance(sample, list) else 'string' if isinstance(sample, str) else 'boolean' if isinstance(sample, bool) else None
        types = node.get('type', [])
        if kind not in ([types] if isinstance(types, str) else types):
            return False
        if 'enum' in node and sample not in node['enum']:
            return False
        if kind == 'object':
            props = node.get('properties', {})
            # Every submitted field must be declared, even if extra fields would
            # otherwise be silently ignored by an older runtime.
            return (set(node.get('required', [])) <= set(sample) <= set(props)
                    and all(accepts(props[k], v, depth+1) for k,v in sample.items()))
        if kind == 'array':
            return node.get('minItems', 0) <= len(sample) <= node.get('maxItems', 2**31) and all(accepts(node.get('items'), v, depth+1) for v in sample)
        return True

    requests = schema.get('oneOf', [])
    samples = {
        'turn/start': {'threadId': 'native-thread', 'input': [], 'toolOutput': {'name': 'pantheon_pending_notification', 'namespace': 'pantheon', 'output': '{"has_unread":true}'}},
        'thread/read': {'threadId': 'native-thread', 'includeTurns': False},
        'thread/loaded/list': {'cursor': 'cursor'},
        'config/read': {'includeLayers': True},
    }
    for method, sample in samples.items():
        candidates = [n for n in requests if n.get('properties', {}).get('method', {}).get('enum') == [method]]
        if len(candidates) != 1 or not accepts(candidates[0]['properties'].get('params'), sample):
            raise Blocked('Installed Codex is missing a required native protocol capability: ' + method)


def binary_identity(executable):
    p = Path(executable).resolve(strict=True)
    s = p.stat()
    return str(p), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


@functools.lru_cache(maxsize=8)
def _inspect(identity):
    with tempfile.TemporaryDirectory(prefix='pantheon-codex-capabilities-') as directory:
        root = Path(directory)
        environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'TMPDIR') if k in os.environ}
        environment['CODEX_HOME'] = str(root/'home')
        (root/'home').mkdir(mode=0o700)
        subprocess.run([identity[0], 'app-server', 'generate-json-schema', '--out', str(root/'schema')],
                       env=environment, capture_output=True, check=True, timeout=20)
        path = root/'schema/ClientRequest.json'
        if path.stat().st_size > 8 * 1024 * 1024:
            raise Blocked('Native protocol schema exceeds inspection limit')
        validate_schema(json.loads(path.read_text()))
    if binary_identity(identity[0]) != identity:
        raise Blocked('Codex executable changed during capability inspection')
    return True


def require_capabilities(executable):
    try:
        return _inspect(binary_identity(executable))
    except Blocked:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise Blocked('Installed Codex cannot export the required native protocol capabilities') from None
