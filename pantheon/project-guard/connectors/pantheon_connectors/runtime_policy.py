"""Explicit connection lifetime; absence preserves finite legacy grants."""
import math
import time

SCHEMA = {'type': 'object', 'properties': {'mode': {'const': 'connection'}},
          'required': ['mode'], 'additionalProperties': False}


def same_scope(current, original):
    """Allow discovery expiry refresh only; all authority/configuration stays pinned."""
    def normalized(config):
        value = dict(config)
        if isinstance(value.get('hosted_onboarding'), dict):
            value['hosted_onboarding'] = dict(value['hosted_onboarding'])
            value['hosted_onboarding'].pop('expires_at', None)
            if value['hosted_onboarding'].get('schema') == 'pantheon.hosted-onboarding.v3':
                # Only callers that revalidate installed compatibility may save
                # fresh public metadata. None of these fields grants runtime.
                value['hosted_onboarding'] = {key: value['hosted_onboarding'][key] for key in
                    ('schema', 'service_origin', 'mcp_resource', 'broker_contract', 'primitive_contracts')}
        return value
    return normalized(current) == normalized(original)


def allowed(config, *, now=None):
    now = time.time() if now is None else now
    if config.get('allow_runtime_turns') is not True:
        return False
    if 'codex_setup_policy' in config:
        from .broker.codex_setup_policy import allowed as setup_allowed
        if not setup_allowed(config):
            return False
    expiry = config.get('trial_expires_at', 0)
    if 'runtime_policy' not in config:
        return isinstance(expiry, (int, float)) and now < expiry <= now + 3600
    started = config.get('runtime_started_at')
    return (config['runtime_policy'] == {'mode': 'connection'}
            and config.get('connection_runtime_supported') is True
            and type(expiry) in (int, float) and expiry == 0
            and type(started) in (int, float) and math.isfinite(started)
            and 0 < started <= now)
