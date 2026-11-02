import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from pantheon_connectors import sites
from pantheon_connectors.worker import Blocked
from pantheon_connectors.state import load_private


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'index.html').write_text('<html>Private example</html>')

    def test_static_bytes_and_stable_digest(self):
        (self.root / 'app.js').write_text('document.title="Hello"')
        files, digest = sites.bundle(str(self.root))
        self.assertEqual(len(files), 2)
        self.assertIn(b'Private example', base64.b64decode(next(f for f in files if f['path'] == 'index.html')['data']))
        self.assertEqual(digest, sites.bundle(str(self.root))[1])

    def test_rejects_secrets_and_provider_code(self):
        for name, content in [('.env', 'secret'), ('_worker.js', 'code'), ('app.js.map', '{}'), ('package.json', '{}'), ('app.js', 'ghp_' + 'x'*32)]:
            with self.subTest(name=name):
                p = self.root / name
                p.write_text(content)
                with self.assertRaises(Blocked):
                    sites.bundle(str(self.root))
                p.unlink()

    def test_rejects_symlink_file_directory_and_parent(self):
        link = self.root / 'copy.html'
        link.symlink_to(self.root / 'index.html')
        with self.assertRaises(Blocked): sites.bundle(str(self.root))
        link.unlink()
        target = self.root / 'outside'
        target.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(Blocked): sites.bundle(str(self.root))
        with self.assertRaises(Blocked): sites.bundle(str(alias))

    def test_rejects_large_file_case_collision_missing_entry(self):
        p = self.root / 'app.js'
        p.write_bytes(b'x'*(sites.MAX_FILE+1))
        with self.assertRaises(Blocked): sites.bundle(str(self.root))
        p.unlink()
        (self.root/'index.html').unlink()
        p.write_text('valid')
        with self.assertRaises(Blocked): sites.bundle(str(self.root))

    def test_disabled_server_does_not_read_files(self):
        service = Mock()
        client = Mock()
        client.request.return_value = {'website_publishing': {'available': False}}
        service.member.return_value = (self.root/'state.json', {}, client)
        with self.assertRaises(Blocked): sites.execute(service, 'pantheon_site_deploy', {'connection':'x', 'directory':'/not-readable', 'key':'valid-request-key'}, {})
        client.request.assert_called_once_with('/v1/manifest')

    def test_tools_do_not_grant_publication_or_take_cloud_credentials(self):
        for tool in sites.TOOLS:
            self.assertFalse(tool['inputSchema']['additionalProperties'])
            self.assertNotIn('visibility', tool['inputSchema']['properties'])
            self.assertNotIn('token', tool['inputSchema']['properties'])

    def test_guidance_matches_automatic_static_publication(self):
        root = Path(__file__).resolve().parents[2]
        paths = [root / 'connectors/pantheon_connectors/skills/collaborate/SKILL.md']
        provenance = root / 'SOURCE-PROVENANCE.json'
        if provenance.is_file():
            # The native Hermes artifact deliberately excludes the Codex skill.
            package = json.loads(provenance.read_text())
            self.assertEqual(package['format'], 'pantheon.hermes-distribution.v1')
            self.assertIn(str(paths[0].relative_to(root)), package['files'])
            self.assertNotIn('skills/pantheon/SKILL.md', package['files'])
        else:
            paths.append(root / 'skills/pantheon/SKILL.md')
        for path in paths:
            text = path.read_text().split('## Static website publishing', 1)[1]
            self.assertIn('no separate website opt-in', text)
            self.assertIn('pantheon_site_status', text)
            self.assertIn('Private context, credentials and chat history stay private', text)
            self.assertNotIn('permission separately', text)
        deploy = next(tool for tool in sites.TOOLS if tool['name'] == 'pantheon_site_deploy')['description']
        self.assertIn('static website', deploy)
        self.assertIn('automatic public grant', deploy)
        self.assertIn('no server code', deploy)

    def tool_fixture(self):
        service, client = Mock(), Mock()
        state = self.root / 'connection.json'
        service.member.return_value = (state, {}, client)
        manifest = {'website_publishing': {'available': True, 'protocol': 'pantheon.sites.v1'}}
        grant = {'id': 'site-id', 'enabled': True, 'expires': '2099-01-01T00:00:00Z'}
        # Keep journal files outside the selected output directory.
        output = self.root / 'output'
        output.mkdir()
        (output / 'index.html').write_text('<html>Hosting test</html>')
        args = {'connection': 'test', 'directory': str(output), 'key': 'stable-request-key'}
        return service, client, state, manifest, grant, args

    def test_upload_preserves_bytes_and_records_metadata_only(self):
        service, client, state, manifest, grant, args = self.tool_fixture()
        client.request.side_effect = [manifest, grant, {'state': 'ready', 'deployment': 'deployment-id'}]
        result = sites.execute(service, 'pantheon_site_deploy', args, {'native': 'binding'})
        service.member.assert_called_once_with('test', {'native': 'binding'})
        self.assertEqual(result['state'], 'ready')
        path, body = client.request.call_args.args
        self.assertEqual(path, '/v1/sites/current/deploy')
        self.assertEqual(body['key'], args['key'])
        self.assertEqual(base64.b64decode(body['files'][0]['data']), b'<html>Hosting test</html>')
        receipt = load_private(state.with_suffix('.site-operation.json'))
        self.assertEqual(set(receipt), {'key', 'digest', 'site', 'state', 'deployment'})
        self.assertEqual(receipt['deployment'], 'deployment-id')

    def test_pending_and_expired_grants_never_upload(self):
        service, client, state, manifest, grant, args = self.tool_fixture()
        for changes in ({'pending': 'operation-id'}, {'expires': '2000-01-01T00:00:00Z'}):
            client.reset_mock(side_effect=True)
            client.request.side_effect = [manifest, {**grant, **changes}]
            with self.assertRaises(Blocked): sites.execute(service, 'pantheon_site_deploy', args, {})
            self.assertEqual(client.request.call_count, 2)

    def test_reconcile_is_read_only_provider_operation_without_local_files(self):
        service, client, state, manifest, grant, args = self.tool_fixture()
        client.request.side_effect = [manifest, {'state': 'ready'}]
        self.assertEqual(sites.execute(service, 'pantheon_site_status', {'connection': 'test', 'reconcile': True}, {}), {'state': 'ready'})
        client.request.assert_called_with('/v1/sites/current/reconcile', {})
        self.assertFalse(state.with_suffix('.site-operation.json').exists())

if __name__ == '__main__': unittest.main()
