"""Temporary legacy environment aliases; never expose conflicting values."""
import os


def get(suffix, default=None, *, environ=None):
    values = os.environ if environ is None else environ
    current, legacy = "PANTHEON_" + suffix, "PYROS_" + suffix
    if current in values and legacy in values and values[current] != values[legacy]:
        raise ValueError(f"Conflicting {current} and {legacy}")
    return values.get(current, values.get(legacy, default))
