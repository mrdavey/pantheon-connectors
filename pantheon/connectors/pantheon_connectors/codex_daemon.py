"""Capability-checked stock CLI discovery. Never launches or resumes an owner.

Only the official deterministic symlink is permitted, validated and pinned before
connecting to its protected target. Explicit-owner symlink rules are unchanged.
"""
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import uuid
from .codex_delivery import DEFAULT_DAEMON, selected
from .codex_output import Owner, socket_identity
from .worker import Blocked

from .codex_compatibility import require_capabilities, TESTED_VERSION

SUPPORTED_VERSION = TESTED_VERSION  # Historical test fixture label, not an allowlist.


def native_identity(meta):
    if not isinstance(meta, dict):
        raise Blocked('Native per-call identity is required')
    thread = meta.get('threadId')
    try:
        uuid.UUID(thread)
    except (ValueError, TypeError, AttributeError):
        raise Blocked('Native per-call thread ID is missing') from None
    lineage = meta.get('x-codex-turn-metadata', {})
    if not isinstance(lineage, dict) or lineage.get('thread_id', thread) != thread:
        raise Blocked('Native thread metadata conflicts')
    return thread


def native_home():
    return pathlib.Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser().resolve(strict=True)


def _identity(path):
    info = path.lstat()
    return dict(device=info.st_dev, inode=info.st_ino, created_ns=info.st_ctime_ns)


def _directory(path, *, private=False):
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
            raise Blocked('Daemon directory ownership/type changed')
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise Blocked('Daemon directory can be replaced by another user')
    info = path.lstat()
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise Blocked('Daemon rendezvous directories must be owner-only 0700')


def _protected_root():
    return pathlib.Path('/tmp').resolve(strict=True) / f'codex-daemon-{os.getuid()}'


def validate_endpoint(home, alias):
    home = pathlib.Path(home)
    expected = home / 'app-server-control' / 'app-server-control.sock'
    if not home.is_absolute() or pathlib.Path(alias) != expected:
        raise Blocked('Discovery did not return this Codex home default endpoint')
    _directory(expected.parent, private=True)
    info = expected.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise Blocked('Expected the user-owned official daemon alias')
    protected = _protected_root()
    _directory(protected, private=True)
    target = protected / hashlib.sha256(os.fsencode(str(expected))).hexdigest()
    if os.readlink(expected) != str(target):
        raise Blocked('Official daemon alias target differs from its deterministic protected path')
    identity = socket_identity(target)
    if stat.S_IMODE(target.lstat().st_mode) != 0o600:
        raise Blocked('Daemon target socket must have mode 0600')
    return dict(alias=str(expected), alias_identity=_identity(expected), target=str(target),
                target_identity=identity, alias_directory={k: v for k, v in _identity(expected.parent).items() if k != 'created_ns'},
                protected_directory={k: v for k, v in _identity(protected).items() if k != 'created_ns'})


def discover(executable, home):
    home = pathlib.Path(home)
    if home != native_home() or not executable or shutil.which('codex') != executable:
        raise Blocked('Discovery must use this native environment and Codex executable')
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR') if key in os.environ}
    environment['CODEX_HOME'] = str(home)
    version = subprocess.run([executable, '--version'], env=environment, capture_output=True,
                             text=True, check=True, timeout=10).stdout.strip()
    if not version.startswith('codex-cli ') or not 0 < len(version[10:]) <= 128:
        raise Blocked('Native Codex version is unavailable')
    native_version = version.removeprefix('codex-cli ')
    require_capabilities(executable)
    result = subprocess.run([executable, 'app-server', 'daemon', 'version'], env=environment,
                            capture_output=True, text=True, check=True, timeout=10)
    value = json.loads(result.stdout)
    if (value.get('status') != 'running' or
            value.get('cliVersion') != native_version or
            not isinstance(value.get('appServerVersion'), str) or not 0 < len(value['appServerVersion']) <= 128 or
            value.get('managedCodexVersion') != value.get('appServerVersion') or
            value.get('managedCodexPath') != str(home / 'packages/app-server-daemon/current/bin/codex')):
        raise Blocked('Official discovery version or managed runtime differs')
    require_capabilities(value['managedCodexPath'])
    return dict(validate_endpoint(home, value.get('socketPath', '')), runtime_version=value['appServerVersion'])


