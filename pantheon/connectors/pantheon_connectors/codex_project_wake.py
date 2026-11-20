"""Opted-in project notice transport through the reviewed Codex native owner."""
from datetime import datetime, timezone
import hashlib
import json
import time

from . import codex_project as project
from .client import APIError
from .codex_daemon import owner_for
from .codex_delivery import DEFAULT_DAEMON
from .codex_setup import prepare
from .onboarding import private_lock
from .state import load_private
from .worker import Blocked
from .broker.codex_setup_policy import provider


def prepare_wake(scope, thread, connection, policy, elicit, minutes, limit):
    current = policy.read()
    if not policy.active(current, thread) or current.get("delivery") != DEFAULT_DAEMON:
        raise Blocked("Use approved default-daemon Pantheon setup to enable project wakes in this conversation.")
    if limit:
        message = f"Allow up to {limit} mention alerts for this project within {minutes} minutes."
    else:
        message = "Resume this assignment in the same conversation when an eligible task becomes available."
    response = elicit(
        message + " This can start a turn using your current provider.",
        {"type": "object", "properties": {}, "required": []},
    )
    if response.get("action") != "accept":
        raise Blocked("The native wake request was declined.")
    config = scope.with_name("wake-" + hashlib.sha256(connection.encode()).hexdigest() + ".json")
    if not config.exists():
        prepare(config, project.ORIGIN, "default", delivery=DEFAULT_DAEMON,
                native_thread=thread, inspect_default_daemon=True)
    pin = load_private(config)
    if pin["binding"]["session"] != thread or provider(pin) != current["provider"]:
        raise Blocked("The native model or account differs from the approved setup.")
    return {"config": str(config), "policy": current["id"]}


def allowed(path, connection, thread, policy, deadline):
    if datetime.now(timezone.utc) >= deadline:
        return None
    account = load_private(path)
    wake = account.get("wakes", {}).get(connection)
    reviewed = policy.read()
    if (not wake or not policy.active(reviewed, thread) or wake["policy"] != reviewed["id"]
            or datetime.fromisoformat(wake["until"].replace("Z", "+00:00")) != deadline):
        return None
    return wake


def run(thread, connection, policy, stop):
    path, _ = project.native_scope(thread, create=False)
    account = load_private(path)
    initial = account.get("wakes", {}).get(connection)
    if not initial:
        return
    deadline = datetime.fromisoformat(initial["until"].replace("Z", "+00:00"))
    project_id, member = connection.split(".")
    route = f"{project.API}/{project_id}/members/{member}/"
    after = ""
    while not stop.is_set() and allowed(path, connection, thread, policy, deadline):
        try:
            with private_lock(path.with_suffix(".lock")):
                account = load_private(path)
                client = project.authorized_client(path, account, timeout=24)
            session = account["session"]
            response = client.request(route + "wake_wait", {"session": session, "after": after})
            if response.get("exhausted"):
                return
            after = response.get("id") or after
            notice = response.get("id")
            if not notice:
                stop.wait(5)
                continue
            if not project.ID.fullmatch(notice):
                return
            wake = allowed(path, connection, thread, policy, deadline)
            if not wake:
                return
            config = load_private(wake["config"])
            if (config.get("binding", {}).get("session") != thread or config.get("delivery") != DEFAULT_DAEMON
                    or provider(config) != policy.read()["provider"]):
                return
            config.update(allow_daemon_access=True, allow_tool_output=True, allow_runtime_turns=True,
                          trial_expires_at=min(deadline.timestamp(), time.time() + 3550))
            owner = owner_for(config)
            try:
                while owner.check() != "idle":
                    if stop.wait(5) or not allowed(path, connection, thread, policy, deadline):
                        return
                if stop.is_set() or not allowed(path, connection, thread, policy, deadline):
                    return
                with private_lock(path.with_suffix(".lock")):
                    client = project.authorized_client(path, load_private(path), timeout=24)
                receipt = client.request(route + "wake_try", {"session": session, "id": notice})
                if receipt.get("send") is not True or stop.is_set() or not allowed(path, connection, thread, policy, deadline):
                    continue
                result = owner.send(json.dumps({"type": "project_mention", "project_connection": connection}))
                if result.get("turn", {}).get("id"):
                    client.request(route + "wake_queued", {"session": session, "id": notice})
            finally:
                owner.close()
        except APIError as error:
            if error.status in (400, 401, 403, 404, 410):
                return
            stop.wait(5)
        except (KeyError, OSError, Blocked, ValueError):
            return  # Preserve the server notice after an uncertain send.


def work_setup(path, thread, connection, policy):
    account = load_private(path)
    current = policy.read()
    saved = account.get("taskWait")
    if (not saved or account.get("focus") != connection
            or not policy.active(current, thread) or saved["policy"] != current["id"]):
        return None
    config = load_private(saved["config"])
    if (config.get("binding", {}).get("session") != thread or config.get("delivery") != DEFAULT_DAEMON
            or provider(config) != current["provider"]):
        return None
    now = time.time()
    config.update(allow_daemon_access=True, allow_tool_output=True, allow_runtime_turns=True,
                  trial_expires_at=min(current["expires_at"] or now + 3550, now + 3550))
    return config


def wait_for_work(thread, connection, policy, stop):
    path, _ = project.native_scope(thread, create=False)
    project_id, member = connection.split(".")
    route = f"{project.API}/{project_id}/members/{member}/"
    while not stop.is_set() and work_setup(path, thread, connection, policy):
        try:
            with private_lock(path.with_suffix(".lock")):
                account = load_private(path)
                client = project.authorized_client(path, account, timeout=24)
            session = account["session"]
            hint = client.request(route + "wait_work", {"session": session})
            if not hint.get("assignmentId"):
                continue
            config = work_setup(path, thread, connection, policy)
            if not config or stop.is_set():
                return
            owner = owner_for(config)
            try:
                while owner.check() != "idle":
                    if stop.wait(5) or not work_setup(path, thread, connection, policy):
                        return
                if stop.is_set() or not work_setup(path, thread, connection, policy):
                    return
                with private_lock(path.with_suffix(".lock")):
                    client = project.authorized_client(path, load_private(path), timeout=24)
                status = client.request(route + "status", {"session": session})
                head, assignment = status["project"], status.get("assignment")
                if (assignment and assignment["id"] == hint["assignmentId"] and assignment["version"] == hint["version"]
                        and assignment["waitForTasks"] and status.get("workHooks") and not head["closed"] and not head["boardPaused"]
                        and not stop.is_set() and work_setup(path, thread, connection, policy)):
                    owner.send(json.dumps({"type": "project_task_ready", "project_connection": connection}))
            finally:
                owner.close()
            return
        except APIError as error:
            if error.status in (400, 401, 403, 404, 409, 410):
                return
            stop.wait(5)
        except (KeyError, OSError, Blocked, ValueError):
            return
