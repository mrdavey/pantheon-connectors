"""Check public native interfaces, never whitelist a Hermes release or Git hash."""
import inspect

from .worker import Blocked


def accepts(ctx, capability, *args, **kwargs):
    method = getattr(ctx, capability, None)
    try:
        if not callable(method):
            raise TypeError(capability)
        inspect.signature(method).bind(*args, **kwargs)
    except (TypeError, ValueError):
        raise Blocked('Installed Hermes lacks a required native capability: ' + capability) from None


def require_delivery(ctx):
    accepts(ctx, 'inject_message', 'content-free notice', session_key='native-session')


def require_registration(ctx):
    if not isinstance(getattr(ctx, 'profile_name', None), str) or not ctx.profile_name:
        raise Blocked('Hermes native profile metadata is unavailable')
    accepts(ctx, 'get_config', 'setup_file', 'default')
    accepts(ctx, 'register_skill', 'collaborate', 'skill-path', 'description')
    accepts(ctx, 'register_hook', 'pre_tool_call', lambda **kwargs: None)
    accepts(ctx, 'register_tool', name='pantheon_doctor', toolset='pantheon', schema={}, handler=lambda: None)
    accepts(ctx, 'on_unload', lambda: None)
    require_delivery(ctx)
