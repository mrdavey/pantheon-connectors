"""Human-reviewed installation preferences, separate from immutable chat grants.

No invitation, membership credential or provider credential is stored here. Native
form acceptance is the only creation path; model arguments cannot grant consent.
"""
import math
import os
from pathlib import Path
import secrets
import tempfile
import time

from ..onboarding import private_directory, private_lock
from ..state import load_private, save_private
from ..worker import Blocked
from . import codex_trust

CONTRACT = 'pantheon.codex-setup.v1'
TOOL = {'name': 'pantheon_setup', 'description':
    'Review Pantheon setup once using the native human form. Reuse active setup for deliberate human invitations; status is read-only, revoke stops derived runtime grants. Model arguments never approve permissions.',
    'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string',
        'enum': ['review', 'status', 'revoke', 'change']}}, 'required': ['action'], 'additionalProperties': False}}
FORM = {'type': 'object', 'properties': {
    'collaboration': {'type': 'boolean', 'title': 'Allow Pantheon collaboration', 'default': True,
        'description': 'Inspect/access the broader local Codex daemon, bind only your invited conversations, receive incoming replies and use their existing provider/account at normal usage cost. Approve exact routine messaging tools in this Codex profile.'},
    'remember': {'type': 'boolean', 'title': 'Remember for invitations I give Codex', 'default': True},
    'minutes': {'type': 'integer', 'title': 'Time limit (0 = until each chat ends)', 'minimum': 0, 'maximum': 60, 'default': 0,
        'description': 'A nonzero limit is one fixed window starting now, including any later invitation; retries never extend it.'},
    'transfers': {'type': 'boolean', 'title': 'Allow temporary file handoff', 'default': False,
        'description': 'Send to a single chat member and save incoming files only under the folder below; existing size and expiry limits apply.'},
    'transfer_folder': {'type': 'string', 'title': 'File handoff folder (absolute path)', 'default': ''},
    'sites': {'type': 'boolean', 'title': 'Allow public website publishing', 'default': False,
        'description': 'Publish static files from the folder below using separately granted chat website authority. Published sites may remain online after chat closure.'},
    'site_folder': {'type': 'string', 'title': 'Public website source folder (absolute path)', 'default': ''},
}, 'required': ['collaboration', 'remember', 'minutes', 'transfers', 'sites']}
MESSAGE = ('Allow Pantheon to join chats from invitations you give Codex, receive replies and collaborate using each conversation\'s existing provider/account at its normal cost. '
           'This includes the broader local Codex daemon API; Pantheon restricts use to invited conversations. Each chat stops on closure, withdrawal or expiry, with at most three active connections. '
           'Remembered choices never share membership or renew old grants. Some providers do not expose exact account identity. '
           'Optional files and public sites use the folders below and separate chat permissions. Existing native denials and unrelated shell/file controls remain. Revoke anytime with pantheon_setup.')


def home():
    return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser().resolve()


def provider(config):
    return {'provider': config.get('reviewed_provider'), 'account': config.get('reviewed_account')}


def choices(response, now):
    if not isinstance(response, dict) or response.get('action') != 'accept':
        return None
    value = response.get('content')
    if (not isinstance(value, dict) or set(value) - set(FORM['properties'])
            or not set(FORM['required']) <= set(value)
            or any(type(value.get(k)) is not bool for k in ('collaboration', 'remember', 'transfers', 'sites'))
            or type(value['minutes']) is not int or not 0 <= value['minutes'] <= 60):
        raise Blocked('The native setup response is incomplete; no permissions were saved')
    if not value['collaboration']:
        return None
    result = {'remember': value['remember'], 'expires_at': now + 60 * value['minutes'] if value['minutes'] else 0,
              'transfers': value['transfers'], 'sites': value['sites'], 'folders': {}}
    for capability, field in (('transfers', 'transfer_folder'), ('sites', 'site_folder')):
        if not result[capability]:
            continue
        folder = value.get(field)
        if not isinstance(folder, str) or not folder or not Path(folder).is_absolute():
            raise Blocked('Selected file capabilities require an existing absolute folder; no permissions were saved')
        path = Path(folder)
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_dir() or path == Path('/'):
            raise Blocked('Select a specific existing folder without symlinks; no permissions were saved')
        result['folders'][capability] = str(path.resolve(strict=True))
    return result


