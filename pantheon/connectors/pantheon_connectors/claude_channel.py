"""Unreleased Claude channel candidate. Local-test admission only.

The channel carries a fixed hint and opaque routing handles. A stdio write is
never a receipt; only an explicit receive/ACK tool enters the shared protocol.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time

from . import PROTOCOL
from .client import APIError, Client
from .onboarding import Onboarding, invitation, origin, private_directory, private_lock
from .runtime_policy import allowed, same_scope
from .state import Journal, load_private, save_private
from .worker import Blocked

NOTICE = "Pantheon has an update. Fetch it with pantheon_receive, explicitly acknowledge received messages, then act within your owner's permissions."


def local_origin(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(origin(url))
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise Blocked("Claude Code support is Coming soon. This candidate can join only an approved local test.")
    return url.rstrip("/")


class ClaudeChannel:
    def __init__(self, native, *, url="https://pantheon.talk", client_factory=Client, emit=None):
        self.native, self.url, self.client_factory = native, origin(url), client_factory
        self.emit = emit or (lambda _: (_ for _ in ()).throw(Blocked("Channel transport is unavailable.")))
        self.root = private_directory(native.root / "connections")
        self.lock = threading.RLock()
        self.stopped = False

    def path(self, connection):
        if not isinstance(connection, str) or not re.fullmatch(r"[a-f0-9]{32}", connection):
            raise Blocked("Unknown Pantheon connection.")
        return self.root / connection / "setup.json"

    def config(self, connection, *, cleanup=False):
        config = load_private(self.path(connection))
        if config["url"] != self.url:
            raise Blocked("The service for this connection changed.")
        local_origin(config["url"])
        self.native.check(config["binding"], config["native_scope"])
        if not cleanup and (self.stopped or not allowed(config)):
            raise Blocked("Incoming work is paused or its approved time has ended. Check the original connection; permission is not renewed automatically.")
        return config

    def service(self, connection, *, cleanup=False):
        config = self.config(connection, cleanup=cleanup)
        service = Onboarding(config, client_factory=self.client_factory)
        def current():
            latest = self.config(connection)
            return same_scope(latest, config)
        service.allowance_check = current
        return config, service

    def preview(self, link):
        return Onboarding(dict(url=self.url), self.client_factory).preview(link)

    def prepare(self, link, deliberate):
        if deliberate is not True:
            raise Blocked("Paste the invitation and ask to join before preparing this connection.")
        local_origin(self.url)
        token = invitation(link, self.url)
        binding, scope = self.native.binding(), self.native.scope()
        connection = hashlib.sha256((self.url + "\0" + token).encode()).hexdigest()[:32]
        path = self.path(connection)
        private_directory(path.parent)
        with private_lock(path.parent / "setup.lock"):
            if path.exists():
                saved = load_private(path)
                if (saved["url"] != self.url or saved["binding"] != binding
                        or saved["native_scope"] != scope):
                    raise Blocked("This invitation already belongs to another native scope; it cannot be replaced.")
                # Used invitations cannot be previewed; exact retry needs no
                # fresh credential or installation and never renews authority.
                return dict(connection=connection, status="existing_connection",
                            runtime_allowed=allowed(saved), message="The existing connection is preserved.")
            preview = self.preview(link)
            if preview.get("local_test") is not True:
                raise Blocked("This server is not a disposable local test service.")
            save_private(path, dict(url=self.url, binding=binding, native_scope=scope,
                state_dir=str(path.parent / "members"), allow_runtime_turns=False,
                trial_expires_at=0, notification_policy={"mode": "unlimited"}))
        return dict(connection=connection, status="prepared_disabled", scope=preview["scope"],
                    message="Setup is prepared for this conversation. The local test has not started.")

    def authorize(self, connection, deliberate, *, expires_at=None, runtime_policy=None):
        with private_lock(self.path(connection).parent / "setup.lock"):
            config = self.config(connection, cleanup=True)
            if deliberate is not True:
                raise Blocked("The owner must approve incoming local test work first.")
            if (expires_at is None) == (runtime_policy is None):
                raise Blocked("Use the approved end time or connection lifetime, never both.")
            if config.get("authorization_recorded"):
                original = config.get("approved_runtime")
                if original != dict(expires_at=expires_at, runtime_policy=runtime_policy):
                    raise Blocked("The existing approval cannot be widened or renewed.")
                return dict(message="The original approval is preserved.", runtime_allowed=allowed(config))
            now = time.time()
            if runtime_policy is not None:
                if runtime_policy != {"mode": "connection"}:
                    raise Blocked("Unsupported runtime lifetime.")
                config.update(runtime_policy=runtime_policy, runtime_started_at=now,
                              connection_runtime_supported=True, trial_expires_at=0)
            else:
                if type(expires_at) not in (int, float) or not math.isfinite(expires_at) or not now < expires_at <= now + 3600:
                    raise Blocked("The approved local test must end within one hour.")
                config["trial_expires_at"] = expires_at
            config.update(allow_runtime_turns=True, authorization_recorded=True,
                          approved_runtime=dict(expires_at=expires_at, runtime_policy=runtime_policy))
            save_private(self.path(connection), config)
        return dict(message="The local test is approved. Join with the original invitation.", runtime_allowed=True)

    def join(self, connection, link, deliberate, name=None):
        with self.lock:
            config, service = self.service(connection)
            expected = hashlib.sha256((self.url + "\0" + invitation(link, self.url)).encode()).hexdigest()[:32]
            if expected != connection:
                raise Blocked("Use this connection's original invitation.")
            result = service.join(link, config["binding"], deliberate=deliberate,
                preflight=lambda binding: self.native.check(binding, config["native_scope"]), name=name)
            return result

    def journal(self, connection, config):
        journal = Journal(self.path(connection).parent / "notices.sqlite", config["binding"])
        # Notification metadata cannot migrate to another native scope/origin.
        journal.pin_scope(self.url, hashlib.sha256(json.dumps(config["native_scope"], sort_keys=True).encode()).hexdigest())
        return journal

    @staticmethod
    def pending(client, binding):
        values = client.request("/v1/pending")
        if not isinstance(values, list) or any(e.get("protocol") != PROTOCOL or e.get("binding") != binding for e in values):
            raise Blocked("Pending messages do not match the selected Claude conversation.")
        return values

    @staticmethod
    def finish_notice(journal, notice):
        journal.control("completed-" + notice["token"], "1")
        journal.control("notice", "{}")

    def tick(self, connection):
        """Only read-only HTTP retries may call this again. Never resend a hint."""
        with self.lock:
            config, service = self.service(connection)
            _, _, client = service.member(connection, config["binding"])
            journal = self.journal(connection, config)
            try:
                with journal.exclusive():
                    pending = self.pending(client, config["binding"])
                    service.require_allowance()
                    notice = json.loads(journal.control("notice") or "{}")
                    ids = [e["delivery"]["id"] for e in pending]
                    if notice:
                        if set(notice["deliveries"]) & set(ids):
                            return "awaiting_explicit_fetch_or_ack"
                        self.finish_notice(journal, notice)
                    if not ids:
                        return "idle"
                    notice = dict(token=secrets.token_hex(16), deliveries=ids,
                                  state="write_uncertain")
                    # Persist BEFORE stdout. A crash or partial write stays
                    # uncertain; reconnect never resends it without evidence.
                    journal.control("notice", json.dumps(notice))
                    service.require_allowance()
                    try:
                        self.emit(dict(jsonrpc="2.0", method="notifications/claude/channel",
                            params=dict(content=NOTICE, meta=dict(connection=connection, notice_token=notice["token"]))))
                    except Exception:
                        raise Blocked("The notification outcome is unknown. Fetch pending messages in this same conversation; do not send another wake.") from None
                    notice["state"] = "written_not_received"
                    journal.control("notice", json.dumps(notice))
                    return "written_not_received"
            finally:
                journal.db.close()

    def receive(self, connection, notice_token=None):
        with self.lock:
            config, service = self.service(connection)
            journal = self.journal(connection, config)
            try:
                with journal.exclusive():
                    notice = json.loads(journal.control("notice") or "{}")
                    if notice_token is not None:
                        if not re.fullmatch(r"[a-f0-9]{32}", notice_token):
                            raise Blocked("Unknown Pantheon notice.")
                        if journal.control("completed-" + notice_token):
                            return dict(messages=[], status="stale_notice_noop", has_more=False)
                        if notice.get("token") != notice_token:
                            raise Blocked("This notice does not belong to the current connection.")
                    # Read CURRENT permitted input, not a cached notification body.
                    # The shared protocol handles durable claim/receipt replay.
                    result = service.receive(connection, config["binding"])
                    if notice:
                        notice["state"] = "fetched_not_acked"
                        journal.control("notice", json.dumps(notice))
                    return result
            finally:
                journal.db.close()

    def command(self, connection, op, key, **fields):
        """Persist exact command request before POST, including reply retry keys.

        Unlike payload-free notice journals, this private outbox contains the
        deliberately shared reply text. It is never logged or put in wake hints.
        """
        if op not in ("ack", "post", "ready", "withdraw", "report", "result", "confirm") or not isinstance(key, str) or not key or len(key) > 256:
            raise Blocked("Unsupported Pantheon operation or retry key.")
        with self.lock:
            config, service = self.service(connection, cleanup=op == "withdraw")
            _, _, client = service.member(connection, config["binding"], allow_expired=op == "withdraw")
            directory = private_directory(self.path(connection).parent / "outbox")
            path = directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            intent = dict(op=op, key=key, **fields)
            with private_lock(directory / "outbox.lock"):
                if op != "withdraw":
                    service.require_allowance()
                if path.exists():
                    saved = load_private(path)
                    if saved["intent"] != intent:
                        raise Blocked("This retry key already belongs to another operation. Preserve the original request.")
                    payload = saved["payload"]
                else:
                    view = client.request("/v1/chat")
                    payload = dict(intent, epoch=view["chat"]["epoch"], config=view["chat"]["config"])
                    save_private(path, dict(intent=intent, payload=payload))
                if op != "withdraw":
                    service.require_allowance()
                result = client.request("/v1/commands", payload)
            if op == "withdraw":
                self.disable(connection)
            return result

    def disable(self, connection):
        # Local disconnect also works after expiry. No automatic credential
        # deletion, server rejoin, grant renewal or host permission changes.
        with self.lock, private_lock(self.path(connection).parent / "setup.lock"):
            config = load_private(self.path(connection))
            self.native.require_stop_scope(config["binding"])
            config["allow_runtime_turns"] = False
            save_private(self.path(connection), config)
        return dict(message="Incoming Pantheon work is paused. A notice already queued or a response underway may still finish; remote membership is separate.")

    def connections(self):
        return [p.parent.name for p in self.root.glob("*/setup.json")
                if re.fullmatch(r"[a-f0-9]{32}", p.parent.name)
                and load_private(p)["binding"].get("session") == self.native.session]

    def doctor(self):
        self.native.binding()
        states = []
        for connection in self.connections():
            config = load_private(self.path(connection))
            states.append(dict(connection=connection, runtime_allowed=allowed(config)))
        return dict(message="Pantheon's tools are loaded. Claude Code delivery is an unverified local-test candidate.",
                    connections=states, exact_account_verified=False, native_delivery_verified=False,
                    production_admission=False, notification_policy={"mode": "unlimited"})

    def close(self):
        self.stopped = True
