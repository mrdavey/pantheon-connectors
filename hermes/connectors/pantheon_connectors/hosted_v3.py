"""Separate v3 compatibility path; v1/v2 exact package pins remain unchanged."""
import copy
import json
from pathlib import Path
import re

from .client import Client
from .hosted_discovery import SCHEMA as V2, validate_discovery, matches_native_host, https_origin
from .worker import Blocked

SCHEMA = 'pantheon.hosted-onboarding.v3'
CONTRACT = 'pantheon.broker.v1'
FIELDS = {'schema', 'service_origin', 'expires_at', 'admission', 'packages', 'mcp_resource',
          'catalog_url', 'guide_url', 'protocol_versions', 'broker_contract', 'primitive_contracts', 'compatibility'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', value):
        raise Blocked('Invalid connector compatibility version')
    return tuple(map(int, value.split('.')))


def compatible(policy, value):
    return (version(policy['minimum_version']) <= version(value) < version(policy['maximum_exclusive'])
            and value not in policy['revoked_versions'])


def validate(value, origin, runtime, *, now=None):
    from .broker.catalog import LOCAL
    if (not isinstance(value, dict) or set(value) != FIELDS or value['schema'] != SCHEMA
            or value['broker_contract'] != CONTRACT or value['mcp_resource'] != origin + '/mcp'
            or value['catalog_url'] != origin + '/v1/tool-catalog'
            or not isinstance(value['guide_url'], str)
            or not re.fullmatch(re.escape(origin + '/v1/onboarding/v3/guide') + r'(?:/[a-f0-9]{64})?', value['guide_url'])
            or value['protocol_versions'] != ['2025-11-25', '2025-06-18']
            or not isinstance(value['primitive_contracts'], list)
            or any(not isinstance(p, str) for p in value['primitive_contracts'])
            or len(value['primitive_contracts']) != len(LOCAL)
            or set(value['primitive_contracts']) != set(LOCAL.values())
            or not isinstance(value['compatibility'], dict)
            or not isinstance(value['packages'], dict)
            or set(value['compatibility']) != set(value['packages'])):
        raise Blocked('Incompatible v3 onboarding descriptor')
    validate_discovery({**value, 'schema': V2}, origin, runtime, now=now)
    admission = value['admission']
    if (set(admission) != {'enabled', 'runtimes'} or len(admission['runtimes']) != len(value['packages'])
            or any(not isinstance(name, str) for name in admission['runtimes'])
            or set(admission['runtimes']) != set(value['packages'])):
        raise Blocked('Invalid v3 admitted runtime set')
    for name, policy in value['compatibility'].items():
        if (name not in ('codex', 'hermes') or not isinstance(policy, dict)
                or set(policy) != {'minimum_version', 'maximum_exclusive', 'revoked_versions', 'host_modes'}
                or not isinstance(policy['revoked_versions'], list) or len(policy['revoked_versions']) > 100
                or any(not isinstance(v, str) for v in policy['revoked_versions'])
                or len(set(policy['revoked_versions'])) != len(policy['revoked_versions'])
                or not valid_host_modes(name, policy, value['packages'][name])):
            raise Blocked('Invalid v3 connector compatibility policy')
        validate_discovery({**value, 'schema': V2}, origin, name, now=now)
        for revoked in policy['revoked_versions']:
            version(revoked)
        if not compatible(policy, value['packages'][name]['version']):
            raise Blocked('Current package is excluded by its compatibility policy')
    if ('native-queue' in value['compatibility'].get('codex', {}).get('host_modes', [])
            and 'hermes' in value['compatibility']
            and version(value['compatibility']['hermes']['minimum_version']) < (0, 5, 6)):
        raise Blocked('Desktop discovery requires Hermes 0.5.6+ to read the shared host policy')
    if (all(version(p['version']) >= ((0, 7, 0) if name == 'codex' else (0, 5, 0))
            for name, p in value['packages'].items())
            and not re.fullmatch(re.escape(origin + '/v1/onboarding/v3/guide/') + '[a-f0-9]{64}', value['guide_url'])):
        raise Blocked('Fresh onboarding requires a content-addressed guide')
    return value


def valid_host_modes(runtime, policy, package):
    if policy.get('host_modes') == ['app-server-tool-output' if runtime == 'codex' else 'native-adapter']:
        return True
    return (runtime == 'codex' and policy.get('host_modes') == ['app-server-tool-output', 'native-queue']
            and version(policy['minimum_version']) >= (0, 9, 0)
            and version(package['version']) >= (0, 9, 0)
            and package['host'].get('compatibility') == 'native-capabilities')


def matches_host(discovery, binding):
    runtime = binding['runtime']
    return matches_native_host(discovery['packages'][runtime], binding,
                               surfaces=discovery['compatibility'][runtime]['host_modes'])


def check_installed(value, runtime, installed):
    if (not isinstance(installed, dict) or set(installed) != {'version', 'source_revision', 'broker_contract'}
            or installed['broker_contract'] != CONTRACT
            or not isinstance(installed['source_revision'], str)
            or not re.fullmatch('[a-f0-9]{40}', installed['source_revision'])
            or not compatible(value['compatibility'][runtime], installed['version'])):
        raise Blocked('Installed broker is incompatible or revoked; preserve connection and review the native upgrade')
    current = value['packages'][runtime]
    if current['version'] == installed['version'] and current['source_revision'] != installed['source_revision']:
        raise Blocked('Published package changed source under the same version')


def fetch(origin, runtime, *, client_factory=Client):
    https_origin(origin)
    if client_factory is Client:
        from .broker.transport import HTTP
        value, _ = HTTP(origin).request('/v1/onboarding/v3')
    else:
        value = client_factory(origin, '').request('/v1/onboarding/v3')
    return validate(value, origin, runtime)


def admission(config, binding, *, client_factory=Client):
    if config.get('hosted_consent') is not True or config.get('mcp') != {'contract': CONTRACT}:
        raise Blocked('This native connection has no reviewed broker scope')
    runtime, origin = binding['runtime'], config['url']
    # A stale descriptor can be revalidated without renewing native consent.
    # Only the trusted live descriptor decides current admission/compatibility.
    saved = validate(config.get('hosted_onboarding'), origin, runtime, now=0)
    fresh = fetch(origin, runtime, client_factory=client_factory)
    check_installed(fresh, runtime, config.get('installed_connector'))
    if (saved['broker_contract'] != fresh['broker_contract']
            or not matches_host(fresh, binding)):
        raise Blocked('Current host is outside the reviewed broker contract')
    return fresh


def installed_codex(metadata):
    return {'version': metadata['package_version'], 'source_revision': metadata['source_revision'],
            'broker_contract': metadata.get('broker_contract')}


def installed_hermes():
    from .broker.package import metadata
    package = metadata()
    if package is None:
        return None
    provenance = json.loads((Path(__file__).resolve().parents[2] / 'SOURCE-PROVENANCE.json').read_text())
    return {'version': package['package_version'], 'source_revision': provenance['source_commit'],
            'broker_contract': package['contract']}


def prepare_config(config, discovery, installed, binding):
    validate(discovery, config['url'], binding['runtime'])
    check_installed(discovery, binding['runtime'], installed)
    if not matches_host(discovery, binding):
        raise Blocked('Native host is outside the broker compatibility policy')
    updated = copy.deepcopy(config)
    updated.update(hosted_onboarding=discovery, installed_connector=installed, mcp={'contract': CONTRACT})
    return updated


def reprepare_config(config, discovery, installed, binding):
    """Reuse consent after a separately installed, verified compatible upgrade.

    Callers obtain installed metadata from the native loaded package, never tool
    arguments. Same-version source replacement and downgrade remain blocked.
    """
    from .runtime_policy import same_scope
    old = config.get('installed_connector')
    if (config.get('hosted_onboarding', {}).get('schema') != SCHEMA
            or config.get('mcp') != {'contract': CONTRACT}
            or not isinstance(old, dict) or old.get('broker_contract') != CONTRACT):
        raise Blocked('Existing preparation requires deliberate contract migration')
    if old != installed and version(installed['version']) <= version(old['version']):
        raise Blocked('Native upgrade cannot downgrade or replace source under one version')
    updated = prepare_config(config, discovery, installed, binding)
    # Package loading was independently verified; every authority field remains
    # byte-for-byte equivalent apart from fresh public discovery metadata.
    comparable = {**updated, 'installed_connector': old}
    if not same_scope(comparable, config):
        raise Blocked('Native upgrade changes the reviewed connection scope')
    return updated
