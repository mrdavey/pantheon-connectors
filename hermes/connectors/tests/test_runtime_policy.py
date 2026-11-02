import unittest
from pantheon_connectors.runtime_policy import allowed

class RuntimePolicyTests(unittest.TestCase):
    def test_connection_has_no_elapsed_time_cap_but_requires_explicit_authority(self):
        config=dict(allow_runtime_turns=True, connection_runtime_supported=True,
                    runtime_policy={'mode':'connection'}, runtime_started_at=100, trial_expires_at=0)
        self.assertTrue(allowed(config, now=1000000))
        for change in [dict(allow_runtime_turns=False),dict(connection_runtime_supported=False),dict(runtime_started_at=0),dict(runtime_started_at=float('nan')),dict(runtime_policy={'mode':'unknown'}),dict(runtime_policy={'mode':'connection','extra':True}),dict(trial_expires_at=1000001)]:
            self.assertFalse(allowed({**config,**change},now=1000000),change)
    def test_legacy_finite_expiry_and_ceiling_unchanged(self):
        self.assertTrue(allowed(dict(allow_runtime_turns=True,trial_expires_at=200),now=100))
        for expiry in (0,99,100,3701,float('inf')):
            self.assertFalse(allowed(dict(allow_runtime_turns=True,trial_expires_at=expiry),now=100))
