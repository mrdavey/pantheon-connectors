"""Claude's documented environment + lifecycle observations. No host launch/login.

These observations are not an atomic dispatch/account attestation. The connector
therefore remains loopback-only even when they all match.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys

from .onboarding import private_directory, private_lock
from .state import load_private, save_private
from .worker import Blocked

VERSION = "0.1.1"


def native_paths(env=None):
    env = os.environ if env is None else env
    session = env.get("CLAUDE_CODE_SESSION_ID", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session):
        raise Blocked("Pantheon cannot identify this Claude conversation. Load it in the intended conversation and check again.")
    profile = Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    if not profile.is_absolute():
        raise Blocked("Claude's configuration directory must be absolute.")
    # Profile location is not an account identifier. Never read login credentials.
    profile_id = hashlib.sha256(str(profile.resolve()).encode()).hexdigest()
    data = env.get("CLAUDE_PLUGIN_DATA")
    if not data:
        raise Blocked("Pantheon's native plugin data directory is unavailable. Reload the approved plugin.")
    root = private_directory(Path(data) / "pantheon-claude" / profile_id)
    return session, profile_id, root


def observe_hook(event, env=None):
    session, profile, root = native_paths(env)
    if not isinstance(event, dict) or event.get("session_id") != session:
        raise Blocked("Claude's lifecycle identity does not match this conversation.")
    path = root / (session + ".json")
    with private_lock(root / "lifecycle.lock"):
        previous = load_private(path) if path.exists() else {}
        kind = event.get("hook_event_name")
        if kind == "SessionStart":
            model, cwd = event.get("model"), event.get("cwd")
            if not isinstance(model, str) or not model or len(model) > 256 or not isinstance(cwd, str) or not Path(cwd).is_absolute():
                save_private(path, dict(previous, session=session, profile=profile, active=False))
                raise Blocked("Claude has not supplied the current model and workspace.")
            # Compaction preserves the same host lifetime, but never reactivates
            # a configuration/model-change fence. Resume gets a new generation.
            compact = event.get("source") == "compact"
            value = dict(session=session, profile=profile, model=model,
                         cwd=str(Path(cwd).resolve()), active=True,
                         generation=secrets.token_hex(16))
            if compact:
                if not previous or any(previous.get(k) != value[k] for k in ("session", "profile", "model", "cwd")):
                    save_private(path, dict(previous, session=session, profile=profile, active=False))
                    raise Blocked("Compaction changed the pinned Claude context.")
                value = previous
        elif kind in ("SessionEnd", "ConfigChange", "PostModelSwitch", "CwdChanged"):
            # No network work in a short-lived shutdown hook. The pump rereads
            # this fence and stops locally; remote withdrawal remains explicit.
            value = dict(previous, session=session, profile=profile, active=False)
        else:
            raise Blocked("Unsupported Claude lifecycle event.")
        save_private(path, value)


class NativeContext:
    def __init__(self, version, env=None):
        self.session, self.profile, self.root = native_paths(env)
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", version):
            raise Blocked("Claude did not supply its client version.")
        self.version = version
        self.observation = None

    def binding(self):
        value = load_private(self.root / (self.session + ".json"))
        if (value.get("active") is not True or value.get("session") != self.session
                or value.get("profile") != self.profile):
            raise Blocked("This Claude conversation changed or stopped. Check the original conversation before reconnecting.")
        if self.observation is not None and value != self.observation:
            raise Blocked("Claude's conversation or model changed. Reload Pantheon in the intended conversation.")
        self.observation = value
        return dict(runtime="claude-code", surface="mcp-channel", version=self.version,
                    profile=self.profile, session=self.session)

    def scope(self):
        self.binding()
        return {key: self.observation[key] for key in ("model", "cwd")}

    def check(self, binding, scope):
        if self.binding() != binding or self.scope() != scope:
            raise Blocked("Pantheon belongs to a different Claude conversation, profile, model or workspace.")

    def require_stop_scope(self, binding):
        """Removal-only identity; deliberately independent of active lifecycle.

        A stopped/changed model, workspace or native version must not prevent
        disabling its saved grant. This check authorizes no reads or commands.
        """
        if (binding.get("runtime") != "claude-code" or binding.get("surface") != "mcp-channel"
                or binding.get("session") != self.session or binding.get("profile") != self.profile):
            raise Blocked("Only the original conversation and profile may disconnect this connection.")


def main():
    try:
        # Never persist transcript paths, prompts, tool input, credentials or the
        # whole hook payload. Output is intentionally empty on success.
        observe_hook(json.loads(sys.stdin.read(65537)))
    except Exception:
        print("Pantheon could not verify this conversation's lifecycle; incoming work remains blocked.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
