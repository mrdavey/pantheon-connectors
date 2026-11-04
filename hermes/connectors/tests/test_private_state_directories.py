"""All connectors create private ancestors; only managed roots allow migration."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pantheon_connectors.onboarding import private_directory
from pantheon_connectors.state import save_private, Journal
from pantheon_connectors.worker import Blocked


class PrivateStateDirectories(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_each_missing_ancestor_is_private_with_permissive_umask(self):
        previous = os.umask(0)
        try:
            for writer in ('directory', 'save', 'journal'):
                with self.subTest(writer=writer):
                    path = self.root / writer / 'pantheon' / 'hosted'
                    if writer == 'directory':
                        private_directory(path)
                    elif writer == 'save':
                        save_private(path / 'setup.json', {'grant': 'unchanged'})
                    else:
                        journal = Journal(path / 'state.db', {'runtime': 'future'})
                        journal.db.close()
                    for directory in (path, path.parent, path.parent.parent):
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        finally:
            os.umask(previous)

    def test_explicit_owned_root_migration_preserves_state_and_parent(self):
        self.root.chmod(0o755)
        path = self.root / 'pantheon'
        path.mkdir(mode=0o755)
        state = path / 'setup.json'
        save_private(state, {'deadline': 123, 'binding': 'original'})
        before = state.read_bytes()
        with self.assertRaises(Blocked):
            private_directory(path)
        private_directory(path, repair_owned=True)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
        self.assertEqual(state.read_bytes(), before)

    def test_migration_rejects_shared_writable_foreign_and_symlink_roots(self):
        path = self.root / 'pantheon'
        path.mkdir()
        for mode in (0o775, 0o777):
            path.chmod(mode)
            with self.assertRaises(Blocked):
                private_directory(path, repair_owned=True)
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        path.chmod(0o755)
        with patch('pantheon_connectors.onboarding.os.getuid', return_value=os.getuid()+1):
            with self.assertRaises(Blocked):
                private_directory(path, repair_owned=True)
        link = self.root / 'link'
        link.symlink_to(path, target_is_directory=True)
        with self.assertRaises(Blocked):
            private_directory(link, repair_owned=True)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
