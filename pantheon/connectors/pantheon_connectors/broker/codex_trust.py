"""Optional exact-tool host policy; never a wildcard, runtime grant or install default."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib

from ..onboarding import private_lock
from ..worker import Blocked

SERVER = ('plugins', 'pantheon@pantheon-hosted', 'mcp_servers', 'pantheon')
# Reviewed ordinary collaboration. Local files, publishing and newly added tools
# retain native prompts. Connect itself still enforces explicit scoped consent.
ROUTINE = ('pantheon_connect', 'pantheon_preview', 'pantheon_doctor', 'pantheon_receive',
           'pantheon_ack', 'pantheon_post', 'pantheon_command', 'pantheon_chat',
           'pantheon_message', 'pantheon_history', 'pantheon_receipts', 'pantheon_members',
           'pantheon_context', 'pantheon_chat_state', 'pantheon_begin_wake', 'pantheon_finish_wake')
TRANSFERS = ('pantheon_transfer_send', 'pantheon_transfer_fetch', 'pantheon_transfer_ack', 'pantheon_transfer_inspect')
SITES = ('pantheon_site_deploy', 'pantheon_site_status')


def lookup(value, path):
    for key in path:
        if not isinstance(value, dict):
            raise Blocked('Existing Pantheon policy has an unsupported shape; preserve it for review')
        value = value.get(key, {})
    return value


def add(text, path, key, value):
    """Insert into an existing ordinary table, or append an explicit child table.

    Inline/dotted layouts that cannot be extended unambiguously are preserved.
    Reparse before writing so no unrelated TOML value can change accidentally.
    """
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith('['):
            try:
                marker = tomllib.loads(line+'\n__pantheon_probe__ = true\n')
                if lookup(marker, path).get('__pantheon_probe__') is True:
                    lines.insert(index+1, f'{key} = {json.dumps(value)}\n')
                    return ''.join(lines)
            except (tomllib.TOMLDecodeError, Blocked, AttributeError):
                pass
    header = '.'.join(json.dumps(part) for part in path)
    return text.rstrip()+f'\n\n[{header}]\n{key} = {json.dumps(value)}\n'


def proposal(text, *, tools=ROUTINE):
    if set(tools) - set((*ROUTINE, *TRANSFERS, *SITES, 'pantheon_setup')):
        raise Blocked('Only reviewed Pantheon tools may receive native trust')
    original = tomllib.loads(text)
    result = text
    changes, retained = [], []
    entries = [(SERVER, 'default_tools_approval_mode', 'prompt')]
    entries += [(SERVER+('tools', name), 'approval_mode', 'approve') for name in tools]
    expected = json.loads(json.dumps(original))
    for path, key, value in entries:
        existing = lookup(original, path)
        if key in existing:
            if existing[key] != value:
                retained.append('.'.join((*path, key)))
            continue  # Explicit prior choices, especially deny, always win.
        result = add(result, path, key, value)
        target = expected
        for part in path:
            target = target.setdefault(part, {})
        target[key] = value
        changes.append('.'.join((*path, key)))
    try:
        if tomllib.loads(result) != expected:
            raise ValueError()
    except (tomllib.TOMLDecodeError, ValueError):
        raise Blocked('Existing host policy cannot be extended without rewriting it; preserve it and use native tool prompts') from None
    return {'text': result, 'changed': changes, 'retained': retained,
            'sha256': hashlib.sha256(result.encode()).hexdigest(), 'tools': list(tools)}


def configure(path, *, approved=False, expected_sha256=None, tools=ROUTINE):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.parent.is_dir():
        raise Blocked('Use the existing native config directory without symlinks')
    with private_lock(path.parent / 'pantheon-tool-policy.lock'):
        before = path.read_text() if path.exists() else ''
        result = proposal(before, tools=tools)
        if not approved:
            return result
        if expected_sha256 != result['sha256']:
            raise Blocked('Host configuration changed since review; inspect the new exact proposal')
        if result['text'] != before:
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.pantheon-policy-')
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    output.write(result['text'])
                    output.flush()
                    os.fsync(output.fileno())
                if (path.read_text() if path.exists() else '') != before:
                    raise Blocked('Concurrent host configuration edit; preserve it and review again')
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {key: value for key, value in result.items() if key != 'text'}
