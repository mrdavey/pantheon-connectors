"""Preapproved tool names do not confer authority on changed server operations."""
import json
from pathlib import Path

from ..worker import Blocked


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k not in ('description', 'title', 'examples')}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


def require(config, descriptor, arguments):
    if 'codex_setup_policy' not in config:
        return
    approved = json.loads(Path(__file__).with_name('permissions.json').read_text()).get(descriptor['name'])
    if approved is None:
        # Setup never preapproves unknown names in native configuration. The
        # host's normal per-call review applies; catalogue validation still
        # confines them to remote chat.member operations.
        if descriptor.get('execution') != 'remote' or descriptor.get('authority') != 'chat.member':
            raise Blocked('New local authority requires a reviewed package')
        return
    for key in ('contract', 'execution', 'operation', 'primitive', 'authority', 'mutating'):
        if approved[key] != descriptor.get(key):
            raise Blocked('The server changed this tool operation; existing tool trust does not approve it')
    for key in ('inputSchema', 'wireInputSchema'):
        current, pinned = semantic(descriptor[key]), semantic(approved[key])
        # Additional optional arguments do not break existing calls, but cannot
        # be exercised under an earlier setup. Changed old fields/requiredness
        # and command op enums always need a reviewed package contract.
        extra = set(current.get('properties', {})) - set(pinned.get('properties', {}))
        if extra.intersection(arguments) or extra.intersection(current.get('required', [])):
            raise Blocked('New tool arguments require a reviewed setup contract')
        current['properties'] = {k: v for k, v in current.get('properties', {}).items() if k not in extra}
        if current != pinned:
            raise Blocked('This tool schema changed its reviewed permission contract')
