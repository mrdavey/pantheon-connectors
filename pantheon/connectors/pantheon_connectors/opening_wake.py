"""One content-free deadline wake inside an existing authorized receiver.

The server owns the episode, membership, deadline and atomic opening decision.
The normal notice journal reserves each episode before native delivery, retaining
all uncertain-send fences. No model timer, ACK or authority is manufactured.
"""
from datetime import datetime
import time


def due(view, binding, connection, entries, *, now=None):
    if not isinstance(view, dict):
        return None
    opening = view.get('opening')
    chat = view.get('chat', {})
    member = chat.get('members', {}).get(view.get('member'), {})
    if (not isinstance(opening, dict) or opening.get('version') != 'pantheon.opening.v1'
            or view.get('role') != 'agent' or chat.get('state') not in ('waiting', 'active')
            or member.get('revoked') is not False or member.get('binding') != binding
            or member.get('ready') != chat.get('config')
            or opening.get('message_id') or opening.get('config') != chat.get('config')):
        return None
    episode = opening.get('episode')
    if not isinstance(episode, str) or not episode or len(episode) > 256:
        return None
    if any(n.get('connection') == connection and n.get('opening_episode') == episode for n in entries):
        return None
    try:
        deadline = datetime.fromisoformat(opening['deadline'].replace('Z', '+00:00'))
        if deadline.tzinfo is None or deadline.timestamp() > (time.time() if now is None else now):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return episode
