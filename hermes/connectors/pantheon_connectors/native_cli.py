"""Codex native skill command. Uses host-provided CODEX_THREAD_ID, never an argument.

JSON tool input is read from stdin so invitation fragments are not process arguments.
The queue surface sends official CLI notices; the older receiver uses a shared owner.
"""
import argparse
import json
import os
import pathlib
from .codex import AttachedAppServer, CodexAdapter
from .onboarding import Onboarding, dispatch, validate_arguments
from .state import load_private, save_private
from .worker import Blocked
from .codex_delivery import ACTIVE_WAIT, DEFAULT_DAEMON, GUI_QUEUE, selected, require_queue, preparation


def fence_hosted_runtime(setup_file, config, connection):
    """Disable only this invitation's local access; retain its original grant."""
    if setup_file is None or config.get('hosted_consent') is not True:
        return False  # Legacy setups may be shared by multiple memberships.
    from .onboarding import private_lock
    from .runtime_policy import same_scope
    path = pathlib.Path(setup_file)
    with private_lock(path.parent / 'setup.lock'):
        current = load_private(path)
        grant = current.get('hosted_runtime_grant')
        if (not same_scope(current, config)
                or current.get('invitation_connection') != connection
                or not isinstance(grant, dict) or grant.get('connection') != connection):
            raise Blocked('Withdrawal does not match the saved hosted runtime scope')
        current.update(allow_runtime_turns=False, allow_daemon_access=False, allow_tool_output=False)
        save_private(path, current)
    return True


def binding_for(config, native_thread=None):
    binding = config.get("binding", {})
    thread = native_thread if native_thread is not None else os.environ.get("CODEX_THREAD_ID")
    if not thread or thread != binding.get("session"):
        raise Blocked("This native command is not in the owner-selected Codex thread")
    if binding.get("runtime") != "codex" or binding.get("surface") not in ("app-server", "native-queue", "app-server-tool-output"):
        raise Blocked("An explicit Codex native binding is required")
    if binding.get("surface") in ("native-queue", "app-server-tool-output"):
        native_home = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
        if native_home != pathlib.Path(config['codex_home']).expanduser().resolve():
            raise Blocked("Selected Codex home differs from this native shell")
    selected(config)
    return binding


def preflight(config, binding):
    if selected(config) == ACTIVE_WAIT:
        return  # Native binding checked; no queue/UI/provider or delivery claim.
    if binding['surface'] == 'app-server-tool-output':
        from .codex_daemon import owner_for
        owner = owner_for(config)
        owner.close()
        return
    if binding['surface'] == 'native-queue':
        from .codex_queue import QueueCLI
        if not QueueCLI(config).available() and config.get('allow_ui_fallback') is not True:
            raise Blocked('Queue absent and UI fallback not approved')
        return
    rpc = AttachedAppServer(config["app_server_socket"], binding["version"], binding["profile"],
                            config.get("codex_executable", "codex"))
    try:
        CodexAdapter(rpc, binding)  # Read only; no resume or turn/start.
    finally:
        rpc.close()


