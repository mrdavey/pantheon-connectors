"""One resumable native connection operation; transport never manufactures consent.

The adapter supplies identity and the existing prepare/authorize/join operations.
Receipts and readiness remain explicit agent decisions after reading actual input.
"""
import math
import time
from pathlib import Path

from ..worker import Blocked
from ..client import APIError
from ..state import load_private, save_private


CONSENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'native_access': {'type': 'boolean'},
        'existing_provider_usage': {'type': 'boolean'},
        'runtime_policy': {'type': 'object', 'properties': {'mode': {'const': 'connection'}},
                           'required': ['mode'], 'additionalProperties': False},
        'expires_at': {'type': 'number'},
        'remember_until': {'type': 'number', 'description':
            'Only after separate explicit consent: reuse this review for new human-supplied invitations in this SAME native conversation. Reused runtime grants end by this fixed UTC epoch (at most one hour); retries never extend it.'},
    },
    'required': ['native_access', 'existing_provider_usage'],
    'oneOf': [{'required': ['runtime_policy']}, {'required': ['expires_at']}],
    'additionalProperties': False,
}

TOOL = {
    'name': 'pantheon_connect',
    'description': 'Connect a human-supplied invitation in this native conversation. Preview and request only missing consent, then prepare, authorize and join in one resumable operation. Reuse valid saved scope; never renew expired/withdrawn grants or ACK unread messages. Optional consent is supplied only after the human approves its exact scope.',
    'inputSchema': {'type': 'object', 'properties': {
        'link': {'type': 'string'}, 'deliberate': {'type': 'boolean'},
        'name': {'type': 'string'}, 'consent': CONSENT_SCHEMA,
    }, 'required': ['link', 'deliberate'], 'additionalProperties': False},
}


def validate(args, *, now=None):
    now = time.time() if now is None else now
    if (not isinstance(args, dict) or set(args) - {'link', 'deliberate', 'name', 'consent'}
            or not {'link', 'deliberate'} <= set(args) or args['deliberate'] is not True
            or not isinstance(args['link'], str)
            or ('name' in args and not isinstance(args['name'], str))):
        raise Blocked('Use the exact human-supplied invitation and deliberate joining intent')
    consent = args.get('consent')
    if consent is None:
        return None
    if (not isinstance(consent, dict) or set(consent) - set(CONSENT_SCHEMA['properties'])
            or consent.get('native_access') is not True
            or consent.get('existing_provider_usage') is not True
            or ('runtime_policy' in consent) == ('expires_at' in consent)):
        raise Blocked('Explicit native access, existing-provider usage and one runtime lifetime are required')
    if 'runtime_policy' in consent and consent['runtime_policy'] != {'mode': 'connection'}:
        raise Blocked('Unsupported runtime lifetime')
    for key in ('expires_at', 'remember_until'):
        if key in consent and (type(consent[key]) not in (float, int)
                or not math.isfinite(consent[key]) or not now < consent[key] <= now + 3600):
            raise Blocked('The approved fixed deadline must be in the next hour; never renew it on retry')
    return dict(consent)


