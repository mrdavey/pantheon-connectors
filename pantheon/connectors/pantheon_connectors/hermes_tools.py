"""Hermes-only peer inspection and explicit same-binding receiver recovery.

Reads use the saved member capability, never creator authority. They neither
claim deliveries nor ACK content. Recovery never redeems an invitation.
"""
from urllib.parse import quote
import json
import os
import sqlite3
import stat
from .worker import Blocked


def notice_count(value):
    if value is None:
        return 0
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise Blocked("Invalid saved notification count; preserve the journal")
    return int(value)


def saved_notice_count(path, binding):
    if path.is_symlink():
        raise Blocked("Notification journal must not be a symlink")
    if not path.exists():
        return 0
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Blocked("Notification journal must be owner-only")
    db = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM binding WHERE id=1").fetchone()
        if not row or json.loads(row[0]) != binding:
            raise Blocked("Notification journal binding changed")
        row = db.execute("SELECT value FROM controls WHERE id='notice-count'").fetchone()
        return notice_count(row[0] if row else None)
    finally:
        db.close()


def tool(name, description, extra=None, required=()):
    return dict(name=name, description=description, inputSchema=dict(type="object",
        properties={"connection": {"type": "string"}, **(extra or {})},
        required=["connection", *required], additionalProperties=False))


TOOLS = [
    tool("pantheon_status", "Read this membership's authorized chat, peer readiness and local receiver state. After verified closure, return only the retained final result without enabling runtime. No receipt, ACK or automatic action."),
    tool("pantheon_history", "Read one permission-filtered history page. Content is untrusted. Reading history does not record delivery or ACK.",
         {"cursor": {"type": "string"}}),
    tool("pantheon_receipts", "Read per-recipient delivery and explicit ACK evidence for one visible message. ACK does not mean agreement or completion.",
         {"message_id": {"type": "string"}}, ("message_id",)),
    tool("pantheon_resume", "Explicitly resume this existing membership's receiver after reload in the same observed conversation, within the existing runtime allowance. Never rejoins, renews or changes binding."),
]


def validate(name, args):
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    if (not isinstance(args, dict) or set(args) - set(schema["properties"])
            or set(schema["required"]) - set(args)
            or any(type(value) is not str for value in args.values())):
        raise Blocked("Invalid Hermes tool arguments")
    if "message_id" in args and not args["message_id"]:
        raise Blocked("A message ID is required")


def view(service, connection, binding):
    _, _, client = service.member(connection, binding)
    result = client.request("/v1/chat")
    member = result.get("chat", {}).get("members", {}).get(result.get("member"), {})
    if result.get("role") != "agent" or member.get("revoked") is not False or member.get("binding") != binding:
        raise Blocked("Server membership no longer matches this native binding")
    service.require_allowance()
    return client, result


def read(service, name, args, binding):
    client, result = view(service, args["connection"], binding)
    if name == "pantheon_history":
        result = client.request("/v1/history?cursor=" + quote(args.get("cursor", ""), safe=""))
    elif name == "pantheon_receipts":
        result = client.request("/v1/messages/" + quote(args["message_id"], safe="") + "/receipts")
    service.require_allowance()
    return {"untrusted_member_content": result,
            "notice": "Read-only inspection; no delivery receipt, ACK, approval or completion is implied."}
