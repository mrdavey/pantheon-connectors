import tempfile
import tomllib
import unittest
from pathlib import Path
from pantheon_connectors.broker.codex_trust import configure, proposal, ROUTINE
from pantheon_connectors.worker import Blocked


class RoutineTrust(unittest.TestCase):
    def test_exact_tools_only_preserve_unrelated_and_prior_denials(self):
        original = 'model = "unchanged"\n# Preserve this comment\n[plugins."pantheon@pantheon-hosted".mcp_servers.pantheon.tools.pantheon_post]\napproval_mode = "reject"\n'
        result = proposal(original)
        values = tomllib.loads(result['text'])
        self.assertIn('# Preserve this comment', result['text'])
        self.assertEqual(values['model'], 'unchanged')
        server = values['plugins']['pantheon@pantheon-hosted']['mcp_servers']['pantheon']
        self.assertEqual(server['default_tools_approval_mode'], 'prompt')
        self.assertEqual(set(server['tools']), set(ROUTINE))
        self.assertEqual(server['tools']['pantheon_post']['approval_mode'], 'reject')
        for sensitive in ('pantheon_transfer_send', 'pantheon_transfer_fetch', 'pantheon_site_deploy', 'future_tool', '*'):
            self.assertNotIn(sensitive, server['tools'])
        self.assertEqual(proposal(result['text'])['text'], result['text'])

    def test_preview_does_not_write_and_changed_proposal_cannot_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'config.toml'
            path.write_text('model = "original"\n')
            preview = configure(path)
            self.assertEqual(path.read_text(), 'model = "original"\n')
            path.write_text('model = "new"\n')
            with self.assertRaises(Blocked):
                configure(path, approved=True, expected_sha256=preview['sha256'])
            fresh = configure(path)
            configure(path, approved=True, expected_sha256=fresh['sha256'])
            self.assertEqual(tomllib.loads(path.read_text())['model'], 'new')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_inline_conflicting_layout_is_preserved(self):
        with self.assertRaises(Blocked):
            proposal('plugins = { "pantheon@pantheon-hosted" = { enabled = true } }\n')
