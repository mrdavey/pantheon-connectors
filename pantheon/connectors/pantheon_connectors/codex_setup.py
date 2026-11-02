"""Capture the current native identity into a DISABLED private trial setup.

Run inside the owner's chosen existing Codex conversation. No UUID argument and
no installation, redemption, permission grant, or message submission.
"""
import argparse
import os
import pathlib
import shutil
import subprocess
import uuid
from .onboarding import origin, private_directory, private_lock
from .state import save_private
from .worker import Blocked
from .codex_delivery import ACTIVE_WAIT, DEFAULT_DAEMON, GUI_QUEUE, MODES, preparation, descriptor


def cli_compatibility():
    """Non-sensitive executable check only; never discover/contact a daemon."""
    executable = shutil.which('codex')
    if not executable:
        raise Blocked('Codex CLI is not on the native PATH')
    result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=10, check=True)
    if not result.stdout.strip().startswith('codex-cli '):
        raise Blocked('Unrecognized Codex version response')
    return executable, result.stdout.strip().removeprefix('codex-cli ')


def prepare(setup_file, url, profile, *, delivery=None, owner_socket=None, native_thread=None, inspect_default_daemon=False, notification_policy=None):
    from .notification_policy import validate
    if notification_policy is not None:
        notification_policy = validate(notification_policy, 8)
    delivery = preparation(delivery, setup_file)
    if notification_policy is None and delivery in (DEFAULT_DAEMON, GUI_QUEUE, "app-server-tool-output"):
        notification_policy = {"mode":"unlimited"}
    if notification_policy is not None and delivery not in (DEFAULT_DAEMON, GUI_QUEUE, "app-server-tool-output"):
        raise Blocked("Explicit notification policy requires tool-output or desktop queue delivery")
    if bool(owner_socket) != (delivery == "app-server-tool-output"):
        raise Blocked("Only shared-owner delivery accepts and requires an explicit owner socket")
    metadata = descriptor()
    if metadata and metadata.get('owner_socket') != (str(owner_socket) if owner_socket else None):
        raise Blocked('Owner socket differs from package intent')
    if delivery == DEFAULT_DAEMON and (inspect_default_daemon is not True or profile != 'default'):
        raise Blocked('Default-daemon inspection requires explicit approval and the default profile')
    identity = native_thread if native_thread is not None else os.environ.get('CODEX_THREAD_ID', '')
    try:
        uuid.UUID(identity)
    except ValueError:
        raise Blocked('Run in the chosen existing Codex native shell; no host thread identity found') from None
    executable, cli_version = cli_compatibility()
    setup_file = pathlib.Path(setup_file).expanduser().absolute()
    private_directory(setup_file.parent)
    with private_lock(setup_file.parent / 'setup.lock'):
        if setup_file.exists() or setup_file.is_symlink():
            raise Blocked('Existing setup preserved; review it instead of overwriting')
        config = dict(url=origin(url), state_dir=str(setup_file.parent / 'codex-state'),
                      codex_executable=str(pathlib.Path(executable).absolute()),
                      codex_home=str(pathlib.Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser().resolve()),
                      delivery=delivery,
                      binding=dict(runtime='codex', surface='native-queue' if delivery in (ACTIVE_WAIT, GUI_QUEUE) else ('app-server-tool-output' if delivery == DEFAULT_DAEMON else delivery), profile=profile, session=identity,
                                   version=cli_version),
                      allow_runtime_turns=False, allow_ui_fallback=False, allow_active_wait=False, trial_expires_at=0, max_notices=0 if delivery == ACTIVE_WAIT else 2)
        if delivery == GUI_QUEUE:
            from .codex_queue import QueueCLI
            config.update(allow_tool_output=False, allow_daemon_access=False,
                          reviewed_provider={'scope': 'existing-desktop-conversation', 'identity_verified': False},
                          reviewed_account=None)
            if not QueueCLI(config).available():
                raise Blocked('Install a supported Codex CLI queue helper on the native PATH; no alternate delivery selected')
        if delivery == 'app-server-tool-output':
            from .codex_output import Owner, socket_identity
            selected_socket = pathlib.Path(owner_socket).expanduser()
            if not selected_socket.is_absolute():
                raise Blocked('Select an absolute explicit owner socket')
            canonical_socket = selected_socket.resolve(strict=True)
            config.update(app_server_socket=str(canonical_socket), requested_owner_socket=str(selected_socket),
                          owner_socket_identity=socket_identity(canonical_socket), allow_tool_output=False)
            owner = Owner(config)  # Read-only identity/profile/loaded-thread checks.
            owner.close()
        if delivery == DEFAULT_DAEMON:
            from .codex_daemon import discover, DefaultDaemonOwner
            endpoint = discover(config['codex_executable'], pathlib.Path(config['codex_home']))
            config['binding']['version'] = endpoint['runtime_version']
            config.update(daemon_endpoint=endpoint, app_server_socket=endpoint['target'],
                          owner_socket_identity=endpoint['target_identity'], allow_tool_output=False,
                          allow_daemon_access=False, max_notices=0)
            config['capture_account_metadata'] = True
            owner = DefaultDaemonOwner(config, inspection=True)
            try:
                config['reviewed_provider'] = owner.usage
                config['reviewed_account'] = owner.account
            finally:
                owner.close()
                config.pop('capture_account_metadata', None)
        if notification_policy is not None:
            config.pop("max_notices", None)
            config["notification_policy"] = notification_policy
        save_private(setup_file, config)
    result = dict(status='prepared_disabled', setup_file=str(setup_file), binding=config['binding'],
                  notice='Preparation enables no runtime and redeems no invitation. Reuse settled consent if the observed scope matches; review only missing or changed scope before activation.')
    if delivery == DEFAULT_DAEMON:
        result.update(reviewed_provider=config['reviewed_provider'],
                      account_scope=dict(exact_account_verified=False,
                          limitation='Native login metadata is pinned, but the host does not expose a stable account/workspace identity for every provider.'),
                      daemon_access_scope='Local daemon API is broader than one conversation. Pantheon must enforce the approved thread, chat, expiry and notification policy.',
                      runtime_enabled=False, receiver='Plugin-owned; no separate user launch')
    return result


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('setup_file')
    parser.add_argument('--url', required=True)
    parser.add_argument('--profile', required=True, help='Owner-confirmed existing profile; default for no named profile')
    parser.add_argument('--delivery', choices=MODES, default=None)
    parser.add_argument('--inspect-default-daemon', action='store_true', help='Only after explicit owner approval of daemon inspection')
    parser.add_argument('--owner-socket', help='Explicit existing private Unix owner socket; never discovered')
    parser.add_argument('--unlimited-notifications', action='store_true', help='Prepare an explicit no-quota policy; runtime remains disabled')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.setup_file, args.url, args.profile, delivery=args.delivery, owner_socket=args.owner_socket, inspect_default_daemon=args.inspect_default_daemon, notification_policy={"mode":"unlimited"} if args.unlimited_notifications else None)))
    except Exception:
        raise SystemExit('Setup preparation blocked; preserve existing setup and verify native host identity/profile.') from None
