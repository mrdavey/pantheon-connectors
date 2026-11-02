"""Synthetic host/protocol evidence only; no Claude/model calls."""
import copy
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
import queue
import subprocess
import sys
import threading
from unittest.mock import patch

from pantheon_connectors.claude_channel import ClaudeChannel, NOTICE
from pantheon_connectors.claude_mcp import Server
from pantheon_connectors.claude_native import NativeContext, observe_hook
from pantheon_connectors.client import APIError, Client
from pantheon_connectors.state import load_private, save_private
from pantheon_connectors.worker import Blocked
from test_conformance import test_brief


class ProtocolFixture:
    def __init__(self):
        self.calls, self.pending, self.saved = [], [], {}
        self.binding = None
        self.config = 1
        self.lose = None
        self.during_pending = None

    def factory(self, url, credential):
        outer = self
        class Port(Client):
            def request(self, path, payload=None):
                return outer.request(path, payload)
        return Port(url, credential)

    def add(self, identity):
        self.pending.append(dict(protocol="pyros.v1", binding=self.binding, brief=test_brief(),
            message=dict(id="m" + identity, kind="message", text="PRIVATE PEER TEXT"),
            delivery=dict(id="d" + identity, state="pending")))

    def request(self, path, payload=None):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "/v1/manifest":
            return dict(protocol="pyros.v1", local_test=True)
        if path == "/v1/preview":
            return dict(purpose="Untrusted room purpose")
        if path == "/v1/redeem":
            self.binding = payload["binding"]
            if not self.pending:
                self.add("1")
            if self.lose == "redeem":
                self.lose = None
                raise APIError(0, "network_timeout")
            return dict(id="member")
        if path == "/v1/pending":
            if self.during_pending:
                self.during_pending()
            return copy.deepcopy(self.pending)
        if path == "/v1/chat":
            return dict(chat=dict(epoch=1, config=self.config))
        if path == "/v1/commands":
            key = payload["key"]
            if key in self.saved and self.saved[key] != payload:
                raise APIError(409)
            self.saved[key] = copy.deepcopy(payload)
            op = payload["op"]
            if op == "claim":
                for e in self.pending:
                    if e["delivery"]["id"] == payload["target"]:
                        e["delivery"].update(state="dispatching", attempt=key, worker=payload["worker"])
            if op == "receipt":
                for e in self.pending:
                    if e["message"]["id"] in payload["ids"]:
                        e["delivery"]["state"] = "received"
            if op == "ack":
                self.pending = [e for e in self.pending if e["message"]["id"] not in payload["ids"]]
            if self.lose == op:
                self.lose = None
                raise APIError(0, "network_timeout")
            return dict(id=key)
        raise AssertionError(path)


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = dict(CLAUDE_CODE_SESSION_ID="session-one", CLAUDE_PLUGIN_DATA=str(self.root / "plugin-data"),
                        CLAUDE_CONFIG_DIR=str(self.root / "profile"))
        self.event = dict(hook_event_name="SessionStart", session_id="session-one", model="synthetic-model",
                          cwd=str(self.root), source="startup", transcript_path="DO NOT STORE", prompt="DO NOT STORE")
        observe_hook(self.event, self.env)
        self.native = NativeContext("synthetic-1", self.env)
        self.port = ProtocolFixture()
        self.events = []
        self.url = "http://127.0.0.1:19837"
        self.link = self.url + "/join#invite=" + "a" * 43
        self.channel = self.make_channel()

    def make_channel(self):
        return ClaudeChannel(self.native, url=self.url, client_factory=self.port.factory, emit=self.events.append)

    def joined(self, *, name=None, connection_policy=False):
        connection = self.channel.prepare(self.link, True)["connection"]
        if connection_policy:
            self.channel.authorize(connection, True, runtime_policy={"mode": "connection"})
        else:
            self.channel.authorize(connection, True, expires_at=time.time() + 300)
        self.channel.join(connection, self.link, True, name=name)
        return connection

    def test_prepare_is_disabled_and_reuses_without_preview_or_renewal(self):
        c = self.channel.prepare(self.link, True)["connection"]
        saved = load_private(self.channel.path(c))
        self.assertFalse(saved["allow_runtime_turns"])
        self.assertEqual(saved["notification_policy"], {"mode": "unlimited"})
        self.assertFalse(Path(saved["state_dir"]).exists())
        before = list(self.port.calls)
        self.channel.prepare(self.link, True)
        self.assertEqual(self.port.calls, before)
        with self.assertRaises(Blocked):
            self.channel.join(c, self.link, True)
        self.assertFalse(any(path == "/v1/redeem" for path, _ in self.port.calls))

    def test_production_and_wrong_origin_are_blocked_before_network(self):
        for url in ("https://pantheon.talk", "http://localhost:8080", "http://127.0.0.1"):
            c = ClaudeChannel(self.native, url=url, client_factory=self.port.factory)
            with self.assertRaises(Blocked):
                c.prepare(url + "/join#invite=" + "a" * 43, True)
        self.assertEqual(self.port.calls, [])
        with self.assertRaises(Blocked):
            self.channel.prepare(self.link.replace("19837", "19838"), True)

    def test_native_lifecycle_identity_and_missing_session_fail_closed(self):
        text = (self.native.root / "session-one.json").read_text()
        self.assertNotIn("DO NOT STORE", text)
        self.native.binding()
        observe_hook(dict(self.event, hook_event_name="SessionEnd"), self.env)
        with self.assertRaises(Blocked):
            self.native.binding()
        observe_hook(dict(self.event, session_id="session-two"), dict(self.env, CLAUDE_CODE_SESSION_ID="session-two"))
        with self.assertRaises(Blocked):
            self.native.binding()  # MCP env stays old on /clear; never follows latest.
        with self.assertRaises(Blocked):
            NativeContext("fixture", dict(self.env, CLAUDE_CODE_SESSION_ID=""))
        with self.assertRaises(Blocked):
            observe_hook(dict(self.event, session_id="forged"), self.env)

    def test_compaction_preserves_generation_and_model_change_blocks(self):
        original = self.native.binding()
        observe_hook(dict(self.event, source="compact"), self.env)
        self.assertEqual(self.native.binding(), original)
        observe_hook(dict(self.event, hook_event_name="PostModelSwitch"), self.env)
        observe_hook(dict(self.event, source="compact"), self.env)
        with self.assertRaises(Blocked):
            self.native.binding()

    def test_incomplete_new_lifecycle_observation_fences_old_active_record(self):
        self.native.binding()
        with self.assertRaises(Blocked):
            observe_hook(dict(self.event, model=""), self.env)
        with self.assertRaises(Blocked):
            self.native.binding()

    def test_supplied_name_and_lost_redemption_reuse_exact_request(self):
        c = self.channel.prepare(self.link, True)["connection"]
        self.channel.authorize(c, True, expires_at=time.time() + 300)
        self.port.lose = "redeem"
        with self.assertRaises(APIError):
            self.channel.join(c, self.link, True, name="Claude Pathfinder")
        self.channel.join(c, self.link, True)
        posts = [p for path, p in self.port.calls if path == "/v1/redeem"]
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[0]["name"], "Claude Pathfinder")
        with self.assertRaises(Blocked):
            self.channel.join(c, self.link.replace("a" * 43, "b" * 43), True)

    def test_notification_is_content_free_once_and_not_receipt(self):
        c = self.joined()
        self.assertEqual(self.channel.tick(c), "written_not_received")
        self.assertEqual(self.channel.tick(c), "awaiting_explicit_fetch_or_ack")
        self.assertEqual(self.make_channel().tick(c), "awaiting_explicit_fetch_or_ack")
        self.assertEqual(len(self.events), 1)
        wire = json.dumps(self.events)
        self.assertNotIn("PRIVATE", wire)
        self.assertNotIn("synthetic-model", wire)
        self.assertNotIn("a" * 43, wire)
        self.assertEqual(self.events[0]["params"]["content"], NOTICE)
        self.assertFalse(any(path == "/v1/commands" for path, _ in self.port.calls))
        # Same result while an agent is busy: coalesce until actual fetch/ACK.
        self.port.add("2")
        self.channel.tick(c)
        self.assertEqual(len(self.events), 1)

    def test_lost_fetch_retries_until_explicit_ack_then_stale_token_noop(self):
        c = self.joined()
        self.channel.tick(c)
        token = self.events[0]["params"]["meta"]["notice_token"]
        first = self.channel.receive(c, token)
        second = self.make_channel().receive(c, token)
        self.assertEqual(first["messages"], second["messages"])
        self.assertFalse(any(p and p.get("op") == "ack" for _, p in self.port.calls))
        self.channel.command(c, "ack", "ack-1", ids=["m1"])
        self.assertEqual(self.channel.tick(c), "idle")
        self.assertEqual(self.channel.receive(c, token)["status"], "stale_notice_noop")
        self.port.add("2")
        self.channel.tick(c)
        self.assertEqual(len(self.events), 2)
        self.assertNotEqual(self.events[-1]["params"]["meta"]["notice_token"], token)

    def test_partial_batches_and_later_arrivals_fetch_current_content(self):
        c = self.joined()
        for i in range(2, 26):
            self.port.add(str(i))
        self.channel.tick(c)
        token = self.events[0]["params"]["meta"]["notice_token"]
        first = self.channel.receive(c, token)
        self.assertEqual(len(first["messages"]), 20)
        self.assertTrue(first["has_more"])
        self.channel.command(c, "ack", "batch-one", ids=["m" + str(i) for i in range(1, 21)])
        self.port.add("26")
        second = self.channel.receive(c, token)
        self.assertEqual(len(second["messages"]), 6)
        self.assertFalse(second["has_more"])

    def test_uncertain_write_is_never_resent_even_after_restart(self):
        c = self.joined()
        def fail(_):
            raise BrokenPipeError("SECRET")
        self.channel.emit = fail
        with self.assertRaises(Blocked):
            self.channel.tick(c)
        self.assertEqual(self.make_channel().tick(c), "awaiting_explicit_fetch_or_ack")
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.channel.receive(c)["messages"]), 1)

    def test_command_response_loss_replays_exact_payload_after_config_change(self):
        c = self.joined()
        self.port.lose = "post"
        with self.assertRaises(APIError):
            self.channel.command(c, "post", "reply-1", text="Authorized reply")
        self.port.config = 2
        self.make_channel().command(c, "post", "reply-1", text="Authorized reply")
        writes = [p for path, p in self.port.calls if path == "/v1/commands" and p["op"] == "post"]
        self.assertEqual(writes[0], writes[1])
        with self.assertRaises(Blocked):
            self.channel.command(c, "post", "reply-1", text="Changed reply")

    def test_stop_during_pending_read_blocks_hint_and_fetch(self):
        c = self.joined()
        def disable():
            saved = load_private(self.channel.path(c))
            saved["allow_runtime_turns"] = False
            save_private(self.channel.path(c), saved)
        self.port.during_pending = disable
        with self.assertRaises(Blocked):
            self.channel.tick(c)
        self.assertEqual(self.events, [])
        # Original grant retry must not re-enable a stopped connection.
        saved = load_private(self.channel.path(c))
        self.assertFalse(self.channel.authorize(c, True, **saved["approved_runtime"])["runtime_allowed"])
        with self.assertRaises(Blocked):
            self.channel.authorize(c, True, runtime_policy={"mode": "connection"})

    def test_expiry_and_local_disconnect_preserve_grant_and_allow_withdrawal(self):
        c = self.joined()
        config = load_private(self.channel.path(c))
        with patch("pantheon_connectors.runtime_policy.time.time", return_value=config["trial_expires_at"] + 1):
            with self.assertRaises(Blocked):
                self.channel.receive(c)
            self.channel.command(c, "withdraw", "withdraw")
        self.assertFalse(load_private(self.channel.path(c))["allow_runtime_turns"])
        self.assertFalse(self.channel.authorize(c, True, **config["approved_runtime"])["runtime_allowed"])

    def test_connection_lifetime_has_no_quota_and_no_implicit_activation(self):
        c = self.joined(connection_policy=True)
        config = load_private(self.channel.path(c))
        self.assertEqual(config["trial_expires_at"], 0)
        self.assertEqual(config["runtime_policy"], {"mode": "connection"})
        self.channel.disable(c)
        self.assertFalse(self.channel.authorize(c, True, runtime_policy={"mode": "connection"})["runtime_allowed"])

    def test_unlimited_notice_cycles_and_disable_before_fetch_receipt(self):
        c = self.joined(connection_policy=True)
        for i in range(1, 13):
            if i > 1:
                self.port.add(str(i))
            self.channel.tick(c)
            token = self.events[-1]["params"]["meta"]["notice_token"]
            self.channel.receive(c, token)
            self.channel.command(c, "ack", "cycle-" + str(i), ids=["m" + str(i)])
        self.assertEqual(len(self.events), 12)
        self.port.add("13")
        before = len(self.port.saved)
        self.port.during_pending = lambda: self.channel.disable(c)
        with self.assertRaises(Blocked):
            self.channel.receive(c)
        self.assertEqual(len(self.port.saved), before)

    def test_pump_does_not_pause_disabled_preparation_before_authorization(self):
        c = self.channel.prepare(self.link, True)["connection"]
        server = Server(io.StringIO(), factory=lambda *_: self.channel)
        server.channel, server.initialized = self.channel, True
        server.pump_once(1)
        self.assertEqual(server.paused, {})
        self.assertEqual(self.events, [])
        self.channel.authorize(c, True, expires_at=time.time() + 300)
        self.channel.join(c, self.link, True)
        server.pump_once(2)
        self.assertEqual(len(self.events), 1)

    def restart_server(self, env=None, version="synthetic-1"):
        output = io.StringIO()
        def factory(client_version, emit):
            return ClaudeChannel(NativeContext(client_version, env or self.env), url=self.url,
                                 client_factory=self.port.factory, emit=emit)
        server = Server(output, factory=factory)
        server.handle(dict(jsonrpc="2.0", id=1, method="initialize", params=dict(
            protocolVersion="2025-11-25", clientInfo=dict(version=version))))
        return server, output

    def test_restart_handshake_resumes_saved_monitor_without_any_tool_call(self):
        c = self.joined(connection_policy=True)
        original = self.channel.path(c).read_bytes()
        server, output = self.restart_server()
        with patch.object(server, "call", side_effect=AssertionError("No foreground tool may run")):
            server.pump_once(0)
            self.assertIsNone(server.channel)  # Wait for complete MCP handshake.
            server.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
            server.handle(dict(jsonrpc="2.0", id=2, method="tools/list"))
            self.assertIsNone(server.channel)  # Discovery is not a tool call.
            server.pump_once(1)
            server.pump_once(6)
        notices = [json.loads(line) for line in output.getvalue().splitlines()
                   if json.loads(line).get("method") == "notifications/claude/channel"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["params"]["meta"]["connection"], c)
        self.assertEqual(notices[0]["params"]["content"], NOTICE)
        self.assertEqual(self.channel.path(c).read_bytes(), original)
        self.assertFalse(any(path == "/v1/commands" for path, _ in self.port.calls))

    def test_restart_monitor_preserves_disabled_expired_and_wrong_session_fences(self):
        c = self.joined()
        saved = load_private(self.channel.path(c))
        for case in ("disabled", "expired", "wrong-session", "wrong-version", "revoked"):
            with self.subTest(case=case):
                config = copy.deepcopy(saved)
                env, version = self.env, "synthetic-1"
                if case == "disabled":
                    config["allow_runtime_turns"] = False
                if case == "expired":
                    config["trial_expires_at"] = time.time() - 1
                if case == "wrong-session":
                    env = dict(self.env, CLAUDE_CODE_SESSION_ID="other-session")
                if case == "wrong-version":
                    version = "other-version"
                save_private(self.channel.path(c), config)
                original = self.channel.path(c).read_bytes()
                server, output = self.restart_server(env, version)
                server.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
                with patch.object(self.port, "request", side_effect=APIError(401)) as request:
                    server.pump_once(0)
                    server.pump_once(5)
                    self.assertEqual(request.call_count, 1 if case == "revoked" else 0)
                self.assertNotIn('"method": "notifications/claude/channel"', output.getvalue())
                self.assertEqual(self.channel.path(c).read_bytes(), original)

    def test_restart_monitor_never_replays_an_uncertain_hint(self):
        c = self.joined()
        self.channel.emit = lambda _: (_ for _ in ()).throw(BrokenPipeError())
        with self.assertRaises(Blocked):
            self.channel.tick(c)
        server, output = self.restart_server()
        server.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
        server.pump_once(0)
        self.assertNotIn('"method": "notifications/claude/channel"', output.getvalue())

    def test_restart_waits_for_missing_lifecycle_observation_without_tool_call(self):
        self.joined()
        lifecycle = self.native.root / "session-one.json"
        lifecycle.unlink()
        server, output = self.restart_server()
        server.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
        with patch.object(self.port, "request", side_effect=AssertionError("No native evidence yet")):
            server.pump_once(0)
        self.assertEqual(server.paused, {})
        observe_hook(dict(self.event, source="resume"), self.env)
        server.pump_once(1)
        self.assertEqual(output.getvalue().count('"method": "notifications/claude/channel"'), 1)

    def test_removal_only_disconnect_survives_lifecycle_fences_and_restart(self):
        for index, kind in enumerate(("PostModelSwitch", "ConfigChange", "SessionEnd", "CwdChanged")):
            with self.subTest(kind=kind):
                observe_hook(self.event, self.env)
                self.native = NativeContext("synthetic-1", self.env)
                self.channel = self.make_channel()
                self.link = self.url + "/join#invite=" + chr(98 + index) * 43
                c = self.joined(connection_policy=True)
                original = load_private(self.channel.path(c))
                observe_hook(dict(self.event, hook_event_name=kind), self.env)
                with self.assertRaises(Blocked):
                    self.channel.config(c)
                server = Server(io.StringIO(), factory=lambda *_: self.channel)
                with patch.object(self.port, "request", side_effect=AssertionError("Removal must be local")):
                    server.call("pantheon_disconnect", dict(connection=c))
                stopped = load_private(self.channel.path(c))
                self.assertEqual(stopped, dict(original, allow_runtime_turns=False))
                # Later matching native observations must not erase the stop.
                observe_hook(dict(self.event, source="resume"), self.env)
                restarted, output = self.restart_server()
                restarted.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
                restarted.pump_once(0)
                self.assertNotIn('"method": "notifications/claude/channel"', output.getvalue())
                with self.assertRaises(Blocked):
                    restarted.channel.receive(c)
                self.assertFalse(restarted.channel.authorize(c, True, runtime_policy={"mode": "connection"})["runtime_allowed"])
                self.assertEqual(load_private(self.channel.path(c)), stopped)

    def test_removal_only_identity_does_not_grant_another_session_or_profile_access(self):
        c = self.joined()
        original = self.channel.path(c).read_bytes()
        for key in ("session", "profile"):
            with self.subTest(key=key), patch.object(self.native, key, "another-identity"):
                with self.assertRaises(Blocked):
                    self.channel.disable(c)
            self.assertEqual(self.channel.path(c).read_bytes(), original)
        # Native version changes cannot prevent removal; no active read follows.
        self.native.version = "new-native-version"
        self.channel.disable(c)
        with self.assertRaises(Blocked):
            self.channel.config(c)

    def test_cwd_changed_fences_incoming_reads_and_writes_without_changing_scope(self):
        c = self.joined()
        original = load_private(self.channel.path(c))
        observe_hook(dict(self.event, hook_event_name="CwdChanged", old_cwd=str(self.root),
                          new_cwd=str(self.root / "elsewhere"), cwd=str(self.root / "elsewhere")), self.env)
        with patch.object(self.port, "request", side_effect=AssertionError("Changed workspace must not access chat")):
            for operation in (lambda: self.channel.tick(c), lambda: self.channel.receive(c),
                              lambda: self.channel.command(c, "post", "after-cd", text="No")):
                with self.assertRaises(Blocked):
                    operation()
        self.assertEqual(self.events, [])
        self.assertEqual(load_private(self.channel.path(c)), original)
        # A restarted process in the changed directory still cannot reuse scope.
        observe_hook(dict(self.event, source="resume", cwd=str(self.root / "elsewhere")), self.env)
        server, output = self.restart_server()
        server.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
        server.pump_once(0)
        self.assertNotIn('"method": "notifications/claude/channel"', output.getvalue())
        self.assertIn(c, server.paused)

    def test_duplicate_process_lock_and_member_revocation_do_not_send(self):
        c = self.joined()
        journal = self.channel.journal(c, load_private(self.channel.path(c)))
        try:
            with journal.exclusive():
                with self.assertRaises(Exception):
                    self.make_channel().tick(c)
        finally:
            journal.db.close()
        self.assertEqual(self.events, [])
        with patch.object(self.port, "request", side_effect=APIError(410)):
            with self.assertRaises(APIError):
                self.channel.tick(c)
        self.assertEqual(self.events, [])

    def test_wrong_binding_and_unknown_token_never_receive(self):
        c = self.joined()
        self.port.pending[0]["binding"] = dict(self.port.binding, session="other")
        with self.assertRaises(Blocked):
            self.channel.tick(c)
        with self.assertRaises(Blocked):
            self.channel.receive(c, "b" * 32)
        with self.assertRaises(Blocked):
            self.channel.receive(c)
        self.assertFalse(any(path == "/v1/commands" for path, _ in self.port.calls))

    def test_profile_session_and_model_scope_changes_cannot_reuse_membership(self):
        self.joined()
        observe_hook(dict(self.event, model="other-model", source="resume"), self.env)
        self.native = NativeContext("synthetic-1", self.env)
        with self.assertRaises(Blocked):
            self.make_channel().prepare(self.link, True)
        second_env = dict(self.env, CLAUDE_CODE_SESSION_ID="session-two")
        observe_hook(dict(self.event, session_id="session-two"), second_env)
        self.native = NativeContext("synthetic-1", second_env)
        channel = self.make_channel()
        self.assertEqual(channel.connections(), [])
        with self.assertRaises(Blocked):
            channel.prepare(self.link, True)

    def test_mcp_negotiation_tool_validation_and_redacted_errors(self):
        output = io.StringIO()
        server = Server(output, factory=lambda *_: self.channel)
        server.handle(dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2026-07-28", clientInfo=dict(version="fixture"))))
        server.handle(dict(jsonrpc="2.0", method="notifications/initialized"))
        server.handle(dict(jsonrpc="2.0", id=2, method="tools/list"))
        server.handle(dict(jsonrpc="2.0", id=3, method="tools/call", params=dict(name="pantheon_prepare", arguments=dict(link=self.link, deliberate="true"))))
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(replies[0]["result"]["capabilities"]["experimental"], {"claude/channel": {}})
        self.assertTrue(replies[-1]["result"]["isError"])
        self.assertNotIn("a" * 43, output.getvalue())
        self.assertEqual(self.port.calls, [])

    def test_read_retry_recovers_and_revocation_stops_without_notice_replay(self):
        c = self.joined()
        server = Server(io.StringIO(), factory=lambda *_: self.channel)
        server.channel, server.initialized = self.channel, True
        original = self.channel.tick
        with patch.object(self.channel, "tick", side_effect=[APIError(503), "idle"]) as tick:
            server.pump_once(10)
            server.pump_once(11)
            self.assertEqual(tick.call_count, 1)
            server.pump_once(12)
            self.assertEqual(tick.call_count, 2)
        self.channel.tick = original
        with patch.object(self.channel, "tick", side_effect=APIError(401)) as tick:
            server.pump_once(17)
            server.pump_once(100)
            self.assertEqual(tick.call_count, 1)
        self.assertIn(c, server.paused)
        self.assertEqual(self.events, [])

    def test_read_retry_exhaustion_requires_explicit_resume_without_renewal(self):
        c = self.joined()
        original = load_private(self.channel.path(c))
        server = Server(io.StringIO(), factory=lambda *_: self.channel)
        server.channel, server.initialized = self.channel, True
        with patch.object(self.channel, "tick", side_effect=APIError(503)) as tick:
            for now in (0, 2, 6, 14, 30, 100):
                server.pump_once(now)
            self.assertEqual(tick.call_count, 5)
        self.assertIn(c, server.paused)
        server.call("pantheon_resume", dict(connection=c))
        self.assertNotIn(c, server.paused)
        self.assertEqual(load_private(self.channel.path(c)), original)

    def test_symlink_private_state_and_foreign_connection_rejected(self):
        c = self.joined()
        path = self.channel.path(c)
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.channel.config(c)
        with self.assertRaises(Blocked):
            self.channel.config("../secret")


@unittest.skipUnless(os.environ.get("PANTHEON_HTTP_TEST_URL"), "requires disposable loopback Go service")
class ClaudeHTTPTests(unittest.TestCase):
    def test_real_http_probe_fetch_ack_reply_recovery_and_withdrawal(self):
        url = os.environ["PANTHEON_HTTP_TEST_URL"]
        from pantheon_connectors.claude_channel import local_origin
        local_origin(url)
        admin = Client(url, secrets.token_urlsafe(32))
        self.assertTrue(Client(url, "").request("/v1/manifest").get("local_test"))
        admin.request("/v1/chats", dict(secret=admin.credential, key=secrets.token_urlsafe(24), title="Claude protocol fixture",
            purpose="Synthetic native host; no real agent", hours=4,
            slots=[dict(id="claude", runtime="claude-code", required=True)]))
        try:
            token = secrets.token_urlsafe(32)
            admin.command("invite", target="claude", secret=token, hours=1)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                env = dict(CLAUDE_CODE_SESSION_ID="http-fixture", CLAUDE_CONFIG_DIR=str(root / "profile"), CLAUDE_PLUGIN_DATA=str(root / "data"))
                observe_hook(dict(hook_event_name="SessionStart", session_id="http-fixture", model="synthetic", cwd=str(root)), env)
                events = []
                channel = ClaudeChannel(NativeContext("synthetic", env), url=url, emit=events.append)
                link = url + "/join#invite=" + token
                c = channel.prepare(link, True)["connection"]
                channel.authorize(c, True, expires_at=time.time() + 300)
                channel.join(c, link, True)
                # Restart the actual Python stdio entrypoint, not Claude. Send
                # only MCP handshake/discovery; no tools/call can start it.
                process = subprocess.Popen([sys.executable, "-m", "pantheon_connectors.claude_mcp"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={**os.environ, **env, "PANTHEON_CLAUDE_TEST_ORIGIN": url})
                replies = queue.Queue()
                def read_output():
                    for line in process.stdout:
                        replies.put(json.loads(line))
                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                try:
                    for request in (
                        dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2025-11-25", clientInfo=dict(version="synthetic"))),
                        dict(jsonrpc="2.0", method="notifications/initialized"),
                        dict(jsonrpc="2.0", id=2, method="tools/list")):
                        process.stdin.write(json.dumps(request) + "\n")
                    process.stdin.flush()
                    output = [replies.get(timeout=10) for _ in range(3)]
                    notices = [r for r in output if r.get("method") == "notifications/claude/channel"]
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0]["params"]["content"], NOTICE)
                    notice = notices[0]["params"]["meta"]["notice_token"]
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    reader.join(timeout=2)
                    process.stdout.close()
                    stderr = process.stderr.read()
                    process.stderr.close()
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stderr, "")
                first = channel.receive(c, notice)
                self.assertEqual(first["messages"], channel.receive(c, notice)["messages"])
                messages = [m["untrusted_member_content"]["message"] for m in first["messages"]]
                probe = next(m for m in messages if m["kind"] == "probe")
                channel.command(c, "ack", "ack", ids=[m["id"] for m in messages])
                channel.command(c, "ready", "ready", text=probe["text"])
                channel.command(c, "post", "post", text="Synthetic protocol reply")
                channel.command(c, "post", "post", text="Synthetic protocol reply")
                self.assertEqual(admin.request("/v1/chat")["chat"]["state"], "active")
                channel.command(c, "withdraw", "withdraw")
                with self.assertRaises(Blocked):
                    channel.tick(c)
        finally:
            admin.command("end", key="end")
