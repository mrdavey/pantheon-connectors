"""Finite native-shell wait; never creates, steers or queues a Codex turn."""
import time
from .onboarding import Onboarding, trial_allowed
from .worker import Blocked
from .codex_delivery import ACTIVE_WAIT, selected


def wait_for_unread(config, binding, args, *, service=None, clock=time.monotonic, pause=time.sleep):
    if selected(config) != ACTIVE_WAIT:
        raise Blocked('Bounded active waiting was not selected')
    if config.get('allow_active_wait') is not True:
        raise Blocked('This trial has not approved a bounded active-turn wait')
    if (not isinstance(args, dict) or set(args) != {'connection', 'seconds'}
            or type(args['seconds']) is not int or not 1 <= args['seconds'] <= 20
            or type(args['connection']) is not str):
        raise Blocked('Expected connection and an integer seconds from 1 to 20')
    service = service or Onboarding(config)
    deadline = clock() + args['seconds']
    while True:
        if not trial_allowed(config):
            raise Blocked('Local trial authorization expired')
        _, _, client = service.member(args['connection'], binding)
        # Bound individual network waits too. A timeout/error ends this command;
        # no retry, alternate transport, hidden model call or receipt occurs.
        client.timeout = min(1, max(.01, deadline - clock()))
        pending = client.request('/v1/pending')
        if not trial_allowed(config):
            raise Blocked('Local trial authorization expired during wait')
        if any(item.get('binding') != binding or item.get('protocol') != 'pyros.v1' for item in pending):
            raise Blocked('Pending input changed native binding or protocol')
        if pending:
            return dict(connection=args['connection'], has_unread=True, status='unread', autonomous_idle_wake=False)
        remaining = deadline - clock()
        if remaining <= 0:
            return dict(connection=args['connection'], has_unread=False, status='timeout', autonomous_idle_wake=False)
        pause(min(.25, remaining))
