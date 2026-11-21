"""Human-reviewed installation preferences, separate from immutable chat grants.

No invitation, membership credential or provider credential is stored here. Native
form acceptance is the only creation path; model arguments cannot grant consent.
"""
import math
import copy
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time

from ..onboarding import private_directory, private_lock
from ..state import load_private, save_private
from ..worker import Blocked
from ..feature_defaults import folders as default_folders
from ..file_security import FileBoundary, absolute_parts
from . import codex_trust
from ..codex_delivery import DEFAULT_DAEMON, GUI_QUEUE

CONTRACT = 'pantheon.codex-setup.v1'
FILE_CAPABILITIES = {
    'pantheon_transfer_send': 'transfers', 'pantheon_transfer_fetch': 'transfers',
    'pantheon_transfer_ack': 'transfers', 'pantheon_site_deploy': 'sites',
}
TOOL = {'name': 'pantheon_setup', 'description':
    'Review Pantheon setup once using the native human form. Reuse active setup for deliberate human invitations; status is read-only, revoke stops derived runtime grants. Model arguments never approve permissions.',
    'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string',
        'enum': ['review', 'status', 'revoke', 'change']},
        'expires_at': {'type': 'number', 'description': 'Optional original human deadline limiting a new review and all its derived grants; never permission or renewal.'},
        'client': {'type': 'string', 'enum': ['cli', 'desktop'], 'description': 'Previously identified client for review/change only. Routing hint, not permission; native acceptance and runtime binding remain required.'}},
        'required': ['action'], 'additionalProperties': False}}
CONNECT_TOOL = {'name': 'pantheon_connect', 'description':
    'Connect the exact invitation deliberately supplied by the human. First use opens the native setup form and requires normal same-conversation reload before joining; '
    'later invitations reuse reviewed profile preferences with fresh chat grants. Do not ask an extra textual '
    'consent question or supply approval arguments. Optional expires_at only narrows runtime to an existing '
    'human-approved deadline; it never grants permission. Retries preserve the original grant. Read and explicitly '
    'ACK actual input after connecting.',
    'inputSchema': {'type': 'object', 'properties': {
        'link': {'type': 'string'}, 'deliberate': {'type': 'boolean'}, 'name': {'type': 'string'},
        'expires_at': {'type': 'number', 'description': 'Optional original UTC epoch deadline; never renew on retry.'},
    }, 'required': ['link', 'deliberate'], 'additionalProperties': False}}
COLLABORATION_DESCRIPTION = (
    'Receive replies and allow routine messaging in chats you invite Codex to, using this conversation at normal usage costs. '
    'Other permissions stay unchanged.')
DAEMON_DESCRIPTION = ('CLI: allow broader local Codex access, used only for invited chats. '
    'Your exact billing account may not be visible.')
DESKTOP_DESCRIPTION = ('Desktop: automated notifications appear as messages and may still arrive after you revoke access. '
    'Pantheon cannot verify your billing account.')
FOLDER_FIELDS = {'transfers': 'transfer_folder', 'sites': 'site_folder'}
FORM = {'type': 'object', 'properties': {
    # Codex CLI 0.162.1 rejects the entire form when any field is numeric.
    # Its supported single-select string enum preserves a real native choice.
    'minutes': {'type': 'string', 'title': 'Time limit',
        'enum': ['0', '15', '30', '45', '60'],
        'enumNames': ['Until each chat ends', '15 minutes', '30 minutes', '45 minutes', '60 minutes'], 'default': '0',
        'description': 'Stops when the agent leaves or the chat ends or expires. A timed limit starts when you save, includes later invitations and never resets on retry.'},
    'transfers': {'type': 'string', 'title': 'File handoff',
        'description': 'Send or receive files with one collaborator when your task and chat allow it.'},
    'sites': {'type': 'string', 'title': 'Public website publishing',
        'description': 'Publish from the selected folder. Published sites may stay online after the chat ends.'},
}, 'required': ['minutes', 'transfers', 'sites']}
MESSAGE = ('Access is saved for future invitations. '
           'Ask to change Pantheon setup or revoke access anytime.')
DEFAULTS_DESCRIPTION = (
    'Access stops when each chat ends, expires or the agent leaves. '
    'File handoff and public website publishing use dedicated Pantheon folders, '
    'only when your task and chat allow them. Published sites may stay online.')

