"""Common durable-pending consumer. Adapters own wake/busy/context semantics."""
import json
import secrets
import threading
import hashlib
from . import PROTOCOL
from .client import APIError
from .state import JournalBusy
from .operational import Diagnostics

class Blocked(RuntimeError):
    pass

class Worker:
    def __init__(self, client, journal, adapter, binding):
        self.client, self.journal, self.adapter, self.binding = client, journal, adapter, binding
        self.journal.pin_scope(client.url, hashlib.sha256(client.credential.encode()).hexdigest())
        self.owner = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.diagnostics = Diagnostics()

    def once(self):
        with self.lock:
            try:
                with self.journal.exclusive():
                    result = self._once()
                    self.diagnostics.emit(result)
                    return result
            except JournalBusy as error:
                self.diagnostics.emit("blocked")
                raise Blocked(str(error)) from None
            except Blocked:
                self.diagnostics.emit("blocked")
                raise
            except APIError:
                self.diagnostics.emit("transport_failed")
                raise
            except Exception:
                self.diagnostics.emit("failed")
                raise

    def _receipt(self, identity, record):
        if not record.get("message") or not record.get("attempt") or not record.get("receipt"):
            raise Blocked("Legacy/incomplete acceptance evidence requires reconciliation")
        self.client.command("receipt", ids=[record["message"]], attempt=record["attempt"], text="harness", key="harness-" + identity)
        self.journal.put(identity, "received", reported=True)

    def _once(self):
        # Receipt outbox survives restart and closure. Re-send only the evidenced
        # metadata operation, never a model turn, even if its previous POST failed.
        for identity, record in self.journal.unreported():
            self._receipt(identity, record)
        deliveries = self.client.request("/v1/pending")
        for envelope in deliveries:
            if envelope["protocol"] != PROTOCOL or envelope["binding"] != self.binding:
                raise Blocked("Protocol or pinned runtime binding mismatch")
            delivery, message = envelope["delivery"], envelope["message"]
            identity = delivery["id"]
            prior = self.journal.state(identity)
            if delivery["state"] == "received":
                # Keep polling while the real agent runs. Never relaunch for a missing ACK.
                return "awaiting_agent_ack"
            if prior == "claiming":
                if not self.adapter.idle():
                    return "busy"
                saved = self.journal.record(identity)["claim"]
                claim = self.client.request("/v1/commands", saved)
                return self._dispatch(identity, message["id"], claim, saved["worker"])
            if delivery["state"] != "pending" or prior:
                # Read-only provider reconciliation may establish prior acceptance.
                # Missing history never proves that retrying a turn is safe.
                record = self.journal.record(identity) or {}
                reconcile = getattr(self.adapter, "reconcile", None)
                if reconcile and delivery.get("attempt"):
                    receipt = reconcile(message["id"], record.get("fingerprint"))
                    if receipt:
                        self.journal.put(identity, "received", receipt, message=message["id"], attempt=delivery["attempt"], reported=False)
                        self._receipt(identity, self.journal.record(identity))
                        return "reconciled"
                raise Blocked("Existing dispatch requires receipt/status reconciliation")
            if not self.adapter.idle():
                if message["kind"] == "terminal":
                    status = self.journal.control("stop-" + identity)
                    if status is None:
                        stop = getattr(self.adapter, "stop_owned", None)
                        try:
                            status = stop(self.journal.accepted()) if stop else "unsupported"
                        except Exception:
                            status = "unknown"
                        self.journal.control("stop-" + identity, status)
                    self.client.command("stop_status", text=status, key="stop-" + identity)
                return "busy"
            def persist_claim(payload):
                if not self.journal.begin(identity, state="claiming", claim=payload, message=message["id"]):
                    raise Blocked("Another local consumer owns this dispatch")
            claim = self.client.command("claim", target=identity, worker=self.owner, key="claim-" + identity, before_send=persist_claim)
            return self._dispatch(identity, message["id"], claim, self.owner)
        return "idle"

    def _dispatch(self, identity, message_id, claim, owner):
        # Refetch both authorization AND content after claim. A context/lifecycle
        # edit during claiming must never leave the provider using the old brief.
        current = self.client.request("/v1/pending")
        current = next((x for x in current if x["delivery"]["id"] == identity), None)
        if current is None:
            self.journal.put(identity, "suppressed")
            return "suppressed"
        if current["protocol"] != PROTOCOL or current["binding"] != self.binding or current["message"]["id"] != message_id:
            raise Blocked("Refreshed protocol/message/runtime binding mismatch")
        if current["delivery"].get("worker") != owner or current["delivery"].get("attempt") != claim["id"] or current["delivery"]["state"] != "dispatching":
            raise Blocked("Dispatch ownership changed; provider call blocked")
        fingerprint = hashlib.sha256(prompt(current, getattr(self.adapter, "owner_instruction", "")).encode()).hexdigest()
        # This transition is durable BEFORE the only provider call. A restart of
        # 'claiming' is safe; 'dispatching' remains uncertain and cannot redispatch.
        self.journal.put(identity, "dispatching", message=message_id, attempt=claim["id"], worker=owner, fingerprint=fingerprint)
        try:
            receipt = self.adapter.deliver(current)
        except Exception:
            self.journal.put(identity, "uncertain")
            self.client.command("dispatch_failed", target=identity, attempt=claim["id"], text="unknown", key="uncertain-" + identity)
            raise Blocked("Provider outcome uncertain; automatic redispatch stopped") from None
        self.journal.put(identity, "received", receipt)
        self._receipt(identity, self.journal.record(identity))
        # No ACK here. Only pantheon_ack from inside the actual agent marks read.
        return "received"

    def run(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                self.once()
                failures = 0
                delay = 5
            except APIError as error:
                if error.status in (401, 403, 410):
                    stop_owned = getattr(self.adapter, "stop_owned", None)
                    try:
                        status = stop_owned(self.journal.accepted()) if stop_owned else "unsupported"
                    except Exception:
                        status = "unknown"
                    self.journal.control("authorization-stop", status)
                    raise Blocked("Authorization revoked or chat closed") from None
                failures += 1
                if failures >= 5:
                    raise Blocked("Transport retry budget exhausted") from None
                delay = min(60, 2 ** failures)
            stop.wait(delay)



def prompt(envelope, owner_instruction=""):
    from pathlib import Path
    framework = json.loads((Path(__file__).resolve().parents[2] / "protocol" / "joining-v1.json").read_text())
    brief = envelope.get("brief", {})
    if brief.get("version") != framework["version"]:
        raise Blocked("Unsupported joining brief; update the trusted connector deliberately")
    if not isinstance(owner_instruction, str) or len(owner_instruction) > 32000:
        raise Blocked("Invalid private owner instruction")
    return json.dumps({
        "trusted_framework": framework,
        "trusted_owner_instruction": owner_instruction or None,
        "owner_selected_return_route": envelope.get("binding", {}).get("return_route"),
        "server_controls": {**{key: brief[key] for key in ("version", "state", "epoch", "expiry", "discovery", "objective_version", "context_version")}, "completion_review": brief.get("completion_review")},
        "untrusted_member_content": {
            "purpose": brief["purpose"], "objective": brief["objective"],
            "shared_context": brief["shared_context"], "definition": brief.get("definition"), "message": envelope["message"]
        }
    }, ensure_ascii=True)
