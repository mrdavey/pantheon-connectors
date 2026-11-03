"""Hermes native gateway port. Host integration must enforce the pinned session atomically.

A generic webhook, session mirror, or fresh AIAgent is intentionally not used.
The host port is a real integration prerequisite, not a supported-runtime claim.
"""
from .worker import Blocked, prompt

class HermesAdapter:
    def __init__(self, gateway, binding, owner_instruction=""):
        if binding["runtime"] != "hermes" or binding["surface"] != "native-adapter":
            raise Blocked("Hermes native binding required")
        self.gateway, self.binding = gateway, binding
        self.owner_instruction = owner_instruction
        if not getattr(gateway, "supports_atomic_pinned_dispatch", False):
            raise Blocked("Hermes host lacks a verified atomic selected-session dispatch hook")
        if gateway.binding() != binding:
            raise Blocked("Hermes profile/session/version changed")

    def idle(self):
        if self.gateway.binding() != self.binding:
            raise Blocked("Hermes session changed; deliberate reconnect required")
        return not self.gateway.busy()

    def deliver(self, envelope):
        if envelope["binding"] != self.binding or not self.idle():
            raise Blocked("Pinned Hermes session unavailable or busy")
        # The native port must atomically reject session rollover and authorization change.
        receipt = self.gateway.dispatch_selected(self.binding, envelope["message"]["id"], prompt(envelope, self.owner_instruction))
        if receipt.get("session") != self.binding["session"] or not receipt.get("accepted"):
            raise Blocked("Hermes did not attest intended-session acceptance")
        return receipt["message_id"]

class HermesSessionsAdapter:
    """Explicitly selected Hermes Sessions API surface, never a webhook fallback.

    Uses the owner's existing profile/session and tools. HTTP alone is insufficient:
    completion must attest the exact session. Responses are not auto-posted to Pantheon.
    """
    def __init__(self, client, binding, owner_instruction=""):
        from urllib.parse import quote
        if binding["runtime"] != "hermes" or binding["surface"] != "sessions-api":
            raise Blocked("Explicit Hermes Sessions API binding required")
        self.client, self.binding = client, binding
        self.owner_instruction = owner_instruction
        self.prefix = "/p/" + quote(binding["profile"], safe="")
        self.session_path = self.prefix + "/api/sessions/" + quote(binding["session"], safe="")
        capabilities = client.request(self.prefix + "/v1/capabilities")
        if capabilities.get("object") != "hermes.api_server.capabilities" or not capabilities.get("features", {}).get("session_chat"):
            raise Blocked("Hermes selected-session capability is unavailable")
        self._check()

    def _check(self):
        health = self.client.request(self.prefix + "/health/detailed")
        if health.get("version") != self.binding["version"] or health.get("platform") != "hermes-agent":
            raise Blocked("Hermes runtime version changed")
        session = self.client.request(self.session_path).get("session", {})
        if session.get("id") != self.binding["session"] or session.get("ended_at") or session.get("is_internal_child"):
            raise Blocked("Pinned Hermes session cannot be resumed")
        if "gateway_busy" not in health:
            raise Blocked("Hermes busy-state evidence unavailable")
        return health

    def idle(self):
        return self._check()["gateway_busy"] is False

    def reconcile(self, message_id, fingerprint=None):
        import hashlib
        if not fingerprint:
            return None
        self._check()
        result = self.client.request(self.session_path + "/messages?order=latest&limit=500")
        if result.get("object") != "list" or result.get("session_id") != self.binding["session"]:
            raise Blocked("Hermes history resolved a different session")
        matches = [str(item["id"]) for item in result.get("data", [])
                   if item.get("session_id") == self.binding["session"] and item.get("role") == "user"
                   and isinstance(item.get("content"), str)
                   and hashlib.sha256(item["content"].encode()).hexdigest() == fingerprint]
        if len(matches) > 1:
            raise Blocked("Multiple Hermes inputs match; manual reconciliation required")
        # A missing/compacted/older record is unknown, never permission to retry.
        return matches[0] if matches else None

    def deliver(self, envelope):
        if envelope["binding"] != self.binding or not self.idle():
            raise Blocked("Pinned Hermes session unavailable or busy")
        result = self.client.request(self.session_path + "/chat", {"input": prompt(envelope, self.owner_instruction)})
        if result.get("object") != "hermes.session.chat.completion" or result.get("session_id") != self.binding["session"]:
            raise Blocked("Hermes completion did not attest the pinned session")
        return envelope["message"]["id"]
