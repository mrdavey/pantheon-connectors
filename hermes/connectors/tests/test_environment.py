import unittest
from pantheon_connectors.environment import get


class EnvironmentCompatibility(unittest.TestCase):
    def test_alias_precedence_and_conflict_redaction(self):
        for values, expected in (
            ({}, None),
            ({"PANTHEON_HTTP_TEST_URL": "new-origin"}, "new-origin"),
            ({"PYROS_HTTP_TEST_URL": "old-origin"}, "old-origin"),
            ({"PANTHEON_HTTP_TEST_URL": "same", "PYROS_HTTP_TEST_URL": "same"}, "same"),
        ):
            with self.subTest(values=values):
                self.assertEqual(get("HTTP_TEST_URL", environ=values), expected)
        for current in ("private-new-value", ""):
            with self.assertRaises(ValueError) as error:
                get("HTTP_TEST_URL", environ={"PANTHEON_HTTP_TEST_URL": current,
                                              "PYROS_HTTP_TEST_URL": "private-old-value"})
            self.assertNotIn("private-", str(error.exception))
        self.assertEqual(get("HTTP_TEST_URL", "fallback", environ={}), "fallback")
