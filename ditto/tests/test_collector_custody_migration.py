import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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