# Only a package that explicitly supports both transports offers this choice.
# Existing daemon-only preferences keep their original semantics on upgrade.
GUI_FORM = copy.deepcopy(FORM)
GUI_FORM['properties'] = {'host': {'type': 'string', 'title': 'Where are you using Codex?',
    'enum': ['cli', 'desktop'], 'enumNames': ['CLI', 'Desktop']}, **GUI_FORM['properties']}
GUI_FORM['required'] = ['host', *FORM['required']]


def validate_connect(args):
    from . import connect
    if not isinstance(args, dict) or set(args) - set(CONNECT_TOOL['inputSchema']['properties']):
        raise Blocked('Use the invitation and optional expires_at; permissions come only from native setup review')
    connect.validate({k: v for k, v in args.items() if k != 'expires_at'})
    if 'expires_at' in args:
        validate_deadline(args['expires_at'])


def validate_deadline(end):
    now = time.time()
    if type(end) not in (int, float) or not math.isfinite(end) or not now < end <= now + 3600:
        raise Blocked('The original deadline must be in the next hour; never renew it on retry')


def home():
    return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser().resolve()


def provider(config):
    return {'provider': config.get('reviewed_provider'), 'account': config.get('reviewed_account')}


def accepted_fields(response, form):
    if not isinstance(response, dict) or response.get('action') != 'accept':
        return None
    value = response.get('content')
    # Codex returns null content for a form with no fields.
    if value is None and not form['required']:
        value = {}
    if not isinstance(value, dict) or set(value) != set(form['required']):
        raise Blocked('The native setup response is incomplete; no permissions were saved')
    for name, field in form['properties'].items():
        if type(value[name]) is not str or ('enum' in field and value[name] not in field['enum']):
            raise Blocked('The native setup response has an invalid choice; no permissions were saved')
    return value


def preferences(value, folders, creatable, *, end, remember=True):
    result = {'remember': remember, 'expires_at': end, 'folders': {}}
    if 'host' in value:
        result['delivery'] = GUI_QUEUE if value['host'] == 'desktop' else DEFAULT_DAEMON
    for capability in FOLDER_FIELDS:
        result[capability] = capability in folders
        if capability not in folders:
            continue
        folder = folders[capability]
        if not isinstance(folder, str) or not Path(folder).is_absolute():
            raise Blocked('Enter an existing absolute folder path; no permissions were saved')
        path = Path('/', *absolute_parts(folder))
        if (any(p.is_symlink() for p in (path, *path.parents))
                or (not path.is_dir() and folder != creatable.get(capability))
                or (path.exists() and not path.is_dir())):
            raise Blocked('Select a specific existing folder without symlinks; no permissions were saved')
        result['folders'][capability] = str(path)
    return result


def review_changes(elicit, message, form, previous, managed):
    previous = previous or {}
    saved = previous.get('folders', {})
    for capability in FOLDER_FIELDS:
        options = {'default': 'Use Pantheon folder', 'off': 'Off', 'custom': 'Choose a folder'}
        default = 'default' if previous.get(capability, True) else 'off'
        folder = saved.get(capability)
        if folder and folder != managed[capability]:
            if not isinstance(folder, str):
                raise Blocked('The saved setup folder is invalid')
            options = {'saved': 'Keep current folder', **options}
            form['properties'][capability]['description'] += '\nCurrent folder is ' + folder
            if default == 'default':
                default = 'saved'
        form['properties'][capability].update(enum=list(options), enumNames=list(options.values()), default=default)
    response = elicit(message, form)
    value = accepted_fields(response, form)
    if value is None:
        return response, None

    custom = {capability: field for capability, field in FOLDER_FIELDS.items() if value[capability] == 'custom'}
    paths = {}
    if custom:
        fields = {field: {'type': 'string', 'title': form['properties'][capability]['title'] + ' folder',
                          'minLength': 1} for capability, field in custom.items()}
        folder_form = {'type': 'object', 'properties': fields, 'required': list(fields)}
        response = elicit('Enter the absolute path to each selected folder. Each folder must already exist.',
                          folder_form)
        paths = accepted_fields(response, folder_form)
        if paths is None:
            return response, None

    folders = {}
    for capability, field in FOLDER_FIELDS.items():
        mode = value[capability]
        if mode == 'custom':
            folders[capability] = paths[field]
        elif mode == 'saved':
            folders[capability] = saved[capability]
        elif mode == 'default':
            folders[capability] = managed[capability]
    minutes = int(value['minutes'])
    creatable = {capability: folder for capability, folder in managed.items() if capability not in custom}
    return response, preferences(value, folders, creatable, end=time.time() + 60 * minutes if minutes else 0)


