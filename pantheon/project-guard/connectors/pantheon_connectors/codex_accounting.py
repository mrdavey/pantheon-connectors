"""Numeric accounting from public App Server events, never private transcripts.

Only a completely observed, unmixed native turn accepted by an idle receiver is
attributable. First observed request + later cumulative deltas are a measured
subset; setup, human and auxiliary work are intentionally never called complete.
"""
import copy
import hashlib
import json
from .onboarding import private_directory, private_lock
from .state import load_private, save_private

LIMIT = 10**12


def _scope(config):
    return {key: config.get(key) for key in ('binding', 'url', 'codex_home', 'app_server_socket', 'owner_socket_identity')}


def _path(config):
    scope = _scope(config)
    identity = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return private_directory(config['state_dir']) / ('codex-usage-' + identity + '.json')


def _values(value):
    if not isinstance(value, dict):
        return None
    counts = [value.get('inputTokens'), value.get('outputTokens')]
    return counts if all(type(n) is int and 0 <= n <= LIMIT for n in counts) else None


class Observer:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.thread = config.get('binding', {}).get('session')
        self.turns = {}

    def __call__(self, event):
        params = event.get('params', {})
        if not isinstance(params, dict) or params.get('threadId') != self.thread:
            return
        method = event.get('method')
        turn = params.get('turn', {})
        turn_id = params.get('turnId') or (turn.get('id') if isinstance(turn, dict) else None)
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
            return
        if method == 'turn/started':
            if len(self.turns) >= 1000:
                return
            # Repeated starts cannot reset a mixed marker or accrued counts.
            self.turns.setdefault(turn_id, dict(counts=[0, 0], total=None, mixed=False, measured=False, reset=False))
            if any(item.get('type') == 'userMessage' for item in turn.get('items', []) if isinstance(item, dict)):
                self.turns[turn_id]['mixed'] = True
            return
        row = self.turns.get(turn_id)
        if row is None:
            return  # Connecting midway cannot prove an exclusively owned turn.
        if method in ('item/started', 'item/completed') and params.get('item', {}).get('type') == 'userMessage':
            row['mixed'] = True
        elif method == 'thread/tokenUsage/updated':
            usage = params.get('tokenUsage', {})
            total, last = _values(usage.get('total')), _values(usage.get('last'))
            if total is None or last is None or any(a > b for a, b in zip(last or [], total or [])):
                row['mixed'] = True
                return
            if row['total'] is None:
                delta = last
            else:
                delta = [a-b for a, b in zip(total, row['total'])]
                if any(n < 0 for n in delta):
                    row['reset'] = True
                    row['mixed'] = True
                    return
            row['total'] = total
            if any(a+b > LIMIT for a, b in zip(row['counts'], delta)):
                row['mixed'] = True
                return
            row['counts'] = [a+b for a, b in zip(row['counts'], delta)]
            row['measured'] = True
        elif method == 'turn/completed':
            if turn.get('status') != 'completed':
                row['mixed'] = True
            # Completed turn content is not persisted; only numeric evidence is.
            if any(item.get('type') == 'userMessage' for item in turn.get('items', []) if isinstance(item, dict)):
                row['mixed'] = True
            self._save(turn_id, row)
            del self.turns[turn_id]

    def _save(self, turn_id, row):
        directory = private_directory(self.config['state_dir'])
        notices_path = directory / 'codex-output-notices.json'
        if not notices_path.exists():
            return
        notices = load_private(notices_path)
        if (notices.get('binding') != self.config.get('binding') or
                any(notices.get('scope', {}).get(key) != value for key, value in _scope(self.config).items() if key != 'binding')):
            return
        matching = [n for n in notices.get('notices', {}).values() if n.get('turn_id') == turn_id]
        if len(matching) != 1:
            return
        notice = matching[0]
        if (notice.get('state') not in ('accepted', 'draining', 'completed')
                or notice.get('owner_state_before') != 'idle' or not notice.get('connection')):
            return
        path = _path(self.config)
        with private_lock(directory / 'codex-usage.lock'):
            saved = load_private(path) if path.exists() else {'scope': _scope(self.config), 'turns': {}}
            if saved.get('scope') != _scope(self.config) or len(saved['turns']) >= 100000:
                return
            if turn_id in saved['turns']:
                return  # Reconnect/replay never increments totals again.
            saved['turns'][turn_id] = dict(connection=notice['connection'], counts=row['counts'],
                measured=row['measured'] and not row['mixed'], reset=row['reset'])
            save_private(path, saved)


def snapshot(config, connection):
    status = dict(availability='unavailable', reason='runtime_unavailable', source='host')
    if config.get('binding', {}).get('surface') != 'app-server-tool-output':
        return {'usage_status': status}
    status['reason'] = 'attribution_unavailable'
    path = _path(config)
    if not path.exists():
        return {'usage_status': status}
    saved = load_private(path)
    if saved.get('scope') != _scope(config):
        return {'usage_status': status}
    rows = [row for row in saved.get('turns', {}).values() if row.get('connection') == connection]
    measured = [row for row in rows if row.get('measured')]
    reset = any(row.get('reset') for row in rows)
    if not measured:
        if reset:
            status['reason'] = 'counter_reset'
        return {'usage_status': status}
    counts = [sum(row['counts'][i] for row in measured) for i in range(2)]
    if any(n > LIMIT for n in counts):
        return {'usage_status': status}
    status.update(availability='partial', reason='counter_reset' if reset else 'partial_counters')
    return {'usage': dict(input_tokens=counts[0], output_tokens=counts[1]), 'usage_status': status, '_lineage': path.stem.removeprefix('codex-usage-')}
