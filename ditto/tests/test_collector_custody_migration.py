import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bittensor_wallet import Keypair

from ditto.treasury import collector_migration as m
from ditto.treasury.collector import CollectorJournal, CollectorPolicy
from ditto.treasury.service_allocation import ServiceDestination


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.key = Keypair.create_from_seed("0x" + "11" * 32)
        self.old = CollectorPolicy(
            genesis_hash="0x" + "01" * 32,
            runtime_code_hash="0x" + "02" * 32,
            collector_coldkey=self.key.ss58_address,
            collector_hotkey="hot",
            registration_delegate="old-reg",
            transfer_delegate="old-xfer",
            revision=3,
            start_block=100,
            destinations=(ServiceDestination("gm", 1000, "procurement"),),
            max_registration_burn_rao=1,
            registration_budget_rao=10,
            max_fee_rao=2,
            fee_reserve_rao=5,
            recovery_cooldown_blocks=300,
            distribution_interval_blocks=300,
            max_distribution_rao=100,
            gcp_project="ditto-app-dev",
            registration_service_account="sn118-collector-registration@ditto-app-dev.iam.gserviceaccount.com",
            transfer_service_account="sn118-collector-transfer@ditto-app-dev.iam.gserviceaccount.com",
            registration_secret_version=1,
            transfer_secret_version=1,
            enabled=True,
        )
        self.new = replace(
            self.old,
            revision=4,
            gcp_project="sn118-gamma-custody",
            registration_delegate="new-reg",
            transfer_delegate="new-xfer",
            registration_service_account="sn118-collector-registration@sn118-gamma-custody.iam.gserviceaccount.com",
            transfer_service_account="sn118-collector-transfer@sn118-gamma-custody.iam.gserviceaccount.com",
        )
        self.source = self.root / "snapshot.db"
        journal = CollectorJournal(
            self.source, self.old, "registration", initialize=True
        )
        journal.db.execute(
            "INSERT INTO operations VALUES "
            "(1,'finalized','registration',3,NULL,NULL,NULL,'{}','{}',101,?,101)",
            (json.dumps({"status": "finalized", "block": 101}),),
        )
        journal.event("settlement", {"operation": 1})
        journal.db.execute("UPDATE cursor SET block=199")
        journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        journal.db.execute("PRAGMA journal_mode=DELETE")
        journal.close()
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.approval = m.manifest(
            self.source, self.old, self.new, "registration", self.sha
        )

    def tearDown(self):
        self.tmp.cleanup()

    def signed(self, value):
        digest = hashlib.sha256(m.canonical(value).encode()).hexdigest()
        return self.key.sign(
            f"ditto-collector-custody-migration-v1:{digest}".encode()
        ).hex()

    def test_finalized_spend_cursor_all_history_survive(self):
        target = self.root / "target.db"
        result = m.migrate(
            self.source,
            target,
            self.old,
            self.new,
            self.approval,
            self.signed(self.approval),
        )
        self.assertEqual(result["registration_reserved_rao"], 3)
        self.assertEqual(result["cursor"], 199)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sha)
        j = CollectorJournal(target, self.new, "registration")
        self.assertEqual(
            j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0], 3
        )
        self.assertEqual(j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        j.close()
        with self.assertRaises(FileExistsError):
            m.migrate(
                self.source,
                target,
                self.old,
                self.new,
                self.approval,
                self.signed(self.approval),
            )

    def test_runtime_reapproval_preserves_already_migrated_custody_and_spend(self):
        from ditto_screening_protocol.collector_receipts import (
            AUDITED_COLLECTOR_CODE_HASH,
            HISTORICAL_COLLECTOR_CODE_HASH,
        )

        old = replace(self.old, runtime_code_hash=HISTORICAL_COLLECTOR_CODE_HASH)
        isolated = replace(self.new, runtime_code_hash=HISTORICAL_COLLECTOR_CODE_HASH)
        # This fixture was pinned before custody migration; bind the revised fixture.
        db = sqlite3.connect(self.source)
        db.execute("UPDATE pin SET digest=?", (old.digest,))
        db.commit()
        db.close()
        sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        first = m.manifest(self.source, old, isolated, "registration", sha)
        current = self.root / "policy4.db"
        m.migrate(self.source, current, old, isolated, first, self.signed(first))
        new = replace(
            isolated, revision=5, runtime_code_hash=AUDITED_COLLECTOR_CODE_HASH
        )
        current_sha = hashlib.sha256(current.read_bytes()).hexdigest()
        approval = m.manifest(current, isolated, new, "registration", current_sha)
        target = self.root / "policy5.db"
        with self.assertRaises(ValueError):
            m.migrate(current, target, isolated, new, approval, self.signed(first))
        self.assertFalse(target.exists())
        result = m.migrate(
            current, target, isolated, new, approval, self.signed(approval)
        )
        self.assertEqual(result["registration_reserved_rao"], 3)
        self.assertEqual(result["cursor"], 199)
        self.assertEqual(hashlib.sha256(current.read_bytes()).hexdigest(), current_sha)
        j = CollectorJournal(target, new, "registration")
        self.assertEqual(
            j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0], 3
        )
        self.assertEqual(j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)
        j.close()
        for changed in (
            replace(new, registration_budget_rao=11),
            replace(new, start_block=101),
            replace(new, transfer_delegate="other"),
            replace(new, revision=6),
            replace(new, runtime_code_hash="0x" + "ab" * 32),
        ):
            with self.assertRaises(ValueError):
                m.policy_transition(isolated, changed)
        with self.assertRaises(ValueError):
            m.policy_transition(new, replace(isolated, revision=6))

    def test_unresolved_unknown_or_settlement_mismatch_refuse(self):
        for state in ("dispatching", "unknown", "expired"):
            db = sqlite3.connect(self.source)
            db.execute("UPDATE operations SET state=?", (state,))
            db.commit()
            db.close()
            sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                m.manifest(self.source, self.old, self.new, "registration", sha)

    def test_caps_identity_history_hash_and_role_cannot_change(self):
        for policy in (
            replace(self.new, start_block=101),
            replace(self.new, registration_budget_rao=11),
            replace(self.new, collector_hotkey="other"),
            replace(self.new, revision=5),
            replace(self.new, registration_delegate="old-reg"),
        ):
            with self.assertRaises(ValueError):
                m.manifest(self.source, self.old, policy, "registration", self.sha)
        for field, value in (
            ("history_sha256", "0" * 64),
            ("cursor", 0),
            ("role", "transfer"),
            ("registration_reserved_rao", 0),
        ):
            approval = {**self.approval, field: value}
            with self.assertRaises(ValueError):
                m.migrate(
                    self.source,
                    self.root / "target.db",
                    self.old,
                    self.new,
                    approval,
                    self.signed(approval),
                )
            self.assertFalse((self.root / "target.db").exists())

    def test_bad_signature_cannot_create_target(self):
        target = self.root / "target.db"
        with self.assertRaises(ValueError):
            m.migrate(self.source, target, self.old, self.new, self.approval, "00" * 64)
        self.assertFalse(target.exists())
        signature = self.signed(self.approval)
        changed = {**self.approval, "cursor": 100}
        with self.assertRaises(ValueError):
            m.migrate(self.source, target, self.old, self.new, changed, signature)
        self.assertFalse(target.exists())

    def test_schema_injection_refused_before_output(self):
        db = sqlite3.connect(self.source)
        db.execute(
            "CREATE TRIGGER poison AFTER UPDATE ON pin "
            "BEGIN DELETE FROM operations; END;"
        )
        db.commit()
        db.close()
        sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            m.manifest(self.source, self.old, self.new, "registration", sha)

    def test_malformed_approval_is_bounded_refusal(self):
        target = self.root / "target.db"
        for signature in (None, 3, "abc", "zz" * 64):
            with self.assertRaises(ValueError):
                m.migrate(
                    self.source, target, self.old, self.new, self.approval, signature
                )
        for field in ("schema", "role", "source_sha256"):
            approval = {k: v for k, v in self.approval.items() if k != field}
            with self.assertRaises(ValueError):
                m.migrate(
                    self.source,
                    target,
                    self.old,
                    self.new,
                    approval,
                    self.signed(approval),
                )
        self.assertFalse(target.exists())

    def test_file_sync_failure_does_not_publish_partial_target(self):
        target = self.root / "target.db"
        signature = self.signed(self.approval)
        with (
            patch.object(m.os, "fsync", side_effect=OSError("fixture sync failure")),
            self.assertRaises(OSError),
        ):
            m.migrate(self.source, target, self.old, self.new, self.approval, signature)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".custody-migration-*")), [])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sha)
        m.migrate(self.source, target, self.old, self.new, self.approval, signature)
        self.assertTrue(target.exists())

    def test_postpublication_sync_failure_preserves_complete_output(self):
        target = self.root / "target.db"
        signature = self.signed(self.approval)
        with (
            patch.object(
                m.os, "fsync", side_effect=[None, OSError("directory sync uncertain")]
            ),
            self.assertRaises(OSError),
        ):
            m.migrate(self.source, target, self.old, self.new, self.approval, signature)
        db = sqlite3.connect(target)
        self.assertEqual(
            db.execute("SELECT digest FROM pin").fetchone()[0], self.new.digest
        )
        self.assertEqual(
            db.execute("SELECT SUM(amount) FROM operations").fetchone()[0], 3
        )
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        db.close()
        with self.assertRaises(FileExistsError):
            m.migrate(self.source, target, self.old, self.new, self.approval, signature)

    def test_symlinks_permissions_sidecars_and_hash_refused(self):
        link = self.root / "link.db"
        link.symlink_to(self.source)
        with self.assertRaises(OSError):
            m.manifest(link, self.old, self.new, "registration", self.sha)
        self.source.chmod(0o644)
        with self.assertRaises(ValueError):
            m.manifest(self.source, self.old, self.new, "registration", self.sha)
        self.source.chmod(0o600)
        with self.assertRaises(ValueError):
            m.manifest(self.source, self.old, self.new, "registration", "0" * 64)
        Path(str(self.source) + "-wal").write_bytes(b"")
        with self.assertRaises(ValueError):
            m.manifest(self.source, self.old, self.new, "registration", self.sha)


if __name__ == "__main__":
    unittest.main()
