"""Trusted Codex hooks observe ongoing project work at native boundaries."""
from datetime import datetime, timezone
import json
import sys
import time
from uuid import NAMESPACE_URL, uuid5

from . import codex_project as project
from .client import APIError
from .onboarding import private_lock
from .state import load_private, save_private
from .worker import Blocked


TOOL_NAMES = {"pantheon_project", "mcp__pantheon__pantheon_project"}


def due(assignment):
    value = datetime.fromisoformat(assignment["checkInDue"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) >= value


def handle(event):
    name = event.get("hook_event_name")
    if name not in {"PreToolUse", "Stop"}:
        return {}
    thread = event.get("session_id")
    if not isinstance(thread, str) or not thread:
        return {}
    try:
        path, thread = project.native_scope(thread, create=False)
        account = load_private(path)
    except (FileNotFoundError, Blocked, ValueError):
        return {}
    if account.get("session", {}).get("id") != thread:
        return {}
    if name == "PreToolUse" and event.get("tool_name") in TOOL_NAMES:
        with private_lock(path.with_suffix(".lock")):
            account = load_private(path)
            account["guardAt"] = time.time()
            save_private(path, account)
        return {}
    connection = account.get("focus")
    if not connection:
        return {}
    message = "Project authorization could not be checked. Read Pantheon project status before continuing."
    try:
        with private_lock(path.with_suffix(".lock")):
            account = load_private(path)
            if account.get("focus") != connection:
                return {"decision": "block", "reason": message}
            client = project.authorized_client(path, account, timeout=4)
        project_id, member = connection.split(".")
        route = f"{project.API}/{project_id}/members/{member}/"
        status = client.request(route + "status", {"session": account["session"]})
        head, assignment = status["project"], status.get("assignment")
        if name == "Stop":
            if assignment and assignment["state"] in {"paused", "stopped"} and assignment["controlStatus"] == "pending":
                nonce = str(uuid5(NAMESPACE_URL, f"pantheon:{connection}:{assignment['id']}:{assignment['controlVersion']}"))
                client.request(route + "work", {"session": account["session"], "command": {
                    "op": "ack_control", "key": nonce, "assignmentId": assignment["id"],
                    "expected": assignment["controlVersion"], "controlStatus": "confirmed",
                }})
                return {}
            if (not assignment or not status.get("workHooks") or head["closed"]
                    or head["boardPaused"] or assignment["objectiveVersion"] != head["objectiveVersion"]
                    or assignment.get("blocker")):
                return {}
            if assignment["state"] == "waiting" and assignment["scope"] == "board" and not assignment.get("waitForTasks"):
                return {"decision": "block", "reason": "Read the eligible task list and claim work or complete the board assignment."}
            if assignment["state"] != "running":
                return {}
            return {"decision": "block", "reason": (
                "The Pantheon assignment is still running. Check current status and report due progress before continuing."
            )}
        if (not status.get("workHooks") or head["closed"] or head["boardPaused"] or not assignment
                or assignment["state"] != "running" or assignment["objectiveVersion"] != head["objectiveVersion"]):
            message = "There is no authorized running task. Read Pantheon project status."
        elif due(assignment):
            message = "Check in with Pantheon before starting another action."
        else:
            return {}
    except APIError as error:
        if name == "Stop":
            if error.status == 403:
                try:
                    receipt = client.request(route + "stop_receipt", {"session": account["session"]})
                    if receipt["status"] != "confirmed":
                        client.request(route + "stop_receipt", {
                            "session": account["session"], "assignmentId": receipt["assignmentId"],
                            "version": receipt["version"],
                        })
                except (APIError, KeyError, TypeError):
                    pass
            return {}
    except (Blocked, KeyError, TypeError, ValueError):
        if name == "Stop":
            return {}
    return {"decision": "block", "reason": message}


def main():
    try:
        event = json.load(sys.stdin)
        result = handle(event)
    except (ValueError, TypeError, OSError):
        result = {}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
