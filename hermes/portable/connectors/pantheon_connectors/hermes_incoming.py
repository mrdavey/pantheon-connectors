"""Verify the separate native host permission through Hermes's supported CLI.

PluginContext.get_config reads only plugin settings, not host permission gates.
Do not inspect private host registries or infer this permission from runtime consent.
This module reads one setting only; the agent uses normal host controls after review.
"""
import json
import shlex
import subprocess

from .worker import Blocked

KEY = 'plugins.entries.pantheon.allow_gateway_injection'


def command(profile, operation, *values):
    if not isinstance(profile, str) or not profile or len(profile) > 256:
        raise Blocked('The observed native profile is unavailable; preserve this conversation')
    return ['hermes', '--profile', profile, 'config', operation, KEY, *values]


def allowed(profile):
    try:
        result = subprocess.run(command(profile, 'get', '--json'), capture_output=True,
                                text=True, timeout=15, check=False)
        # The native CLI can warn and fall back after profile-resolution errors.
        # Such a value cannot prove this observed profile's permission.
        return result.returncode == 0 and not result.stderr.strip() and json.loads(result.stdout) is True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


class PermissionRequired(Blocked):
    def __init__(self, profile):
        self.profile = profile
        super().__init__('Hermes host incoming permission is missing or cannot be verified. '
            'Private runtime consent does not enable the host setting. Preserve the invitation '
            'and original deadline. Under already approved incoming scope, record the prior '
            'setting and use the normal Hermes config command, then retry this same operation. '
            'Ask only if that incoming permission has not been approved.')

    def result(self):
        return {'status': 'blocked', 'reason': str(self),
            'next_action': 'verify_reviewed_host_permission_then_retry_same_scope',
            'host_incoming_permission': 'missing_or_unverified',
            'native_commands': {
                'inspect': shlex.join(command(self.profile, 'get', '--json')),
                'enable_only_under_explicit_approval': shlex.join(command(self.profile, 'set', 'true'))},
            'cleanup': 'Restore the exact prior value with normal native controls when no other approved connection needs it. Preserve an originally unset setting as unset. No plugin reload or consent renewal is required.'}


def require(profile, probe=allowed):
    if probe(profile) is not True:
        raise PermissionRequired(profile)
