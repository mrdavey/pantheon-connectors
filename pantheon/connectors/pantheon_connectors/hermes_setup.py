"""Disabled, conversation-scoped hosted preparation using native identity only."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .hosted_discovery import TRUSTED_ORIGIN, fetch_discovery, download_package, matches_native_host
from .onboarding import Onboarding, invitation, private_directory, private_lock
from .state import load_private, save_private
from .worker import Blocked
from . import runtime_policy, notification_policy, hermes_limits, hermes_review

VERSION = "0.3.21"
TOOLS = [{"name": "pantheon_prepare", "description": "Prepare the human-supplied hosted invitation in this observed Hermes conversation. Creates private disabled setup only; never installs, joins, enables incoming permission or starts a runtime window.",
          "inputSchema": {"type": "object", "properties": {"link": {"type": "string"}, "deliberate": {"type": "boolean"}, "duration_seconds": {"type":"integer","minimum":1,"maximum":3600,"default":3600}},
                          "required": ["link", "deliberate"], "additionalProperties": False}}]

TOOLS.append({"name": "pantheon_authorize", "description": "Record explicit runtime approval for a NEW hosted Hermes preparation. Omitted duration uses the prepared 60-minute default; explicit connection lifetime remains opt-in. Never enables host incoming permission, joins, starts work, renews or widens a prior finite grant.",
    "inputSchema": {"type":"object","properties":{"connection":{"type":"string"},
        "deliberate":{"type":"boolean"},"runtime_policy":runtime_policy.SCHEMA,
        "notification_policy":notification_policy.SCHEMA,
        "link":{"type":"string"},"duration_seconds":{"type":"integer","minimum":1,"maximum":3600},
        "expires_at":{"type":"number","description":"Explicit fixed UTC epoch, at most one hour away. Mutually exclusive with duration_seconds; never moves on retry."},
        "timezone":{"type":"string","description":"Already known owner IANA timezone, for display only; omit for labeled UTC."}},
        "required":["connection","deliberate"],"additionalProperties":False}})


def authorize_connection(setup, binding, args):
    if (not isinstance(args, dict) or set(args)-{"notification_policy"} != {"connection", "deliberate", "runtime_policy"}
            or args["deliberate"] is not True or args["runtime_policy"] != {"mode":"connection"}):
        raise Blocked("Explicit current connection-lifetime approval is required")
    with private_lock(setup.with_suffix(".authorize.lock")):
        config = load_private(setup)
        if (config.get("connection_runtime_supported") is not True or config.get("hosted_consent") is not True
                or config.get("url") != TRUSTED_ORIGIN or config.get("prepared_binding") != binding
                or config.get("prepared_connection") != args["connection"]):
            raise Blocked("Connection-lifetime approval needs a fresh hosted preparation in this conversation")
        proposed_notices = approved_notification_policy(config, args)
        if "hermes_runtime_grant" in config:
            if (not hermes_limits.connection_grant_matches(config)
                    or proposed_notices != notification_policy.policy(config, "hermes")):
                raise Blocked("Saved runtime grant changed; preserve it for recovery")
            return {"status":"runtime_authorized_existing" if hermes_limits.trial_allowed(config) else "runtime_not_authorized",
                    "notice":"Identical approval retry preserves the original grant; it never reactivates a disconnected connection."}
        # Historical consent remains finite even after it is disabled or expired.
        if (config.get("allow_runtime_turns") is not False or config.get("trial_started_at") != 0
                or config.get("trial_expires_at") != 0 or "hermes_trial_duration_seconds" in config
                or "runtime_policy" in config or "runtime_started_at" in config or config.get("cleanup")
                or any(Path(config["state_dir"]).iterdir())):
            raise Blocked("Existing approval or connection evidence cannot become a connection-lifetime grant")
        config["notification_policy"] = proposed_notices
        config.update(allow_runtime_turns=True,runtime_policy={"mode":"connection"},runtime_started_at=time.time())
        config["hermes_runtime_grant"] = hermes_limits.connection_grant(config)
        save_private(setup,config)
    return {"status":"runtime_authorized", "notice":"Scoped runtime consent recorded until disconnect, revocation or room closure/expiry. Host incoming permission remains separate. No join, receiver or model turn started."}


def approved_notification_policy(config, args):
    """Only explicit first authorization can narrow a prepared notice policy."""
    current = notification_policy.policy(config, "hermes")
    proposed = notification_policy.validate(args["notification_policy"], 8) if "notification_policy" in args else current
    if (current["mode"] == "limited"
            and (proposed["mode"] != "limited" or proposed["limit"] > current["limit"])):
        raise Blocked("Notification approval cannot widen the prepared limit")
    if "hermes_notice_limit" in config and proposed != current:
        raise Blocked("Preserve the existing legacy notification limit")
    return proposed


def duration(value):
    if type(value) is not int or not 1 <= value <= hermes_limits.MAX_DURATION:
        raise Blocked("Hermes duration must be a whole number from 1 to 3600 seconds")
    return value


def expiry_timestamp(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("Timezone required")
        return date.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise Blocked("Invitation/room expiry is unavailable; preserve disabled setup") from None


def finite_disclosure(config, room_expiry=None):
    grant = config["hermes_finite_runtime_grant"]
    end = min(grant["trial_expires_at"],config.get("hermes_room_expires_at",grant["trial_expires_at"]))
    if room_expiry is not None:
        end = min(end, expiry_timestamp(room_expiry))
    local = datetime.fromtimestamp(end, ZoneInfo(grant["timezone"]))
    return {"duration_seconds":grant["duration_seconds"], "expires_at":end,
            "end_time":local.strftime("%-I:%M:%S %p %Z on %-d %B %Y"),
            "notice":"Ends at " + local.strftime("%-I:%M:%S %p %Z on %-d %B %Y") +
                     ", or earlier on disconnect, revocation or room closure/expiry. No automatic renewal."}


def authorize(setup, binding, args, *, preview=None):
    if isinstance(args, dict) and "runtime_policy" in args:
        return authorize_connection(setup, binding, args)
    allowed = {"connection","deliberate","link","duration_seconds","expires_at","timezone","notification_policy"}
    if (not isinstance(args, dict) or set(args)-allowed or not {"connection","deliberate","link"} <= set(args)
            or args["deliberate"] is not True or not isinstance(args["link"],str)):
        raise Blocked("Explicit joining/runtime approval and the original invitation are required; duration defaults to 60 minutes")
    fixed = args.get('expires_at')
    if 'expires_at' in args and ('duration_seconds' in args or type(fixed) not in (int, float)
            or not math.isfinite(fixed) or not time.time() < fixed <= time.time() + 3600):
        raise Blocked('A fixed runtime deadline must be in the next hour and cannot be combined with a duration')
    zone = args.get("timezone","UTC")
    try:
        if not isinstance(zone,str):
            raise ValueError("Invalid timezone")
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError,ValueError):
        raise Blocked("Use the owner's already known IANA timezone or omit it for UTC") from None
    with private_lock(setup.with_suffix(".authorize.lock")):
        config = load_private(setup)
        proposed = 3600 if fixed is not None else duration(args.get("duration_seconds",config.get("proposed_hermes_duration_seconds",hermes_limits.HOSTED_DEFAULT_DURATION)))
        if (config.get("hermes_finite_runtime_supported") is not True or config.get("hosted_consent") is not True
                or config.get("url") != TRUSTED_ORIGIN or config.get("prepared_binding") != binding
                or config.get("prepared_connection") != args["connection"] or handle_for(args["link"]) != args["connection"]):
            raise Blocked("Finite approval needs a fresh compatible hosted preparation in this conversation")
        proposed_notices = approved_notification_policy(config, args)
        if "hermes_finite_runtime_grant" in config:
            if (not hermes_limits.finite_grant_matches(config)
                    or config["hermes_finite_runtime_grant"]["duration_seconds"] != proposed
                    or config['hermes_finite_runtime_grant'].get('approved_expires_at') != fixed
                    or proposed_notices != notification_policy.policy(config, "hermes")):
                raise Blocked("Saved runtime approval differs; retries cannot change its duration")
            return {"status":"runtime_authorized_existing" if hermes_limits.trial_allowed(config) else "runtime_not_authorized",
                    "runtime":finite_disclosure(config)}
        if (config.get("allow_runtime_turns") is not False or config.get("trial_started_at") != 0
                or config.get("trial_expires_at") != 0 or "hermes_trial_duration_seconds" in config
                or "runtime_policy" in config or "runtime_started_at" in config or "hermes_runtime_grant" in config
                or config.get("cleanup") or any(Path(config["state_dir"]).iterdir())):
            raise Blocked("Existing consent or connection evidence cannot be renewed or replaced")
        inspect = preview or Onboarding(config).preview
        scope = inspect(args["link"])["scope"]
        invitation_expiry = expiry_timestamp(scope.get("expires"))
        started = time.time()  # After inspection and explicit approval; never preparation time.
        end = min(fixed if fixed is not None else started+proposed,invitation_expiry)
        if not hermes_limits.finite(end) or end <= started:
            raise Blocked("Invitation expired before activation; no runtime grant recorded")
        config.update(allow_runtime_turns=True,trial_started_at=started,trial_expires_at=end,
                      hermes_trial_duration_seconds=proposed,notification_policy=proposed_notices)
        config["hermes_finite_runtime_grant"] = dict(binding=binding,connection=args["connection"],url=TRUSTED_ORIGIN,
            duration_seconds=proposed,trial_started_at=started,trial_expires_at=end,timezone=zone,
            notification_policy=notification_policy.policy(config,"hermes"))
        if 'reviewed_provider' in config:
            config['hermes_finite_runtime_grant']['reviewed_provider'] = config['reviewed_provider']
        if fixed is not None:
            config['hermes_finite_runtime_grant']['approved_expires_at'] = fixed
        save_private(setup,config)
    return {"status":"runtime_authorized", "runtime":finite_disclosure(config),
            "notice":"Runtime approval recorded. Host permission remains separate; no join, receiver or model turn started."}


def handle_for(link):
    token = invitation(link, TRUSTED_ORIGIN)
    return hashlib.sha256((TRUSTED_ORIGIN + "\0" + token).encode()).hexdigest()[:32]


def conversation_directory(root, binding):
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return Path(root) / digest


def verify_installed(package, root=None):
    """Compare loaded source with immutable exported provenance, not a version claim."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    provenance = json.loads((root / "SOURCE-PROVENANCE.json").read_text())
    from .broker.package import metadata
    broker = metadata(root)
    if provenance.get("source_commit") != package["source_revision"] or package["version"] != (broker['package_version'] if broker else VERSION):
        raise Blocked("Installed Hermes package differs from hosted discovery")
    records = provenance.get("files", {})
    required = {"plugin.yaml", "__init__.py", "connectors/pantheon_connectors/hermes_plugin.py",
                "connectors/pantheon_connectors/hermes_setup.py", "connectors/pantheon_connectors/hosted_discovery.py"}
    if broker:
        required |= {'broker-package.json', 'connectors/pantheon_connectors/hosted_v3.py'}
        required |= {'connectors/pantheon_connectors/broker/' + name + '.py' for name in ('client','catalog','adapter','transport','package','__init__')}
    if not required.issubset(records):
        raise Blocked("Installed package provenance is incomplete")
    for name, record in records.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (root / path, *(root / path).parents)):
            raise Blocked("Installed provenance has an unsafe path")
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != record["sha256"]:
            raise Blocked("Installed Hermes package failed integrity verification")


