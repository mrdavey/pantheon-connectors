"""Shared native workspace defaults; these paths never grant authority."""
from pathlib import Path


def folders(root):
    root = Path(root)
    if not root.is_absolute():
        raise ValueError('An absolute native profile directory is required')
    return {'transfers': str(root / 'files'), 'sites': str(root / 'sites')}
