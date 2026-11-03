"""Hermes native plugin entry point; loaded only after host installation consent."""


def register(ctx):
    # Hermes owns this package namespace and evicts it on supported reloads.
    # A top-level import survives those reloads and can reuse another profile's
    # connector code after an upgrade, even when the installed bytes are fresh.
    from .connectors.pantheon_connectors.hermes_plugin import register as native_register
    return native_register(ctx)
