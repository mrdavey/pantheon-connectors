import unittest
from pantheon_connectors.cli import inspection_browser_url
from pantheon_connectors.worker import Blocked

class InspectionBrowserLinkTests(unittest.TestCase):
    def test_capability_stays_in_fragment(self):
        self.assertEqual(inspection_browser_url('https://pantheon.example/', 'test-capability'),
                         'https://pantheon.example/app/invite#inspection=test-capability')

    def test_rejects_untrusted_origin_shapes(self):
        for origin in ['http://pantheon.example', 'https://user:pass@pantheon.example',
                       'https://pantheon.example/path', 'https://pantheon.example?track=x',
                       'https://pantheon.example#x', '//pantheon.example', 'javascript:alert(1)']:
            with self.subTest(origin=origin), self.assertRaises(Blocked):
                inspection_browser_url(origin, 'test-capability')
