"""Finite Hermes runtime settings. Configuration is not owner authorization."""
import math
import time
from .onboarding import trial_allowed as legacy_trial_allowed
from .worker import Blocked
from . import notification_policy, runtime_policy

DEFAULT_DURATION = 600  # Legacy local trials only; never reinterpret saved approvals.
HOSTED_DEFAULT_DURATION = 3600
DEFAULT_NOTICE_LIMIT = 8
MAX_DURATION = 3600
MAX_NOTICE_LIMIT = 128


def settings(config):
    duration = config.get("hermes_trial_duration_seconds",
                          None if config.get("hosted_consent") is True else DEFAULT_DURATION)
    notices = notification_policy.limit(config, "hermes")
    if (duration is None and "hermes_trial_duration_seconds" in config
            or duration is not None and (type(duration) is not int or not 1 <= duration <= MAX_DURATION)):
        raise Blocked("Hermes trial duration must be 1–3600 seconds")
    return duration, notices


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def connection_grant(config):
    return dict(binding=config.get("prepared_binding"),connection=config.get("prepared_connection"),
                url=config.get("url"),runtime_policy=config.get("runtime_policy"),
                runtime_started_at=config.get("runtime_started_at"),
                notification_policy=notification_policy.policy(config,"hermes"),
                **({'reviewed_provider':config['reviewed_provider']} if 'reviewed_provider' in config else {}))


def connection_grant_matches(config):
    return (config.get("hosted_consent") is True and config.get("connection_runtime_supported") is True
            and config.get("url") == "https://pantheon.talk" and isinstance(config.get("prepared_binding"),dict)
            and isinstance(config.get("prepared_connection"),str)
            and config.get("runtime_policy") == {"mode":"connection"}
            and config.get("trial_expires_at") == 0 and config.get("trial_started_at") == 0
            and "hermes_trial_duration_seconds" not in config
            and finite(config.get("runtime_started_at")) and 0 < config["runtime_started_at"] <= time.time()
            and config.get("hermes_runtime_grant") == connection_grant(config))


def finite_grant_matches(config):
    grant = config.get("hermes_finite_runtime_grant")
    if not isinstance(grant,dict):
        return False
    expected = dict(binding=config.get("prepared_binding"),connection=config.get("prepared_connection"),
        url=config.get("url"),duration_seconds=config.get("hermes_trial_duration_seconds"),
        trial_started_at=config.get("trial_started_at"),trial_expires_at=grant.get("trial_expires_at"),
        timezone=grant.get("timezone"),notification_policy=notification_policy.policy(config,"hermes"))
    if 'reviewed_provider' in config:
        expected['reviewed_provider'] = config['reviewed_provider']
    if 'approved_expires_at' in grant:
        approved = grant['approved_expires_at']
        if (not finite(approved) or not finite(grant.get('trial_started_at'))
                or not grant['trial_started_at'] < approved <= grant['trial_started_at'] + MAX_DURATION
                or not finite(grant.get('trial_expires_at')) or grant['trial_expires_at'] > approved):
            return False
        expected['approved_expires_at'] = approved
    return (config.get("hermes_finite_runtime_supported") is True and config.get("hosted_consent") is True
        and config.get("url") == "https://pantheon.talk" and "runtime_policy" not in config
        and grant == expected and finite(grant.get("trial_started_at"))
        and finite(grant.get("trial_expires_at")) and type(grant.get("duration_seconds")) is int
        and 0 < grant["trial_started_at"] < grant["trial_expires_at"] <= grant["trial_started_at"]+grant["duration_seconds"]
        and (config.get("trial_expires_at") == grant["trial_expires_at"]
             or config.get("allow_runtime_turns") is False and config.get("trial_expires_at") == 0))


def anchored_grant(config):
    return config.get("hermes_runtime_grant") or config.get("hermes_finite_runtime_grant")


def trial_allowed(config):
    duration, _ = settings(config)
    if "runtime_policy" in config:
        return runtime_policy.allowed(config) and connection_grant_matches(config)
    if "hermes_finite_runtime_grant" in config and not finite_grant_matches(config):
        return False
    if "hermes_finite_runtime_grant" in config and "hermes_room_expires_at" in config:
        end = config["hermes_room_expires_at"]
        if not finite(end) or not time.time() < end <= config["hermes_finite_runtime_grant"]["trial_expires_at"]:
            return False
    if not legacy_trial_allowed(config):
        return False
    # Legacy records retain their existing absolute expiry. Fresh preparation
    # opts into an anchored duration; reads/reloads never slide that window.
    if "hermes_trial_duration_seconds" not in config and config.get("hosted_consent") is not True:
        return True
    start, expiry = config.get("trial_started_at"), config.get("trial_expires_at")
    return (finite(start) and finite(expiry) and 0 < start <= time.time()
            and start < expiry <= start + (duration if duration is not None else MAX_DURATION))


def describe(config, used=None):
    duration, limit = settings(config)
    expiry = config.get("trial_expires_at", 0)
    if finite(config.get("hermes_room_expires_at")) and finite(expiry):
        expiry = min(expiry,config["hermes_room_expires_at"])
    unapproved_connection = (config.get("connection_runtime_supported") is True
        and config.get("allow_runtime_turns") is False and config.get("trial_started_at") == 0
        and expiry == 0 and "runtime_policy" not in config and "hermes_trial_duration_seconds" not in config)
    result = dict(duration_seconds=duration, duration_anchored=("hermes_trial_duration_seconds" in config or config.get("hosted_consent") is True),
                  maximum_duration_seconds=None if "runtime_policy" in config or unapproved_connection else MAX_DURATION,
                  runtime_policy=config.get("runtime_policy", {"mode":"unapproved" if unapproved_connection else "finite"}),
                  supported_runtime_policies=(["finite", "connection"] if config.get("hermes_finite_runtime_supported") is True
                                             else ["connection"]) if unapproved_connection else [],
                  proposed_duration_seconds=config.get("proposed_hermes_duration_seconds"),
                  notice_limit=limit, notification_policy=notification_policy.policy(config, "hermes"), trial_expires_at=expiry,
                  remaining_seconds=None if config.get("runtime_policy") == {"mode":"connection"} else
                      (max(0, expiry-time.time()) if finite(expiry) else 0),
                  runtime_authorized=trial_allowed(config),
                  notice="Limits bound connector notices and new operations, not total model calls, tokens, spend or in-flight turns.")
    if used is not None:
        result.update(notification_policy.describe(config, "hermes", used))
    return result
