"""Read-only onboarding next steps. These hints never establish host/provider consent."""
import re
from . import hermes_limits


def progress(config, binding):
    common = dict(host_prompts='native_host_managed_not_bypassed',
                  host_incoming_permission='verify_or_reuse_exact_still_valid_approval',
                  provider_usage='use_verified_existing_metadata_or_review_only_missing_usage_scope',
                  membership='server_authority_check_required_before_work',
                  count_condition="Other room/provider/incoming scope is still verified and unchanged; otherwise review only the missing scope once.",
                  extra_preparation_duration_start_confirmations=0)
    if (config.get('prepared_binding') != binding or not isinstance(config.get('prepared_connection'),str)
            or not re.fullmatch(r'[a-f0-9]{32}',config['prepared_connection'])):
        return dict(common,state='scope_check_required',next_action='check_exact_saved_scope',
                    pantheon_confirmations=None,
                    notice='Verify the existing scope; do not request fresh broad consent to repair identity or loading.')
    try:
        allowed = hermes_limits.trial_allowed(config)
    except Exception:
        return dict(common,state='invalid_saved_scope',next_action='stop_preserve_saved_scope',
                    pantheon_confirmations=0,notice='Report the saved-scope error only. Do not restart onboarding or replace its grant.')
    if allowed:
        return dict(common,state='saved_runtime_approval_valid_locally',
                    next_action='continue_same_scope_after_member_and_access_checks',pantheon_confirmations=0,
                    notice='Reuse the identical still-valid runtime approval. Do not ask for preparation, duration or start approval again. This is not proof of host permission, provider usage approval or live membership.')
    previous = (hermes_limits.anchored_grant(config) or config.get('trial_started_at')
                or config.get('runtime_started_at') or config.get('trial_expires_at')
                or config.get('cleanup') or config.get('allow_runtime_turns') is True)
    if previous:
        disabled = config.get('allow_runtime_turns') is not True
        return dict(common,state='saved_scope_disabled' if disabled else 'saved_scope_expired',
                    next_action='stop_preserve_disabled_scope' if disabled else 'stop_preserve_expired_scope',
                    pantheon_confirmations=0,
                    notice='The prior allowance is disabled or expired. Report that specific blocker; no automatic renewal, reactivation, replacement or new start question.')
    if (config.get('hermes_finite_runtime_supported') is not True
            and config.get('connection_runtime_supported') is not True):
        return dict(common,state='legacy_scope_check_required',next_action='check_existing_legacy_scope',
                    pantheon_confirmations=None,
                    notice='Keep the existing legacy preparation and its configured limits. Verify its applicable contract rather than proposing a new default or broad approval.')
    duration = config.get('proposed_hermes_duration_seconds',
                          config.get('hermes_trial_duration_seconds',hermes_limits.HOSTED_DEFAULT_DURATION))
    return dict(common,state='new_scope_needs_review',next_action='preview_scope_then_one_missing_approval',
                pantheon_confirmations=1,proposed_duration_seconds=duration,
                notice='Finish read-only room/goal and usage checks, reuse settled applicable access approval, then bundle only missing joining/runtime/incoming scope into one Pantheon confirmation. The duration default is not consent; no separate prepare, duration-selection or start question.')
