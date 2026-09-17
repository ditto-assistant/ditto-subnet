import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import capture_backend_snapshot as snapshot


class SnapshotTests(unittest.TestCase):
    def entries(self):
        return [{"table": name, "sha256": "a" * 64, "rows": 3} for name in snapshot.TABLES]

    def test_manifest_exact_scope_and_duplicates(self):
        manifest = {"cases": [{"question_id": q, "fixture_user": "lme_s_" + q} for q in ["b_abs", "a"]]}
        self.assertEqual(snapshot.manifest_users(manifest, 2), ["lme_s_a", "lme_s_b_abs"])
        manifest["cases"][0]["fixture_user"] = "lme_s_a"
        with self.assertRaises(ValueError):
            snapshot.manifest_users(manifest, 2)
        with self.assertRaises(ValueError):
            snapshot.manifest_users({"cases": [manifest["cases"][1]] * 2}, 2)

    def test_sql_is_one_readonly_transaction_fixed_tables(self):
        sql = snapshot.snapshot_sql(["lme_s_a"])
        self.assertTrue(sql.startswith("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"))
        self.assertIn("SET LOCAL TIME ZONE 'UTC';", sql)
        self.assertEqual(sql.count("WITH rows AS MATERIALIZED"), 10)
        self.assertEqual(sql.count("string_agg(hash, '' ORDER BY hash)"), 10)
        self.assertIn("FROM users t WHERE uid = ANY(", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))
        with self.assertRaises(ValueError):
            snapshot.snapshot_sql(["lme_s_a';DELETE FROM users;--"])

    def test_go_struct_field_order_and_empty_hash(self):
        result = snapshot.build_snapshot(["lme_s_a"], self.entries())
        fields = ','.join('{"table":"' + t + '","sha256":"' + "a" * 64 + '","rows":3}' for t in snapshot.TABLES)
        expected_bytes = ('{"version":"postgres-jsonb-row-multiset-sha256-v1","sha256":"","users":["lme_s_a"],"tables":[' + fields + ']}').encode()
        self.assertEqual(result["sha256"], hashlib.sha256(expected_bytes).hexdigest())
        snapshot.compare_snapshot(result, {"meta": {"lme_prepared_snapshot": json.dumps(result)}})
        with self.assertRaises(ValueError):
            snapshot.compare_snapshot(result, {**result, "sha256": "b" * 64})

    def test_invalid_table_evidence(self):
        for entries in [self.entries()[:-1], list(reversed(self.entries())), [{**e, "rows": True} for e in self.entries()]]:
            with self.assertRaises(ValueError):
                snapshot.build_snapshot(["lme_s_a"], entries)
        with self.assertRaises(ValueError):
            json.loads('{"cases":[],"cases":[]}', object_pairs_hook=snapshot.unique_object)

    def test_docker_psql_contract_and_fail_closed(self):
        completed = subprocess.CompletedProcess([], 0, '\n'.join(json.dumps(e) for e in self.entries()), '')
        with patch.object(snapshot.subprocess, "run", return_value=completed) as run:
            actual = snapshot.capture("ditto-postgres-lme-test", "ditto_lme_s_final", ["lme_s_a"])
            self.assertEqual(actual, snapshot.build_snapshot(["lme_s_a"], self.entries()))
            command = run.call_args.args[0]
            self.assertIn("ON_ERROR_STOP=1", command)
            self.assertIn("-X", command)
            self.assertTrue(run.call_args.kwargs["check"])
        with patch.object(snapshot.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                snapshot.capture("ditto-postgres", "postgres", ["lme_s_a"])
            run.assert_not_called()

    def test_output_exclusive_private(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            snapshot.write_new(output, {"sha256": "a" * 64})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                snapshot.write_new(output, {})


if __name__ == "__main__":
    unittest.main()