def execute(config, name, args, *, native_thread=None, setup_file=None):
    from .runtime_policy import same_scope
    from .onboarding import trial_allowed
    def current_scope():
        current = load_private(setup_file) if setup_file is not None else config
        if not same_scope(current, config):
            raise Blocked('Local runtime scope changed; preserve this connection and check current authorization')
        return current
    def allowance():
        return trial_allowed(current_scope())
    binding = binding_for(config, native_thread)
    from .broker.codex_setup_policy import require_tool
    require_tool(config, name, args)
    if name in ('pantheon_begin_wake', 'pantheon_finish_wake') and binding['surface'] == 'app-server-tool-output':
        from .codex_output import OutputNotices
        if not isinstance(args, dict) or set(args) != {'connection', 'wake_id'} or any(type(v) is not str for v in args.values()):
            raise Blocked('Expected connection and wake_id from the actual tool-output notice')
        if name == 'pantheon_finish_wake' and not allowance():
            return OutputNotices(config).fenced_wake_status(
                args['connection'], args['wake_id'], native_thread=native_thread,
                scope_check=current_scope)
        preflight(config, binding)
        return OutputNotices(config, unchanged=allowance).wake(args['connection'], args['wake_id'], finish=name == 'pantheon_finish_wake', native_thread=native_thread)
    if name == 'pantheon_wait_for_unread' and binding['surface'] == 'native-queue':
        from .codex_wait import wait_for_unread
        return wait_for_unread(config, binding, args)
    if name in ('pantheon_begin_wake', 'pantheon_finish_wake') and binding['surface'] == 'native-queue':
        require_queue(config)
        from .codex_queue import Notices
        if not isinstance(args, dict) or set(args) != {'connection', 'wake_id'} or any(type(v) is not str for v in args.values()):
            raise Blocked('Expected connection and wake_id from the Pantheon notice')
        if name == 'pantheon_finish_wake' and selected(config) == GUI_QUEUE and not allowance():
            return Notices(config).fenced_wake_status(args['connection'], args['wake_id'], scope_check=current_scope)
        return Notices(config, unchanged=allowance).wake(args['connection'], args['wake_id'], finish=name == 'pantheon_finish_wake')
    if name == 'pantheon_pair_panel' and binding['surface'] == 'native-queue':
        require_queue(config)
        from .codex_queue import Notices
        if set(args) != {'token'} or not isinstance(args['token'], str):
            raise Blocked('Expected the current panel pairing token')
        return Notices(config).panel_confirm(args['token'])
    def check(selected):
        if selected != binding:
            raise Blocked("Native binding changed")
        if name != 'pantheon_doctor' and not allowance():
            raise Blocked('Runtime authorization is absent, disabled or expired')
        preflight(config, selected)
        if name != 'pantheon_doctor' and not allowance():
            raise Blocked('Runtime authorization ended during native preflight')
    def doctor():
        current = current_scope()
        if (selected(current) == GUI_QUEUE and not trial_allowed(current)) or (selected(current) == DEFAULT_DAEMON and (
                not trial_allowed(current) or current.get('allow_daemon_access') is not True
                or current.get('allow_tool_output') is not True)):
            return dict(status='local_runtime_inactive', runtime_authorized=False,
                        daemon_inspected=False, runtime_verified=False,
                        next_action='stop_inactive_grant' if current.get('hosted_runtime_grant') else 'review_missing_consent',
                        notice='Local state only: runtime access is inactive. No network or native inspection was performed; no delivery, receipt or remote withdrawal is inferred. Preserve the existing grant; do not renew it for diagnostics.')
        check(binding)
        if selected(config) == ACTIVE_WAIT:
            return dict(status="active_wait_binding_checked", released=False,
                        delivery=ACTIVE_WAIT, runtime_verified=False, autonomous_idle_wake=False,
                        notice="Identity and delivery intent checked only. After exact bounded approval, join/drain and invoke one finite wait in this active turn. No runtime delivery has been tested.")
        if selected(config) == DEFAULT_DAEMON:
            return dict(status="default_daemon_preflight_passed", released=False, runtime_verified=False,
                        notice="No turn requested. After scoped join the plugin manages its bounded receiver.")
        if selected(config) == GUI_QUEUE:
            return dict(status='queue_preflight_passed', runtime_verified=False,
                        notice='No turn requested. The plugin manages the receiver after scoped join. Queue acceptance alone is not an agent receipt or ACK.')
        return {"status": "queue_preflight_passed" if binding['surface'] == 'native-queue' else "shared_owner_preflight_passed", "released": False,
                "notice": "No turn requested. The owner must approve the bounded trial and start its receiver."}
    service = Onboarding(config)
    service.allowance_check = allowance
    # Withdrawing an existing membership needs its authenticated binding, but
    # must remain possible after the trial or its queue executable stops working.
    withdrawing = (binding['surface'] in ('native-queue', 'app-server-tool-output') and name == 'pantheon_command'
                   and isinstance(args, dict) and args.get('op') == 'withdraw')
    if name not in ("pantheon_preview", "pantheon_join", "pantheon_doctor") and not withdrawing:
        check(binding)
    if withdrawing:
        from .codex_queue import Notices
        validate_arguments(name, args)
        service.member(args['connection'], binding, allow_expired=True)
        if selected(config) == ACTIVE_WAIT:
            return dispatch(service, name, args, binding, check, doctor)
        if binding['surface'] == 'app-server-tool-output':
            from .codex_output import OutputNotices
            # Fence after binding/member validation but before the remote write.
            # An uncertain response must never leave this scope locally enabled.
            fenced = fence_hosted_runtime(setup_file, config, args['connection'])
            value = OutputNotices(config).withdraw(args['connection'], lambda: dispatch(service, name, args, binding, check, doctor))
            value['wake_cleanup']['private_runtime_disabled'] = fenced
            return value
        fenced = fence_hosted_runtime(setup_file, config, args['connection'])
        value = Notices(config).withdraw(args['connection'], lambda: dispatch(service, name, args, binding, check, doctor))
        value['wake_cleanup']['private_runtime_disabled'] = fenced
        return value
    value = dispatch(service, name, args, binding, check, doctor)
    if name == "pantheon_join" and value.get("connection"):
        value["receiver_binding_file"] = str(pathlib.Path(config["state_dir"]) / (value["connection"] + ".json"))
        if selected(config) in (DEFAULT_DAEMON, GUI_QUEUE):
            value['next'] += ' The plugin owns the bounded receiver; no separate user launch is required.'
        elif binding['surface'] == 'app-server-tool-output':
            value['next'] += " Start codex_output_watch with the ORIGINAL setup file and this connection only after explicit shared-owner consent."
        elif selected(config) == ACTIVE_WAIT:
            value["next"] += " Selected bounded-active-wait: after exact approval and readiness/draining, invoke pantheon_wait_for_unread once (at most 20 seconds) in this active turn. Queue/UI delivery is prohibited. Joining/readiness does not verify runtime delivery or idle wake."
        else:
            value["next"] += (" Start codex_watch with the ORIGINAL setup file and this connection; do not use receiver_binding_file." if binding["surface"] == "native-queue" else " Start the bounded receiver using that file in this shared owner's native terminal.")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("setup_file", type=pathlib.Path)
    args = parser.parse_args()
    try:
        request = json.load(__import__("sys").stdin)
        config = load_private(args.setup_file)
        preparation(config.get("delivery"), args.setup_file)
        value = execute(config, request["name"], request.get("arguments", {}), setup_file=args.setup_file)
    except Blocked as error:
        value = {"status": "blocked", "reason": str(error)}
    except Exception:
        value = {"status": "blocked", "reason": "Check private setup and supported native owner; no alternate route selected"}
    print(json.dumps(value))


if __name__ == "__main__":
    main()
