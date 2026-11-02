"""Codex App Server stdio adapter; never creates or silently replaces a thread."""
import json
import queue
import subprocess
import threading
import os
import pathlib
import re
import stat
from .worker import Blocked, prompt

class AppServer:
    def __init__(self, profile, executable="codex", *, command=None):
        self.process = subprocess.Popen(command or [executable, "--profile", profile, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.responses, self.pending, self.lock = {}, queue.Queue(), threading.Lock()
        self.sequence, self.busy, self.thread = 0, False, None
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.identity = self.call("initialize", {"clientInfo": {"name": "pantheon", "version": "0.2.0"}, "capabilities": {"experimentalApi": False}})
            self._write({"method": "initialized"})
        except Exception:
            self.close()
            raise Blocked("App Server initialization failed; no turn requested") from None

    def _write(self, value):
        with self.lock:
            self.process.stdin.write(json.dumps(value) + "\n")
            self.process.stdin.flush()

    def _read(self):
        try:
            self._read_events()
        except Exception:
            pass  # Never emit runtime payloads in a reader-thread traceback.
        finally:
            for waiter, _ in list(self.responses.values()):
                waiter.put({"error": {"message": "Native connection closed"}})

    def _read_events(self):
        for line in getattr(self, "messages", None) or self.process.stdout:
            event = json.loads(line)
            if "method" not in event:
                pending = self.responses.get(event.get("id"))
                if pending:
                    waiter, method = pending
                    if method == "thread/resume" and "result" in event:
                        self.thread = event["result"]["thread"]["id"]
                    if method == "turn/start" and "result" in event:
                        self.busy = event["result"]["turn"].get("status") not in ("completed", "failed", "interrupted")
                    waiter.put(event)
                continue
            if event.get("params", {}).get("threadId") not in (None, self.thread):
                continue
            if event["method"] == "turn/completed":
                self.busy = False
            elif event["method"] == "turn/started":
                self.busy = True
            elif "id" in event:
                # Native approval stays pending for a supported human approval channel.
                # Never auto-approve file/command/tool/user-input requests.
                self.pending.put(event)

    def call(self, method, params):
        self.sequence += 1
        identity = self.sequence
        waiter = queue.Queue()
        self.responses[identity] = (waiter, method)
        try:
            self._write({"id": identity, "method": method, "params": params})
            result = waiter.get(timeout=30)
            if "error" in result:
                raise Blocked("Codex rejected operation; inspect native runtime privately")
            return result["result"]
        finally:
            self.responses.pop(identity, None)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class AttachedAppServer(AppServer):
    """Attach only to an owner-selected, private existing control socket.

    Closing this client leaves the owning App Server alive. Never starts a daemon,
    loads a thread from disk, changes a profile, or accesses Desktop private IPC.
    """
    shared_owner = True

    def __init__(self, socket_path, version, profile, executable="codex"):
        path = pathlib.Path(socket_path).expanduser()
        try:
            info = path.lstat()
        except OSError:
            raise Blocked("Supported App Server control socket is unavailable") from None
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Blocked("App Server requires an absolute owner-only Unix socket")
        try:
            from websockets.sync.client import unix_connect
        except ImportError:
            raise Blocked("Install connectors/requirements-codex.txt in the selected local Python environment") from None
        self.websocket = unix_connect(str(path), proxy=None, compression=None, open_timeout=10, close_timeout=3, max_size=8 * 1024 * 1024)
        self.messages = self.websocket
        self.responses, self.pending, self.lock = {}, queue.Queue(), threading.Lock()
        self.sequence, self.busy, self.thread = 0, False, None
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.identity = self.call("initialize", {"clientInfo": {"name": "pantheon", "version": "0.2.0"}, "capabilities": {"experimentalApi": False}})
            self._write({"method": "initialized"})
            if not re.search(r"/" + re.escape(version) + r"(?:\s|$)", self.identity.get("userAgent", "")):
                raise Blocked("Connected App Server version differs from the pinned version")
            self.require_profile(profile)
        except Exception:
            self.close()
            raise

    def _write(self, value):
        with self.lock:
            self.websocket.send(json.dumps(value))

    def close(self):
        self.websocket.close()

    def require_profile(self, profile):
        result = self.call("config/read", {"includeLayers": True})
        layers = result.get("layers")
        if not isinstance(layers, list):
            raise Blocked("App Server profile provenance is unavailable")
        names = {layer.get("name", {}).get("profile") for layer in layers
                 if layer.get("name", {}).get("type") == "user" and not layer.get("disabledReason")}
        names.discard(None)
        if names != (set() if profile == "default" else {profile}):
            raise Blocked("Connected App Server uses a different profile")

    def require_loaded(self, thread_id):
        cursor = None
        for _ in range(100):
            params = {"cursor": cursor} if cursor else {}
            result = self.call("thread/loaded/list", params)
            if thread_id in result.get("data", []):
                return
            cursor = result.get("nextCursor")
            if not cursor:
                break
        raise Blocked("Selected thread is not loaded in this owner; open it there first")

class CodexAdapter:
    def __init__(self, rpc, binding, owner_instruction=""):
        if binding["runtime"] != "codex" or binding["surface"] != "app-server":
            raise Blocked("Codex binding required")
        self.rpc, self.binding = rpc, binding
        self.owner_instruction = owner_instruction
        if getattr(rpc, "shared_owner", False):
            rpc.require_profile(binding["profile"])
            rpc.require_loaded(binding["session"])
            # Read only: resume could load disk if the thread unloaded between calls.
            result = rpc.call("thread/read", {"threadId": binding["session"], "includeTurns": False})
            rpc.thread = binding["session"]
        else:
            # Legacy test port; production CLI exclusively uses AttachedAppServer.
            result = rpc.call("thread/resume", {"threadId": binding["session"]})
        if result["thread"]["id"] != binding["session"]:
            raise Blocked("Codex resumed a different thread")
        # Unknown live state is not safe to dispatch into.
        status = result["thread"].get("status", {}).get("type")
        if status not in ("idle", "active"):
            raise Blocked("Codex live thread state unavailable")
        rpc.busy = status == "active"

    def idle(self):
        if getattr(self.rpc, "shared_owner", False):
            self.rpc.require_profile(self.binding["profile"])
            self.rpc.require_loaded(self.binding["session"])
            thread = self.rpc.call("thread/read", {"threadId": self.binding["session"], "includeTurns": False}).get("thread", {})
            if thread.get("id") != self.binding["session"] or thread.get("status", {}).get("type") not in ("idle", "active"):
                raise Blocked("Selected shared thread state is unavailable")
            self.rpc.busy = thread["status"]["type"] != "idle"
        return not self.rpc.busy and self.rpc.pending.empty()

    def reconcile(self, message_id, fingerprint=None):
        thread = self.rpc.call("thread/read", {"threadId": self.binding["session"], "includeTurns": True}).get("thread", {})
        if thread.get("id") != self.binding["session"]:
            raise Blocked("Reconciliation returned a different Codex thread")
        matches = [turn["id"] for turn in thread.get("turns", [])
                   if any(item.get("type") == "userMessage" and item.get("clientId") == message_id for item in turn.get("items", []))]
        if len(matches) > 1:
            raise Blocked("Multiple native turns match; manual reconciliation required")
        # Do not persist or forward native transcript content.
        return matches[0] if matches else None

    def stop_owned(self, receipts):
        thread = self.rpc.call("thread/read", {"threadId": self.binding["session"], "includeTurns": True}).get("thread", {})
        if thread.get("id") != self.binding["session"]:
            raise Blocked("Stop inspection returned another thread")
        active = [turn for turn in thread.get("turns", []) if turn.get("status") == "inProgress"]
        own = [turn for turn in active if turn.get("id") in receipts]
        for turn in own:
            self.rpc.call("turn/interrupt", {"threadId": self.binding["session"], "turnId": turn["id"]})
        # Native completion notifications, not this request, establish idle state.
        return "requested" if own else "unknown" if active else "idle"

    def deliver(self, envelope):
        if envelope["binding"] != self.binding or not self.idle():
            raise Blocked("Pinned Codex session unavailable or busy")
        result = self.rpc.call("turn/start", {"threadId": self.binding["session"],
            "clientUserMessageId": envelope["message"]["id"],
            "input": [{"type": "text", "text": prompt(envelope, self.owner_instruction)}]})
        return result["turn"]["id"]  # intended App Server turn acceptance; not agent ACK
