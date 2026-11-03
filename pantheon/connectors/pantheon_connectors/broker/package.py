"""Inert immutable-package metadata, never model-supplied routing instructions."""
import json
from pathlib import Path

from ..worker import Blocked


def metadata(root=None):
    path = (Path(root) if root else Path(__file__).resolve().parents[3]) / 'broker-package.json'
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) != {'contract', 'package_version'}
            or value['contract'] != 'pantheon.broker.v1' or not isinstance(value['package_version'], str)):
        raise Blocked('Invalid broker package metadata')
    return value
