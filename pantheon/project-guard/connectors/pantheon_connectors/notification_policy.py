"""Notification volume is independent of runtime, membership and provider consent."""
from .worker import Blocked

SCHEMA = {'oneOf': [
    {'type': 'object', 'properties': {'mode': {'const': 'unlimited'}},
     'required': ['mode'], 'additionalProperties': False},
    {'type': 'object', 'properties': {'mode': {'const': 'limited'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8}},
     'required': ['mode', 'limit'], 'additionalProperties': False}]}


def validate(value, maximum):
    if not isinstance(value, dict):
        raise Blocked('Explicit notification policy required')
    if value == {'mode': 'unlimited'}:
        return dict(value)
    if (set(value) == {'mode', 'limit'} and value['mode'] == 'limited'
            and type(value['limit']) is int and 1 <= value['limit'] <= maximum):
        return dict(value)
    raise Blocked('Choose unlimited without a limit, or a supported positive limited quota')


def policy(config, runtime):
    field, default, maximum = ('max_notices', None, 8) if runtime == 'codex' else ('hermes_notice_limit', 8, 128)
    if 'notification_policy' in config:
        result = validate(config['notification_policy'], maximum)
        if field in config and (result['mode'] != 'limited' or type(config[field]) is not int or config[field] != result['limit']):
            raise Blocked('Conflicting legacy notification quota and explicit policy')
        return result
    return validate(dict(mode='limited', limit=config.get(field, default)), maximum)


def limit(config, runtime):
    return policy(config, runtime).get('limit')


def exhausted(config, runtime, used):
    maximum = limit(config, runtime)
    return maximum is not None and used >= maximum


def describe(config, runtime, used):
    p = policy(config, runtime)
    maximum = p.get('limit')
    return dict(notification_policy=p, notices_used=used,
                notices_remaining=None if maximum is None else max(0, maximum-used),
                budget_exhausted=maximum is not None and used >= maximum,
                cost_notice='Notification counts are not model-call, token or spend limits; runtime expiry and cancellation remain independent.')
