"""Private content-free onboarding evidence. Never read as authorization.

Only existing native checks and successful scoped operations may record progress.
A stage is historical evidence, not proof that consent or readiness is still valid.
"""
import hashlib
import json
from pathlib import Path
import re
import time

from ..state import load_private, save_private
from ..worker import Blocked

STAGES = ('discovered', 'prepared', 'host_loaded', 'authorized', 'joined', 'delivery_verified')
STATES = (*STAGES, 'blocked', 'stopped', 'revoked')
EVIDENCE = {'trusted_descriptor', 'verified_package', 'native_tool_dispatch',
            'native_allowance_checked', 'redemption_committed', 'ready_accepted',
            'withdraw_accepted', 'runtime_inactive', 'native_check_failed', 'scope_denied'}


def _location(config, connection, binding):
    from ..onboarding import private_directory
    if (config.get('mcp') != {'contract': 'pantheon.broker.v1'}
            or not re.fullmatch('[a-f0-9]{32}', connection)):
        raise Blocked('Invalid bootstrap evidence scope')
    # Outside the membership directory: an empty state directory remains the
    # existing native check for a fresh, not-yet-authorized preparation.
    directory = private_directory(Path(config['state_dir']).parent / 'bootstrap')
    scope = hashlib.sha256(json.dumps({'origin': config['url'], 'connection': connection,
        'binding': binding}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return directory / (connection + '.json'), scope


def record(config, connection, binding, state, evidence, *, now=time.time):
    from ..onboarding import private_lock
    if state not in STATES or evidence not in EVIDENCE:
        raise ValueError('Unrecognized bootstrap evidence')
    path, scope = _location(config, connection, binding)
    with private_lock(path.with_suffix('.lock')):
        value = load_private(path) if path.exists() or path.is_symlink() else {
            'schema': 'pantheon.bootstrap.v1', 'scope': scope, 'stage': None,
            'state': None, 'events': []}
        if value.get('scope') != scope or value.get('schema') != 'pantheon.bootstrap.v1':
            raise Blocked('Bootstrap evidence belongs to another scope')
        if value['state'] in ('stopped', 'revoked') and state in STAGES:
            return value  # Preparation/retry is never resumption consent.
        if state in STAGES and value['stage'] is not None and STAGES.index(state) < STAGES.index(value['stage']):
            return value  # Returning preparation never erases later evidence.
        if value['state'] == state:
            return value
        if state in STAGES:
            value['stage'] = state
        value['state'] = state
        value['events'] = (value['events'] + [{'state': state, 'evidence': evidence,
                                              'at': now()}])[-24:]
        save_private(path, value)
        return value


def read(config, connection, binding):
    path, scope = _location(config, connection, binding)
    if not path.exists() and not path.is_symlink():
        return {'state': 'not_recorded'}
    value = load_private(path)
    if value.get('scope') != scope or value.get('schema') != 'pantheon.bootstrap.v1':
        raise Blocked('Bootstrap evidence belongs to another scope')
    return {**value, 'historical_evidence_only': True}


def prepared(config, connection, binding):
    if config.get('mcp') != {'contract': 'pantheon.broker.v1'}:
        return
    for state, evidence in (('discovered', 'trusted_descriptor'), ('prepared', 'verified_package'),
                            ('host_loaded', 'native_tool_dispatch')):
        record(config, connection, binding, state, evidence)
