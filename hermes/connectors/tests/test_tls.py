import io
import json
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch
from pantheon_connectors import tls, codex_mcp
from pantheon_connectors.client import Client, APIError


class TLSTests(unittest.TestCase):
    def test_macos_adds_system_trust_and_preserves_verification(self):
        context = ssl.create_default_context()
        with patch.object(tls.sys, 'platform', 'darwin'), patch.dict(tls.os.environ, {}, clear=True), patch.object(tls.Path, 'is_file', return_value=True), patch.object(ssl.SSLContext, 'load_verify_locations') as load, patch.object(tls.ssl, 'create_default_context', return_value=context):
            actual = tls.client_context()
        self.assertIs(actual, context)
        self.assertTrue(actual.check_hostname)
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        load.assert_called_once_with(cafile='/etc/ssl/cert.pem')

    def test_platform_and_explicit_trust_overrides_are_preserved(self):
        for platform, env, expected in [('darwin', {}, True), ('linux', {}, False), ('darwin', {'SSL_CERT_FILE': '/custom.pem'}, False), ('darwin', {'SSL_CERT_DIR': '/custom'}, False)]:
            with self.subTest(platform=platform, env=env):
                context = Mock()
                with patch.object(tls.sys, 'platform', platform), patch.dict(tls.os.environ, env, clear=True), patch.object(tls.Path, 'is_file', return_value=True), patch.object(tls.ssl, 'create_default_context', return_value=context):
                    self.assertIs(tls.client_context(), context)
                self.assertEqual(context.load_verify_locations.called, expected)

    def test_certificate_failure_keeps_safe_reason(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError('private-token'))
        with patch('pantheon_connectors.client.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(APIError) as caught:
                Client('https://pantheon.talk', 'private-token').request('/v1/onboarding')
        self.assertEqual(caught.exception.reason, 'tls_verification_failed')
        self.assertNotIn('private-token', str(caught.exception))

    def test_mcp_reports_tls_cause_without_blame_or_secrets(self):
        server = Mock()
        server.handle.side_effect = APIError(0, 'tls_verification_failed')
        output = io.StringIO()
        with patch('pantheon_connectors.codex_hosted.HostedServer', return_value=server), patch.object(codex_mcp.sys, 'stdin', io.StringIO('{"id":1,"method":"tools/call"}\n')), patch.object(codex_mcp.sys, 'stdout', output):
            codex_mcp.serve()
        error = json.loads(output.getvalue())['error']
        self.assertEqual(error['data']['reason'], 'tls_verification_failed')
        self.assertIn('certificate verification failed', error['message'])
        self.assertNotIn('scoped consent', error['message'])
        server.close.assert_called_once()
    def test_missing_state_response_names_tool_without_paths(self):
        server=Mock();server.handle.side_effect=FileNotFoundError(2,'private-token','/private/token')
        output=io.StringIO()
        request=json.dumps({'id':1,'method':'tools/call','params':{'name':'pantheon_join'}})+'\n'
        with patch('pantheon_connectors.codex_hosted.HostedServer',return_value=server),patch.object(codex_mcp.sys,'stdin',io.StringIO(request)),patch.object(codex_mcp.sys,'stdout',output):codex_mcp.serve()
        error=json.loads(output.getvalue())['error']
        self.assertEqual(error['data']['reason'],'local_file_missing')
        self.assertEqual(error['data']['tool'],'pantheon_join')
        self.assertNotIn('private-token',output.getvalue());self.assertNotIn('/private/token',output.getvalue())
