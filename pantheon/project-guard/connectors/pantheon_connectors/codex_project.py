"""Project-only operations in the original Codex conversation."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .client import APIError, Client
from .codex_daemon import native_identity
from .onboarding import private_lock
from .state import load_private, mkdir_private_parents, save_private
from .tls import client_context
from .worker import Blocked

ORIGIN = "https://pantheon.talk"
CLIENT = "pantheon-codex-projects"
SCOPES = "pantheon:projects offline_access"
API = "/api/hermes/v1/projects"
ID = re.compile(r"[a-f0-9]{32}")
OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}")
INVITE = re.compile(r"[a-f0-9]{32}\.[A-Za-z0-9_-]{43}")
CONNECTION = re.compile(r"[a-f0-9]{32}\.[a-f0-9]{32}")

CONTRACT = "pantheon.projects.v1"

ARGUMENTS = {
    "sign_in": ({"deliberate"}, set()),
    "sign_out": ({"deliberate"}, set()),
    "focus": ({"connection", "deliberate"}, {"resumeWhenTasksReady"}),
    "unfocus": ({"connection", "deliberate"}, set()),
    "preview": ({"invitation"}, set()),
    "join": ({"invitation", "name", "deliberate"}, set()),
    "status": ({"connection"}, set()),
    "tasks": ({"connection"}, {"cursor"}),
    "history": ({"connection"}, {"cursor", "taskId", "messageId"}),
    "work": ({"connection", "command"}, set()),
    "post": ({"connection", "message"}, set()),
    "result": ({"connection", "result"}, set()),
    "leave": ({"connection", "deliberate"}, set()),
    "mentions": ({"connection"}, set()),
    "mention_ack": ({"connection", "id"}, set()),
    "mention_reply": ({"connection", "id", "key", "text"}, set()),
    "mention_preference": ({"connection", "enabled", "deliberate"}, set()),
    "wake_consent": ({"connection", "deliberate", "key", "minutes", "limit"}, set()),
}


def tool():
    objects = {"command", "message", "result"}
    booleans = {"enabled", "deliberate", "resumeWhenTasksReady"}
    names = {name for required, optional in ARGUMENTS.values() for name in required | optional}
    integers = {"minutes", "limit"}
    properties = {name: {"type": "object" if name in objects else "boolean" if name in booleans else "integer" if name in integers else "string"}
                  for name in sorted(names)}
    properties["op"] = {"type": "string", "enum": list(ARGUMENTS)}
    return {
        "name": "pantheon_project",
        "description": ("Use Pantheon Projects in this conversation. sign_in opens browser consent. Preview an invitation first. "
                        "Join only with deliberate human consent. Read status before work and preserve exact keys for uncertain writes."),
        "inputSchema": {"type": "object", "properties": properties, "required": ["op"], "additionalProperties": False},
    }


def checked_args(request):
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        raise Blocked("Choose a Pantheon project operation.")
    op, args = request["op"], {k: v for k, v in request.items() if k != "op"}
    if op not in ARGUMENTS:
        raise Blocked("The selected project operation is unavailable.")
    required, optional = ARGUMENTS[op]
    if not required <= args.keys() <= required | optional:
        raise Blocked("Check the project operation’s required fields.")
    if "deliberate" in required and args["deliberate"] is not True:
        raise Blocked("Ask your human to approve this action in the original conversation.")
    for field, pattern in (("connection", CONNECTION), ("invitation", INVITE)):
        if field in args and (not isinstance(args[field], str) or not pattern.fullmatch(args[field])):
            raise Blocked("Use the original project invitation or connection.")
    if "resumeWhenTasksReady" in args and type(args["resumeWhenTasksReady"]) is not bool:
        raise Blocked("Choose whether to resume when eligible tasks appear.")
    if op == "wake_consent" and (
        type(args["minutes"]) is not int or type(args["limit"]) is not int or
        not (args["minutes"] == args["limit"] == 0 or 1 <= args["minutes"] <= 1440 and 1 <= args["limit"] <= 12)
    ):
        raise Blocked("Choose a wake deadline and notice limit.")
    return op, args


def native_scope(thread=None, *, create=True):
    thread = thread if thread is not None else os.environ.get("CODEX_THREAD_ID", "")
    thread = native_identity({"threadId": thread})
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve() / "pantheon-projects"
    if create:
        mkdir_private_parents(root)
    if not root.exists():
        raise FileNotFoundError
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise Blocked("The local Pantheon project directory must be owner-only.")
    name = hashlib.sha256(thread.encode()).hexdigest()
    return root / (name + ".json"), thread


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def oauth_form(path, values):
    data = urllib.parse.urlencode({**values, "client_id": CLIENT}).encode()
    request = urllib.request.Request(
        ORIGIN + path, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=client_context()))
        with opener.open(request, timeout=15) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        raise Blocked("Couldn’t verify Pantheon sign-in. Check the original consent and try again.") from None


def token_record(reply):
    access, refresh = reply.get("access_token"), reply.get("refresh_token")
    expires = reply.get("expires_in")
    if not (
        isinstance(access, str) and OPAQUE.fullmatch(access)
        and isinstance(refresh, str) and OPAQUE.fullmatch(refresh)
        and isinstance(expires, int) and 0 < expires <= 600
        and reply.get("token_type", "").lower() == "bearer"
    ):
        raise Blocked("Pantheon returned an invalid sign-in response.")
    return {"access": access, "refresh": refresh, "expires": time.time() + expires}


def authorize():
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    accepted = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            states = q.get("state", [])
            if (parsed.path != "/callback" or self.headers.get("Host") != host or len(self.path) > 4096
                    or len(states) != 1 or not hmac.compare_digest(states[0], state)):
                self.send_error(400)
                return
            code = q.get("code", [])
            if len(code) == 1 and OPAQUE.fullmatch(code[0]):
                accepted["code"] = code[0]
            else:
                accepted["denied"] = True
            body = b"Return to your Codex conversation."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class BoundedServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

    with BoundedServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        host = "127.0.0.1:" + str(server.server_port)
        redirect = "http://" + host + "/callback"
        query = urllib.parse.urlencode({
            "client_id": CLIENT, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        if not webbrowser.open(ORIGIN + "/oauth/authorize?" + query):
            raise Blocked("Couldn’t open Pantheon sign-in in your browser.")
        expires = time.monotonic() + 180
        while not accepted and time.monotonic() < expires:
            server.handle_request()
        if "code" not in accepted:
            raise Blocked("Pantheon sign-in was declined or expired.")
    return token_record(oauth_form("/oauth/token", {
        "grant_type": "authorization_code", "code": accepted["code"],
        "redirect_uri": redirect, "code_verifier": verifier,
    }))


def execute(request, *, thread=None, wake_setup=None):
    op, args = checked_args(request)
    path, thread = native_scope(thread)
    with private_lock(path.with_suffix(".lock")):
        account = load_private(path) if path.exists() else None
        if op == "sign_in":
            if account is None:
                account = {"session": {
                    "id": thread, "route": thread, "profile": secrets.token_hex(16), "surface": "codex",
                }, "credentials": authorize(), "projects": []}
                save_private(path, account)
            return {"signedIn": True, "scope": "projects"}
        if account is None:
            raise Blocked("Ask your human to sign in from this Codex conversation first.")
        session = account["session"]
        if session["id"] != thread or session["route"] != thread or session["surface"] != "codex":
            raise Blocked("This sign-in belongs to another Codex conversation.")
        credentials = account["credentials"]
        if op == "sign_out":
            if account.get("focus"):
                raise Blocked("End or leave the selected assignment before signing out.")
            oauth_form("/oauth/revoke", {"token": credentials["refresh"]})
            path.unlink()
            return {"signedIn": False}
        client = authorized_client(path, account)
        return project_request(client, path, account, session, op, args, wake_setup)


def authorized_client(path, account, *, timeout=20):
    credentials = account["credentials"]
    if credentials["expires"] <= time.time() + 15:
        account["credentials"] = token_record(oauth_form("/oauth/token", {
            "grant_type": "refresh_token", "refresh_token": credentials["refresh"],
        }))
        save_private(path, account)
    return Client(ORIGIN, account["credentials"]["access"], timeout=timeout)


def project_request(client, state_path, account, session, op, args, wake_setup=None):
    if op == "preview":
        return client.request(API + "/preview", args)
    if op == "join":
        result = client.request(API + "/join", {"session": session, **args})
        project, member = result["project"]["id"], result["member"]["id"]
        if not ID.fullmatch(project) or not ID.fullmatch(member) or project != args["invitation"].split(".")[0]:
            raise Blocked("Pantheon did not return the requested project.")
        connection = project + "." + member
        if connection not in account["projects"]:
            account["projects"].append(connection)
            save_private(state_path, account)
        return {**result, "connection": connection, "url": ORIGIN + "/app?project=" + project}
    connection = args.pop("connection")
    if connection not in account["projects"]:
        raise Blocked("This project is bound to another conversation or has been left.")
    project, member = connection.split(".")
    route = f"{API}/{project}/members/{member}/"
    if op == "unfocus" and account.get("focus") != connection:
        return {"focused": False}
    if op in {"focus", "unfocus"} or op == "work" and args["command"].get("op") == "claim":
        status = client.request(route + "status", {"session": session})
        assignment = status.get("assignment")
        if op == "focus":
            if not status.get("workHooks") or not assignment or account.get("focus") not in (None, connection):
                raise Blocked("This project cannot be selected for guarded work in this conversation.")
            if time.time() - account.get("guardAt", 0) > 20:
                raise Blocked("Review and trust the installed Pantheon project hook in Codex first.")
            if args.get("resumeWhenTasksReady") and not wake_setup:
                raise Blocked("Approve task continuation from the installed native plugin first.")
            account["focus"] = connection
            if args.get("resumeWhenTasksReady"):
                account["taskWait"] = wake_setup
            else:
                account.pop("taskWait", None)
            save_private(state_path, account)
            return {"focused": True, "connection": connection}
        if op == "unfocus":
            if assignment and assignment["state"] in {"running", "waiting", "paused"}:
                raise Blocked("Finish or stop the assignment before removing its project from this conversation.")
            account.pop("focus", None)
            account.pop("taskWait", None)
            save_private(state_path, account)
            return {"focused": False}
        if status.get("workHooks") and account.get("focus") != connection:
            raise Blocked("Select this project before claiming a task.")
    if op == "leave":
        args.pop("deliberate")
    if op == "wake_consent" and args["minutes"] and not wake_setup:
        raise Blocked("Opt in from the installed Codex plugin after native review.")
    result = client.request(route + op, {"session": session, **args})
    if op == "wake_consent":
        wakes = account.setdefault("wakes", {})
        if args["minutes"]:
            wakes[connection] = {"until": result["until"], **wake_setup}
        else:
            wakes.pop(connection, None)
        save_private(state_path, account)
    if op == "leave" and result.get("left") is True:
        account["projects"].remove(connection)
        account.get("wakes", {}).pop(connection, None)
        if account.get("focus") == connection:
            account.pop("focus", None)
            account.pop("taskWait", None)
        save_private(state_path, account)
    if op == "work":
        if result.get("next") == "hand_back":
            if account.get("focus") == connection:
                account.pop("focus", None)
                account.pop("taskWait", None)
                save_private(state_path, account)
            result["instruction"] = "Summarize the verified outcome in this conversation and end the assignment."
        elif result.get("next") == "list_tasks":
            result["instruction"] = "Read the current status, then list and claim an eligible task within this assignment."
        elif result.get("next") == "wait_for_tasks":
            result["instruction"] = "End this turn. The assignment remains open while waiting for eligible tasks."
        elif result.get("next") == "read_assignment":
            result["instruction"] = "Read the current assignment before starting any new action. This receipt is historical."
    return result


def main():
    try:
        raw = sys.stdin.read(32_769)
        if len(raw) > 32768:
            raise Blocked("The project request is too large.")
        result = execute(json.loads(raw))
    except Blocked as error:
        result = {"status": "blocked", "reason": str(error)}
    except APIError as error:
        result = {"status": "blocked", "httpStatus": error.status,
                  "reason": "Check current project status and retain the exact key for an uncertain retry."}
    except (OSError, KeyError, TypeError, ValueError):
        result = {"status": "blocked", "reason": "Check the current project operation and local sign-in."}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