def thread_usage(thread):
    # source/originator describe creation, not the current UI. The default
    # daemon can report appServer/custom even for the ordinary CLI. Do not use
    # that field as Desktop detection or silently infer another user surface.
    if thread.get('parentThreadId'):
        raise Blocked('This route requires the selected main conversation')
    value = dict(model=thread.get('model'), model_provider=thread.get('modelProvider'))
    if any(not isinstance(v, str) or not v for v in value.values()):
        raise Blocked('Current thread provider/model is unavailable')
    return value


def account_metadata(response):
    """Use only public account/read metadata; never read or refresh credentials.

    Email/type detect some login changes but cannot distinguish workspaces or
    API-key accounts. Do not label this as verified exact account ownership.
    """
    account = response.get('account')
    if not isinstance(account, dict) or account.get('type') not in ('chatgpt', 'apiKey', 'amazonBedrock'):
        raise Blocked('Native account metadata is unavailable; check the existing login without switching accounts')
    result = dict(type=account['type'], exact_account_verified=False)
    if account['type'] == 'chatgpt':
        email = account.get('email')
        if not isinstance(email, str) or not email or len(email) > 512:
            raise Blocked('Native account login metadata is unavailable; preserve the current scope')
        result['login_sha256'] = hashlib.sha256(email.encode()).hexdigest()
    return result


class DefaultDaemonOwner(Owner):
    def __init__(self, config, *, inspection=False):
        if selected(config) != DEFAULT_DAEMON:
            raise Blocked('Default daemon delivery must be selected explicitly')
        if not inspection:
            from .onboarding import trial_allowed
            if config.get('allow_daemon_access') is not True or not trial_allowed(config):
                raise Blocked('Explicit valid default-daemon access is required')
        if config['binding']['profile'] != 'default':
            raise Blocked('Default daemon profile changed')
        actual = discover(config['codex_executable'], pathlib.Path(config['codex_home']))
        if actual.get('runtime_version') != config['binding']['version']:
            raise Blocked('Native runtime changed since preparation; review before rebinding')
        # Older exact-version preparations have no runtime_version in their pin.
        pinned = dict(config.get('daemon_endpoint', {}))
        if 'runtime_version' not in pinned:
            actual = {k:v for k,v in actual.items() if k != 'runtime_version'}
        if actual != pinned:
            raise Blocked('Discovered owner changed; no automatic repinning or reconnect')
        super().__init__(config)

    def check(self):
        status = super().check()
        thread = self.rpc.call('thread/read', {'threadId': self.config['binding']['session'], 'includeTurns': False})['thread']
        self.usage = thread_usage(thread)
        if 'reviewed_provider' in self.config and self.usage != self.config['reviewed_provider']:
            raise Blocked('Thread provider/model changed since approved preparation')
        if self.config.get('capture_account_metadata') is True or 'reviewed_account' in self.config:
            self.account = account_metadata(self.rpc.call('account/read', {'refreshToken': False}))
            if 'reviewed_account' in self.config and self.account != self.config['reviewed_account']:
                raise Blocked('Native account login metadata changed since approval; preserve this connection and review the changed scope')
        return status

    def verify_socket(self):
        pin = validate_endpoint(pathlib.Path(self.config['codex_home']), self.config['daemon_endpoint']['alias'])
        pin['runtime_version'] = self.config['binding']['version']
        if 'runtime_version' not in self.config['daemon_endpoint']:
            pin.pop('runtime_version')
        if (pin != self.config['daemon_endpoint'] or pin['target'] != self.config['app_server_socket']
                or pin['target_identity'] != self.config['owner_socket_identity']):
            raise Blocked('Pinned default-daemon endpoint changed')


def owner_for(config):
    return DefaultDaemonOwner(config) if selected(config) == DEFAULT_DAEMON else Owner(config)