def access_choices(response, now, form, *, previous, managed):
    """Save preferences only after real native acceptance of the access request."""
    value = accepted_fields(response, form)
    if value is None:
        return None
    previous = previous or {}
    end = previous.get('expires_at', 0)
    if end and end <= now:
        raise Blocked('The saved time limit has expired. Use Change setup to choose a new limit.')
    saved = previous.get('folders', {})
    folders = {capability: saved.get(capability, managed[capability])
               for capability in FOLDER_FIELDS if previous.get(capability, True)}
    return preferences(value, folders, managed, end=end, remember=previous.get('remember', True))


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
        active = self.active(value, thread)
        reason = ('missing' if not value else 'revoked' if not value.get('active') else
                  'expired' if value.get('expires_at') and value['expires_at'] <= time.time() else
                  'conversation_restricted' if not active else '')
        return {'status': 'setup_active' if active else 'setup_required',
                'reason': reason, 'policy_version': CONTRACT,
                **({'client': 'desktop' if value.get('delivery') == GUI_QUEUE else 'cli',
                    'delivery': value.get('delivery', DEFAULT_DAEMON)} if value else {}),
                'runtime_started': False, 'preference_is_not_a_chat_grant': True,
                **({k: value[k] for k in ('remember', 'expires_at', 'transfers', 'sites', 'folders')} if value else {})}

    def review(self, thread, elicit, inspect, *, change=False, expires_at=None, choose_host=False, client=None):
        if client not in (None, 'cli', 'desktop') or (client == 'desktop' and not choose_host):
            raise Blocked('Unsupported setup client')
        if expires_at is not None:
            validate_deadline(expires_at)
        if not change and self.active(self.read(), thread):
            status = self.status(thread)
            if client is not None and client != status['client']:
                raise Blocked('Saved client differs; use setup change for native review of this transport')
            return status
        # Nothing sensitive is inspected and no native policy is changed before
        # the real client's correlated acceptance response.
        message = ('Changing setup stops access to existing chats. ' if change else '') + MESSAGE
        managed = default_folders(self.home / 'pantheon')
        previous = self.read()
        if not change and previous and previous.get('expires_at') and previous['expires_at'] <= time.time():
            raise Blocked('The saved time limit has expired. Use Change setup to choose a new limit.')
        selected_client = client or (('desktop' if previous.get('delivery') == GUI_QUEUE else 'cli') if previous else None)
        form = copy.deepcopy(GUI_FORM if choose_host and selected_client is None else FORM)
        if selected_client:
            disclosure = DESKTOP_DESCRIPTION if selected_client == 'desktop' else DAEMON_DESCRIPTION
            message = 'Client: ' + ('Desktop' if selected_client == 'desktop' else 'CLI') + '. ' + message
        else:
            disclosure = DAEMON_DESCRIPTION + ('\n\n' + DESKTOP_DESCRIPTION if choose_host else '')
        message = COLLABORATION_DESCRIPTION + '\n\n' + disclosure + '\n\n' + message
        if change and expires_at is not None:
            form['properties']['minutes']['description'] += (
                '\nMaximum deadline: ' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(expires_at))
                + ', including later invitations. An earlier selected limit still applies.')
        if change:
            response, selected = review_changes(elicit, message, form, previous, managed)
        else:
            form = {'type': 'object', 'properties': {
                k: v for k, v in form['properties'].items() if k == 'host'},
                'required': ['host'] if 'host' in form['properties'] else []}
            message += '\n\n' + (DEFAULTS_DESCRIPTION if not previous else
                'Your saved time limit, file settings and conversation scope stay unchanged.')
            deadline = min(x for x in (expires_at, (previous or {}).get('expires_at'), float('inf')) if x)
            if math.isfinite(deadline):
                message += '\nAccess ends by ' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(deadline)) + '.'
            response = elicit(message, form)
            selected = access_choices(response, time.time(), form, previous=previous, managed=managed)
        if selected is None:
            action = response.get('action') if isinstance(response, dict) else None
            if action == 'decline':
                reason = 'native_form_declined'
            elif action == 'cancel':
                reason = 'native_form_cancelled'
            else:
                reason = 'native_form_unanswered'
            return {'status': 'setup_declined', 'reason': reason,
                    'runtime_started': False, 'membership_started': False}
        if selected_client:
            selected['delivery'] = GUI_QUEUE if selected_client == 'desktop' else DEFAULT_DAEMON
        if expires_at is not None:
            validate_deadline(expires_at)
            selected['expires_at'] = min(selected['expires_at'] or expires_at, expires_at)
        boundaries = {capability: FileBoundary(folder, capability, self.home)
                      for capability, folder in selected['folders'].items()}
        private_directory(self.path.parent, repair_owned=True)
        with private_lock(self.path.with_suffix('.lock')):
            current = self.read()
            if current != previous:
                raise Blocked('Pantheon setup changed during review; check setup status and review the current settings.')
            if not change and self.active(current, thread):
                return self.status(thread)
            with tempfile.TemporaryDirectory(prefix='review-', dir=self.path.parent) as temporary:
                captured = inspect(Path(temporary) / 'setup.json', **({'delivery': selected['delivery']} if choose_host else {}))
            for capability, folder in selected['folders'].items():
                boundary = boundaries[capability]
                try:
                    if folder == managed[capability]:
                        with boundary.directory(boundary.root[:-1]) as parent:
                            name = boundary.root[-1]
                            try:
                                os.mkdir(name, mode=0o700, dir_fd=parent.fd)
                            except FileExistsError:
                                pass
                            with parent.child(name) as child:
                                child.verify()
                                info = os.fstat(child.fd)
                                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                                    raise Blocked('The dedicated Pantheon folder must be owner-only')
                    else:
                        with boundary.directory(boundary.root):
                            pass
                except OSError:
                    raise Blocked('Choose an existing setup folder without symlinks') from None
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

    def connect_arguments(self, thread, args):
        """Derive permissions only after native review; model input can narrow time."""
        validate_connect(args)
        result = {k: v for k, v in args.items() if k != 'expires_at'}
        if 'expires_at' in args:
            reviewed = self.recall(thread)
            if reviewed is None:
                raise Blocked('Native setup review is required before deriving a chat grant')
            consent = reviewed['consent']
            if consent.get('expires_at') and args['expires_at'] > consent['expires_at']:
                raise Blocked('The chat deadline cannot exceed the fixed setup window')
            result['consent'] = {k: v for k, v in consent.items() if k != 'runtime_policy'}
            result['consent']['expires_at'] = args['expires_at']
        return result

    def attach(self, path, config, thread):
        """Caller holds the policy lock through connect; no claim is a grant."""
        value = self.read()
        if (not self.active(value, thread) or provider(config) != value['provider']
                or config.get('delivery') != value.get('delivery', DEFAULT_DAEMON)):
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
            or value['id'] != ref['id']
            or config.get('delivery') != value.get('delivery', DEFAULT_DAEMON)):
        raise Blocked('Setup preference was revoked, changed or expired')
    return value