def run(args, *, prepare, authorize, join, inspect, preview, recall=lambda: None,
        remember=lambda consent, state: None):
    """Callbacks always operate in the adapter's authenticated native identity."""
    explicit = validate(args)
    prior = inspect()
    if prior.get('previous') and not prior.get('active'):
        return {'status': 'runtime_inactive', 'next_action': 'stop_inactive_grant',
                'notice': 'This connection is expired or disabled. Preserve it; connect never renews it.'}
    remembered = None if prior.get('previous') or explicit else recall()
    consent = explicit or (remembered or {}).get('consent')
    if consent is not None:
        validate({**args, 'consent': consent})
    if not prior.get('active') and consent is None:
        return {'status': 'consent_required', 'scope': preview(),
                'next_action': 'review_missing_scope_once',
                'notice': 'Approve native incoming access and use of this conversation\'s existing provider, with a connection lifetime or fixed deadline. Installation/tool trust is separate. No daemon inspection, join or runtime has started.'}
    prepared = prepare(consent is not None)
    if prepared.get('status') == 'blocked':
        return prepared
    state = inspect()
    if remembered and state.get('provider') != remembered.get('provider'):
        raise Blocked('Provider/account metadata changed since the remembered review; keep runtime disabled and review only the changed scope')
    if state.get('previous'):
        if not state.get('active'):
            return {'status': 'runtime_inactive', 'next_action': 'stop_inactive_grant'}
        if explicit and state.get('lifetime') != {k: explicit[k] for k in ('runtime_policy', 'expires_at') if k in explicit}:
            raise Blocked('Connect retries must preserve the original approved lifetime')
    else:
        if consent is None:
            raise Blocked('No applicable native consent')
        result = authorize(consent)
        if result.get('status') in ('blocked', 'runtime_not_authorized'):
            return result
        state = inspect()
        if not state.get('active'):
            raise Blocked('Runtime authorization did not become active; preserve preparation')
    # Join persists the exact redemption intent before sending it. A bounded
    # retry resumes that same member, including a failed read after redemption.
    # Never rerun authorization or extend the original consent on this path.
    authorized = state
    for attempt in range(2):
        try:
            joined = join()
        except APIError as error:
            if not error.retryable():
                raise
            joined = {'status': 'blocked', 'reason': error.guidance(), 'retry_same_join': True}
        if joined.get('retry_same_join') is not True:
            break
        if attempt == 0:
            time.sleep(0.5)
        current = inspect()
        if not current.get('active'):
            return {'status': 'runtime_inactive', 'next_action': 'stop_inactive_grant'}
        if any(current.get(k) != authorized.get(k) for k in ('lifetime', 'provider', 'connection')):
            raise Blocked('Connection scope changed during recovery; preserve the original grant')
        if attempt == 1:
            return {**joined, 'status': 'connection_pending',
                    'connection': current['connection'], 'delivery_verified': False,
                    'next_action': 'resume_same_connect_after_service_recovers',
                    'next': 'Preserve this connection and the original invitation, arguments and deadline. The exact saved redemption may already have committed. Resume pantheon_connect with those same values after service recovers; do not create another membership, renew consent or report delivery verified.'}
    if joined.get('status') == 'blocked':
        return joined
    if not joined.get('connection'):
        raise Blocked('Join did not establish a connection; preserve its retry state')
    if explicit and 'remember_until' in explicit:
        remember(explicit, state)
    return {**joined, 'onboarding_status': 'connected', 'consent_reused': bool(prior.get('active') or remembered),
            'delivery_verified': False, 'next_action': 'receive_read_ack_then_ready',
            'next': 'Fetch the scoped input, read it, explicitly ACK its message IDs and satisfy its readiness instruction. Then call pantheon_history to read permitted earlier context in 10-entry pages; use next as cursor for older entries as needed and explicitly ACK only other authors’ entries actually read. Past messages are context, not fresh work. Joining alone is not delivery verification.'}


class Review:
    """Explicit, fixed-deadline review, scoped to one authenticated conversation.

    It contains no invitation or membership credential. It never authorizes a
    different conversation, provider, or a previously fenced connection.
    Callers serialize reads/writes with their conversation lock.
    """
    def __init__(self, path, binding):
        self.path, self.binding = Path(path), binding

    def recall(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        saved = load_private(self.path)
        if saved.get('binding') != self.binding:
            raise Blocked('Remembered review belongs to a different native conversation')
        end = saved.get('expires_at')
        if type(end) not in (int, float) or not math.isfinite(end):
            raise Blocked('Remembered review has an invalid deadline')
        if end <= time.time():
            return None
        return {'provider': saved.get('provider'), 'consent': {
            'native_access': True, 'existing_provider_usage': True, 'expires_at': end}}

    def remember(self, consent, state):
        end = min(consent['remember_until'], consent.get('expires_at', consent['remember_until']))
        value = {'binding': self.binding, 'provider': state['provider'], 'expires_at': end}
        if self.path.exists() or self.path.is_symlink():
            saved = load_private(self.path)
            if saved == value:
                return
            if (saved.get('binding') != self.binding or type(saved.get('expires_at')) not in (int, float)
                    or not math.isfinite(saved['expires_at']) or saved['expires_at'] > time.time()):
                raise Blocked('A saved review cannot be widened or renewed by connect; preserve its fixed deadline')
            # Only a NEW explicit review can replace an expired review. Recall
            # returns None after expiry; retrying the old consent fails validation.
        save_private(self.path, value)