class Policy:
    def __init__(self, path, native_home=None):
        self.path = Path(path)
        self.home = Path(native_home).resolve() if native_home else home()

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        value = load_private(self.path)
        if (value.get('contract') != CONTRACT or value.get('home') != str(self.home)
                or value.get('uid') != os.getuid()):
            raise Blocked('Saved Pantheon setup belongs to another profile or contract')
        return value

    def active(self, value, thread=None):
        if not value or value.get('active') is not True:
            return False
        expiry = value.get('expires_at')
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or (expiry != 0 and expiry <= time.time()):
            return False
        return thread is None or value.get('remember') is True or value.get('thread') == thread

    def status(self, thread):
        value = self.read()
        return {'status': 'setup_active' if self.active(value, thread) else 'setup_required',
                'runtime_started': False, 'preference_is_not_a_chat_grant': True,
                **({k: value[k] for k in ('remember', 'expires_at', 'transfers', 'sites', 'folders')} if value else {})}

    def review(self, thread, elicit, inspect, *, change=False):
        if not change and self.active(self.read(), thread):
            return self.status(thread)
        # Nothing sensitive is inspected and no native policy is changed before
        # the real client's correlated acceptance response.
        message = ('Changing setup stops existing derived grants; they cannot be reopened. ' if change else '') + MESSAGE
        selected = choices(elicit(message, FORM), time.time())
        if selected is None:
            return {'status': 'setup_declined', 'runtime_started': False}
        private_directory(self.path.parent)
        with private_lock(self.path.with_suffix('.lock')):
            if not change and self.active(self.read(), thread):
                return self.status(thread)
            with tempfile.TemporaryDirectory(prefix='review-', dir=self.path.parent) as temporary:
                captured = inspect(Path(temporary) / 'setup.json')
            tools = (*codex_trust.ROUTINE, 'pantheon_setup',
                     *(codex_trust.TRANSFERS if selected['transfers'] else ()),
                     *(codex_trust.SITES if selected['sites'] else ()))
            proposal = codex_trust.configure(self.home / 'config.toml', tools=tools)
            value = dict(contract=CONTRACT, id=secrets.token_hex(16), active=False,
                         uid=os.getuid(), home=str(self.home), thread=thread,
                         provider=provider(captured), created_at=time.time(), claims=[], **selected)
            save_private(self.path, value)
            trust = codex_trust.configure(self.home / 'config.toml', approved=True,
                                         expected_sha256=proposal['sha256'], tools=tools)
            value['active'] = True
            save_private(self.path, value)
        return {**self.status(thread), 'native_policy': trust,
                'next_action': 'connect_original_human_invitation',
                'notice': 'Setup saved. New chats get fresh grants. Native tools may need the host\'s normal reload; no runtime or membership was started.'}

    def revoke(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {'status': 'setup_revoked', 'runtime_started': False}
        with private_lock(self.path.with_suffix('.lock')):
            value = self.read()
            value['active'] = False
            save_private(self.path, value)
        return {'status': 'setup_revoked', 'runtime_started': False,
                'notice': 'Derived grants are fenced. Already accepted native turns and hosted deliverables are retained; native tool settings grant no runtime by themselves.'}

    def recall(self, thread):
        value = self.read()
        if not self.active(value, thread):
            return None
        lifetime = {'expires_at': value['expires_at']} if value['expires_at'] else {'runtime_policy': {'mode': 'connection'}}
        return {'provider': value['provider'], 'consent': {'native_access': True, 'existing_provider_usage': True, **lifetime}}

    def attach(self, path, config, thread):
        """Caller holds the policy lock through connect; no claim is a grant."""
        value = self.read()
        if not self.active(value, thread) or provider(config) != value['provider']:
            raise Blocked('Setup review is missing or provider/account metadata changed; keep this chat disabled and review the changed scope')
        claims = value.get('claims', [])
        active = 0
        retained = []
        from ..runtime_policy import allowed
        for claim in claims:
            saved_path = Path(claim)
            if saved_path == path:
                continue
            try:
                saved = load_private(saved_path)
            except FileNotFoundError:
                continue
            if allowed(saved):
                active += 1
                retained.append(claim)
        if active >= 3:
            raise Blocked('Three Pantheon connections already use setup; finish or withdraw one before connecting another')
        value['claims'] = retained + [str(path)]
        save_private(self.path, value)
        config['codex_setup_policy'] = {'path': str(self.path), 'id': value['id']}
        return config


def for_config(config):
    ref = config.get('codex_setup_policy')
    if not isinstance(ref, dict) or set(ref) != {'path', 'id'}:
        raise Blocked('Invalid setup preference reference')
    expected = home() / 'pantheon' / 'setup-policy.json'
    if Path(ref['path']) != expected:
        raise Blocked('Setup preference must remain in this native profile')
    policy = Policy(expected)
    value = policy.read()
    if (not policy.active(value, config.get('binding', {}).get('session'))
            or value['id'] != ref['id']):
        raise Blocked('Setup preference was revoked, changed or expired')
    return value


def allowed(config):
    try:
        for_config(config)
        return True
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        return False


def require_tool(config, name, args):
    if 'codex_setup_policy' not in config:
        return
    # Cleanup remains available after revocation, without granting new activity.
    if name in ('pantheon_doctor', 'pantheon_finish_wake') or (name == 'pantheon_command' and args.get('op') == 'withdraw'):
        return
    value = for_config(config)
    capability = 'transfers' if name in codex_trust.TRANSFERS else 'sites' if name in codex_trust.SITES else None
    if capability:
        if value.get(capability) is not True:
            raise Blocked('This capability was not selected during setup; review setup before using it')
        local = args.get('directory' if capability == 'sites' else 'path')
        if local is not None:
            target = Path(local)
            folder = Path(value['folders'][capability])
            if (not target.is_absolute() or any(p.is_symlink() for p in (target, *target.parents))
                    or not target.resolve().is_relative_to(folder)):
                raise Blocked('Local file is outside the folder selected during setup')
    elif name not in (*codex_trust.ROUTINE, 'pantheon_join'):
        # Newly published remote tools keep the native default prompt. Setup
        # never adds them to the exact host allowlist. They cannot introduce a
        # local primitive or bypass server membership authority.
        from .catalog import for_origin
        catalogue = for_origin(config['url'])
        catalogue.refresh()
        descriptor = catalogue.lookup(name)
        if descriptor['execution'] != 'remote' or descriptor['authority'] != 'chat.member':
            raise Blocked('This operation is outside the reviewed setup contract')