def stage_native_source(descriptor, output, *, opener=None):
    """Create an inert Git source for the host's normal installer, never install.

    The caller uses reviewed helper code. Only the hash-verified downloaded tree
    enters this repository, outside existing repositories. No hooks/global Git
    configuration, host profiles, dependencies or runtime processes are involved.
    """
    output = Path(output).absolute()
    if any((parent / ".git").exists() for parent in output.parents):
        raise Blocked("Stage the native source outside existing repositories")
    result = download_package(descriptor, "hermes", output, opener=opener)
    if (output / ".git").exists():
        raise Blocked("Hosted distribution must not contain Git control files")
    verify_installed(descriptor["packages"]["hermes"], output)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Pantheon Distribution", GIT_AUTHOR_EMAIL="distribution@pantheon.invalid",
               GIT_COMMITTER_NAME="Pantheon Distribution", GIT_COMMITTER_EMAIL="distribution@pantheon.invalid",
               GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath="+os.devnull, "-c", "init.templateDir=",
            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-C", str(output), *args],
            env=env, capture_output=True, text=True, check=True).stdout.strip()
    git("init", "--initial-branch=package")
    git("add", "--all")
    git("commit", "-m", "Stage verified Hermes distribution")
    result.update(source=output.as_uri(), artifact_commit=git("rev-parse", "HEAD"))
    return result


