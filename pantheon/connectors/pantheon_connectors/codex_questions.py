"""Durable native human-question claims; never store question or answer text."""
import hashlib
import json
from .onboarding import private_directory, private_lock
from .state import load_private, save_private
from .worker import Blocked

QUESTION_TOOL = dict(name='pantheon_question', description='Claim a native human question once, mark it answered/cancelled, or list pending markers. Ask the human in this native conversation only when should_prompt is true. Never put question or answer text in this marker.',
    inputSchema=dict(type='object', properties={
        'connection': {'type': 'string', 'minLength': 1, 'maxLength': 256},
        'id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'state': {'type': 'string', 'enum': ['pending', 'answered', 'cancelled']},
        'context_version': {'type': 'integer', 'minimum': 0}},
        required=['connection'], additionalProperties=False))


def question(config, connection, id=None, state=None, context_version=None, *, current_context=None):
    if not isinstance(connection, str) or not connection.strip() or len(connection) > 256:
        raise Blocked('Invalid question connection')
    if id is None:
        if state is not None or context_version is not None:
            raise Blocked('Question state requires an ID')
    elif (not isinstance(id, str) or not id.strip() or len(id) > 128
          or state not in ('pending', 'answered', 'cancelled')):
        raise Blocked('Invalid question marker')
    if context_version is not None and (type(context_version) is not int or context_version < 0):
        raise Blocked('Invalid question context version')
    current_context = current_context or {}
    if any(type(current_context.get(key)) is not int or current_context[key] < 0 for key in ('context_version', 'config')):
        raise Blocked('Fresh question context is required')
    if state == 'pending' and context_version != current_context['context_version']:
        raise Blocked('Question context changed')
    binding = config.get('binding', {})
    if binding.get('runtime') != 'codex' or not isinstance(binding.get('session'), str) or not binding['session']:
        raise Blocked('Question requires a native Codex binding')
    scope = {'binding': binding, 'connection': connection, 'url': config.get('url')}
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    directory = private_directory(config['state_dir'])
    path = directory / ('codex-questions-' + digest + '.json')
    with private_lock(directory / 'codex-questions.lock'):
        saved = load_private(path) if path.exists() else {'scope': scope, 'markers': {}}
        if saved.get('scope') != scope or not isinstance(saved.get('markers'), dict):
            raise Blocked('Question scope changed')
        markers = saved['markers']
        for marker in markers.values():
            if marker['state'] == 'pending' and any(marker[key] != current_context[key] for key in ('context_version', 'config')):
                marker['state'] = 'cancelled'
        should_prompt = False
        if id is not None:
            if id in markers:
                if state != 'pending':
                    markers[id]['state'] = state
            elif state != 'pending':
                raise Blocked('Question marker was not found')
            else:
                if len(markers) >= 1000:
                    raise Blocked('Question marker limit reached')
                markers[id] = dict(connection=connection, id=id, state='pending',
                    context_version=current_context['context_version'], config=current_context['config'])
                should_prompt = True
        save_private(path, saved)
        return ({**markers[id], 'should_prompt': should_prompt} if id is not None else
                {'pending': [dict(marker) for marker in markers.values() if marker['state'] == 'pending'], 'should_prompt': False})


def cancel_all(config, connection):
    """Fence local pending markers after verified withdrawal/closure."""
    scope = {'binding': config.get('binding', {}), 'connection': connection, 'url': config.get('url')}
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    directory = private_directory(config['state_dir'])
    path = directory / ('codex-questions-' + digest + '.json')
    with private_lock(directory / 'codex-questions.lock'):
        if not path.exists():
            return 0
        saved = load_private(path)
        if saved.get('scope') != scope or not isinstance(saved.get('markers'), dict):
            raise Blocked('Question scope changed')
        count = 0
        for marker in saved['markers'].values():
            if marker['state'] == 'pending':
                marker['state'] = 'cancelled'
                count += 1
        save_private(path, saved)
        return count