def allowed(config):
    try:
        for_config(config)
        return True
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        return False


def require_tool(config, name, args):
    local_capability = FILE_CAPABILITIES.get(name)
    if 'codex_setup_policy' not in config:
        if local_capability:
            raise Blocked('Local file operations require a folder approved in native Pantheon setup')
        return
    # Cleanup remains available after revocation, without granting new activity.
    if name in ('pantheon_doctor', 'pantheon_finish_wake') or (name == 'pantheon_command' and args.get('op') == 'withdraw'):
        return
    value = for_config(config)
    capability = 'transfers' if name in codex_trust.TRANSFERS else 'sites' if name in codex_trust.SITES else None
    if capability:
        if value.get(capability) is not True:
            raise Blocked('This capability was not selected during setup; review setup before using it')
        if local_capability:
            folders = value.get('folders', {})
            folder = folders.get(local_capability)
            if not folder:
                raise Blocked('Select a folder for this capability in native Pantheon setup')
            native_home = Path(value['home'])
            boundary = FileBoundary(folder, local_capability, native_home)
            site = local_capability == 'sites'
            boundary.target(args.get('directory' if site else 'path'), directory=site)
            return boundary
    elif name not in (*codex_trust.ROUTINE, 'pantheon_join', 'pantheon_resume'):
        # Resume is a local tool, guarded by the active setup above and the
        # existing connection's runtime/membership checks in native_cli.
        # Newly published remote tools keep the native default prompt. Setup
        # never adds them to the exact host allowlist. They cannot introduce a
        # local primitive or bypass server membership authority.
        from .catalog import for_origin
        catalogue = for_origin(config['url'])
        catalogue.refresh()
        descriptor = catalogue.lookup(name)
        if descriptor['execution'] != 'remote' or descriptor['authority'] != 'chat.member':
            raise Blocked('This operation is outside the reviewed setup contract')