def prepare(root, binding, args, *, discover=fetch_discovery, verify=verify_installed, usage=None):
    if (not isinstance(args, dict) or set(args)-{"link", "deliberate", "duration_seconds"} or not {"link","deliberate"} <= set(args)
            or not isinstance(args["link"], str) or args["deliberate"] is not True):
        raise Blocked("Preparation needs the human-supplied invitation and deliberate instruction")
    proposed = duration(args.get("duration_seconds",hermes_limits.HOSTED_DEFAULT_DURATION))
    if (not isinstance(usage, dict) or set(usage) != {'model', 'model_provider'}
            or any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in usage.values())):
        raise Blocked('Observed native provider/model metadata is required for hosted preparation')
    connection = handle_for(args["link"])
    started = time.monotonic()
    from . import hosted_v3
    installed = hosted_v3.installed_hermes()
    broker = installed is not None
    if broker:
        if discover is fetch_discovery:
            discover = hosted_v3.fetch
    descriptor = discover(TRUSTED_ORIGIN, "hermes")
    discovered = time.monotonic()
    package = descriptor["packages"]["hermes"]
    if not matches_native_host(package, binding):
        raise Blocked("Native Hermes compatibility does not match hosted discovery")
    if broker:
        hosted_v3.validate(descriptor, TRUSTED_ORIGIN, 'hermes')
        hosted_v3.check_installed(descriptor, 'hermes', installed)
    verify(installed if broker else package)
    verified = time.monotonic()
    # No network response can select a local path. No credentials are minted.
    private_directory(root)
    directory = private_directory(conversation_directory(root, binding))
    with private_lock(directory / "prepare.lock"):
        index_path = directory / "index.json"
        index = load_private(index_path) if index_path.exists() or index_path.is_symlink() else dict(binding=binding, connections=[], selected=None)
        if index["binding"] != binding:
            raise Blocked("Prepared conversation binding changed")
        setup_dir = private_directory(directory / connection)
        setup = setup_dir / "setup.json"
        if setup.exists() or setup.is_symlink():
            saved = load_private(setup)
            if 'reviewed_provider' in saved and saved['reviewed_provider'] != usage:
                raise Blocked('Prepared provider/model differs; preserve existing consent and review the changed scope')
            if (saved.get("prepared_binding") != binding or saved.get("url") != TRUSTED_ORIGIN
                    or saved.get("prepared_connection") != connection
                    or (not broker and saved.get("hosted_onboarding", {}).get("packages", {}).get("hermes") != package)
                    or (broker and saved.get('hosted_onboarding', {}).get('schema') != hosted_v3.SCHEMA)):
                raise Blocked("Existing preparation differs; preserve it for explicit recovery")
            # Idempotent preparation never resets budgets, flags, expiry or evidence.
            # A fresh descriptor for the same exact package may replace expired
            # discovery metadata; this never renews the independent runtime grant.
            if saved["hosted_onboarding"] != descriptor or (broker and saved.get('installed_connector') != installed):
                if broker:
                    saved = hosted_v3.reprepare_config(saved, descriptor, installed, binding)
                else:
                    saved["hosted_onboarding"] = descriptor
                save_private(setup, saved)
            reused = True
        else:
            private_directory(setup_dir / "state")
            config = dict(url=TRUSTED_ORIGIN, state_dir=str(setup_dir / "state"),
                prepared_binding=binding, prepared_connection=connection,
                hosted_onboarding=descriptor, hosted_consent=True, connection_runtime_supported=True,
                hermes_finite_runtime_supported=True, proposed_hermes_duration_seconds=proposed,
                reviewed_provider=usage,
                allow_runtime_turns=False, trial_started_at=0, trial_expires_at=0,
                notification_policy={"mode":"unlimited"})
            if broker:
                config = hosted_v3.prepare_config(config, descriptor, installed, binding)
            save_private(setup, config)
            reused = False
        if connection not in index["connections"]:
            index["connections"].append(connection)
        index["selected"] = connection
        save_private(index_path, index)
    onboarding = hermes_review.progress(load_private(setup),binding)
    from .broker.bootstrap import prepared
    prepared(load_private(setup), connection, binding)
    return setup, {"status": "prepared_existing" if reused else "prepared_disabled", "connection": connection,
                   "setup_file": str(setup), "native_binding_observed": True,
                   "proposed_duration_seconds":load_private(setup).get("proposed_hermes_duration_seconds"),
                   "onboarding":onboarding,
                   "reviewed_provider":load_private(setup).get('reviewed_provider'),
                   "account_verified":False,
                   "account_limitation":"Native Hermes hooks do not expose a stable account identity; exact-account automatic delivery remains unqualified.",
                   "duration_notice":"For new Hermes connections, disclose the prepared duration with joining approval; do not ask a separate duration-selection question. The clock starts only on authorized activation.",
                   "preparation_timing": {"discovery_seconds": discovered-started,
                       "integrity_seconds": verified-discovered, "private_setup_seconds": time.monotonic()-verified,
                       "evidence": "machine_preparation_only_not_installation_or_invitation_to_ready"},
                   "notice": "Preparation itself grants no incoming permission or runtime. " + onboarding["notice"]}


def select(root, binding, args):
    """Resolve only this conversation's explicit index, never search other profiles."""
    directory = conversation_directory(root, binding)
    index_path = directory / "index.json"
    if not index_path.exists() and not index_path.is_symlink():
        return None
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise Blocked("Prepared state must not contain symlinks")
    index = load_private(index_path)
    if index["binding"] != binding:
        raise Blocked("Prepared conversation binding changed")
    connection = args.get("connection") if isinstance(args, dict) else None
    if isinstance(args, dict) and "link" in args:
        connection = handle_for(args["link"])
    if connection is None:
        connection = index["selected"]
    if not isinstance(connection, str) or not re.fullmatch(r"[0-9a-f]{32}", connection) or connection not in index["connections"]:
        raise Blocked("Invitation or connection is not prepared in this conversation")
    setup = directory / connection / "setup.json"
    if setup.parent.is_symlink():
        raise Blocked("Prepared state must not contain symlinks")
    saved = load_private(setup)
    if saved.get("prepared_binding") != binding or saved.get("prepared_connection") != connection:
        raise Blocked("Prepared setup does not match this conversation")
    return setup
