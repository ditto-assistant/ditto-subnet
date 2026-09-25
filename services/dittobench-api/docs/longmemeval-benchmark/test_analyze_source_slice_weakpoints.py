import hashlib
import json
import unittest
from unittest.mock import patch

import analyze_source_slice_weakpoints as audit


class AuditTests(unittest.TestCase):
    def test_rejects_unpinned_input(self):
        with patch.object(audit.Path, "read_bytes", return_value=b"{}"):
            with self.assertRaisesRegex(ValueError, "hash differs"):
                audit.analyze("unused")

    def test_rejects_missing_cases_even_with_matching_hash(self):
        raw = json.dumps({"control": {"per_case": []},
                          "treatment": {"per_case": []}}).encode()
        with patch.object(audit.Path, "read_bytes", return_value=raw):
            with patch.object(audit, "EXPECTED_SHA256", hashlib.sha256(raw).hexdigest()):
                with self.assertRaisesRegex(ValueError, "500 matched"):
                    audit.analyze("unused")


if __name__ == "__main__":
    unittest.main()
