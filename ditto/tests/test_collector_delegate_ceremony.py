"""Secret-safe two-role ceremony controls; no personal or generated live keys."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[2] / "scripts/collector_delegate_key.py"
spec = importlib.util.spec_from_file_location("collector_delegate_key", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SENTINEL = " ".join(["secret-sentinel"] * 24)
ADDRESS = "5" + "A" * 47
FORBIDDEN = ["5" + c * 47 for c in "BCDEF"]


class FakeKeys:
    ss58_address = ADDRESS
    calls = 0

    @classmethod
    def generate_mnemonic(cls, *, n_words):
        assert n_words == 24
        cls.calls += 1
        return SENTINEL

    @classmethod
    def create_from_mnemonic(cls, mnemonic):
        assert mnemonic == SENTINEL
        return cls()


class FakeCloud:
    def __init__(self, role="registration"):
        self.role = role
        self.parent = f"projects/123456/secrets/sn118-collector-{role}-delegate"
        self.versions = []
        self.uploads = 0
        self.fail_upload = False

    def assert_empty(self):
        if self.versions:
            raise module.Refusal("existing-secret-version-no-rotation")

    def add(self, value):
        assert value == SENTINEL
        self.uploads += 1
        self.versions = [1]
        if self.fail_upload:
            raise RuntimeError(SENTINEL)

    def access(self):
        return SENTINEL


class CeremonyControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.keys = patch.object(module, "keypair_type", return_value=FakeKeys)
        self.keys.start()
        FakeKeys.calls = 0

    def tearDown(self):
        self.keys.stop()
        self.temp.cleanup()

    def test_both_roles_write_public_receipt_then_rederive_without_keys_on_disk(self):
        for role in module.ROLES:
            with self.subTest(role=role):
                root = self.root / role
                root.mkdir(mode=0o700)
                cloud = FakeCloud(role)
                result = module.ceremony(cloud, "generate", root, FORBIDDEN)
                self.assertEqual(result["address"], ADDRESS)
                self.assertEqual(result["status"], "stored")
                checked = module.ceremony(cloud, "verify", root, FORBIDDEN)
                self.assertEqual(checked["status"], "independently-rederived")
                self.assertEqual(cloud.uploads, 1)
                for f in root.iterdir():
                    self.assertNotIn(SENTINEL, f.read_text())
                    self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_upload_timeout_never_creates_another_key_or_attempt(self):
        cloud = FakeCloud()
        cloud.fail_upload = True
        with self.assertRaises(RuntimeError):
            module.ceremony(cloud, "generate", self.root, FORBIDDEN)
        self.assertEqual(module.read_receipt(self.root)["status"], "pending-upload")
        with self.assertRaisesRegex(module.Refusal, "existing-intent"):
            module.ceremony(cloud, "generate", self.root, FORBIDDEN)
        self.assertEqual(FakeKeys.calls, 1)
        self.assertEqual(cloud.uploads, 1)
        self.assertEqual(module.ceremony(cloud, "verify", self.root, FORBIDDEN)["address"], ADDRESS)

    def test_crash_before_generation_is_latched_even_with_empty_secret(self):
        cloud = FakeCloud()
        with patch.object(module, "keypair_type", side_effect=RuntimeError(SENTINEL)):
            with self.assertRaises(RuntimeError):
                module.ceremony(cloud, "generate", self.root, FORBIDDEN)
        self.assertEqual(module.read_receipt(self.root)["status"], "started")
        with self.assertRaisesRegex(module.Refusal, "existing-intent"):
            module.ceremony(cloud, "generate", self.root, FORBIDDEN)
        self.assertEqual(FakeKeys.calls, 0)

    def test_existing_version_including_disabled_or_destroyed_never_generates(self):
        for state in ("ENABLED", "DISABLED", "DESTROYED"):
            with self.subTest(state=state):
                cloud = FakeCloud()
                cloud.versions = [{"state": state}]
                with self.assertRaisesRegex(module.Refusal, "existing-secret-version"):
                    module.ceremony(cloud, "generate", self.root, FORBIDDEN)
                self.assertEqual(FakeKeys.calls, 0)

    def test_corrupt_receipt_or_pending_temp_cannot_be_discarded_for_retry(self):
        for filename in ("receipt.json", ".receipt.pending"):
            file = self.root / filename
            file.write_text("corrupt-public-record")
            with self.assertRaisesRegex(module.Refusal, "existing-intent"):
                module.ceremony(FakeCloud(), "generate", self.root, FORBIDDEN)
            file.unlink()

    def test_root_and_receipt_symlinks_refused(self):
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            module.ceremony(FakeCloud(), "generate", link, FORBIDDEN)
        link.unlink()
        (self.root / "receipt.json").symlink_to(self.root / "absent")
        with self.assertRaisesRegex(module.Refusal, "existing-intent"):
            module.ceremony(FakeCloud(), "generate", self.root, FORBIDDEN)

    def test_open_directory_or_lock_refused(self):
        self.root.chmod(0o750)
        with self.assertRaisesRegex(module.Refusal, "permissions"):
            module.ceremony(FakeCloud(), "generate", self.root, FORBIDDEN)
        self.root.chmod(0o700)
        lock = self.root / "ceremony.lock"
        lock.touch(mode=0o644)
        with self.assertRaisesRegex(module.Refusal, "permissions"):
            module.ceremony(FakeCloud(), "generate", self.root, FORBIDDEN)

    def test_live_lock_excludes_second_generator(self):
        fd = os.open(self.root / "ceremony.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            module.fcntl.flock(fd, module.fcntl.LOCK_EX | module.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                module.ceremony(FakeCloud(), "generate", self.root, FORBIDDEN)
        finally:
            os.close(fd)
        self.assertEqual(FakeKeys.calls, 0)

    def test_wrong_role_wrong_version_and_wrong_stored_key_refused(self):
        cloud = FakeCloud()
        module.ceremony(cloud, "generate", self.root, FORBIDDEN)
        for wrong in (FakeCloud("transfer"),):
            with self.assertRaisesRegex(module.Refusal, "receipt-binding"):
                module.ceremony(wrong, "verify", self.root, FORBIDDEN)
        with patch.object(cloud, "access", return_value="wrong key"):
            with self.assertRaises(Exception):
                module.ceremony(cloud, "verify", self.root, FORBIDDEN)
        self.assertEqual(cloud.uploads, 1)

    def test_offline_address_collision_stops_before_upload(self):
        with self.assertRaisesRegex(module.Refusal, "generated-address-invalid"):
            module.ceremony(FakeCloud(), "generate", self.root, [ADDRESS])
        self.assertEqual(module.read_receipt(self.root)["status"], "started")

    def test_main_redacts_transport_and_sdk_errors_and_has_no_secret_arguments(self):
        argv = [str(SCRIPT), "--project", "test-project", "--role", "transfer",
                "--mode", "generate", "--confirm", "GENERATE GCP COLLECTOR TRANSFER DELEGATE"]
        for address in FORBIDDEN:
            argv.extend(["--forbidden-address", address])
        out, err = io.StringIO(), io.StringIO()
        with patch.object(module.sys, "argv", argv), patch.object(module, "Cloud", side_effect=RuntimeError(SENTINEL)), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(module.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn(SENTINEL, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class WireControls(unittest.TestCase):
    def client(self):
        client = object.__new__(module.Cloud)
        client.parent = "projects/123456/secrets/sn118-collector-registration-delegate"
        return client

    def test_crc32c_known_vector(self):
        self.assertEqual(module.crc32c(b"123456789"), 0xE3069283)

    def test_upload_checks_exact_first_version_and_server_checksum(self):
        client = self.client()
        calls = []
        def call(suffix, body):
            calls.append((suffix, body))
            return {"name": client.parent + "/versions/1", "state": "ENABLED",
                    "clientSpecifiedPayloadChecksum": True}
        client.call = call
        client.add(SENTINEL)
        payload = calls[0][1]["payload"]
        self.assertEqual(base64.b64decode(payload["data"]), SENTINEL.encode())
        self.assertEqual(payload["dataCrc32c"], str(module.crc32c(SENTINEL.encode())))
        for result in ({}, {"name": client.parent + "/versions/2"},
                       {"name": client.parent + "/versions/1", "state": "ENABLED", "clientSpecifiedPayloadChecksum": False}):
            client.call = lambda *_args, value=result: value
            with self.assertRaisesRegex(module.Refusal, "uncertain"):
                client.add(SENTINEL)

    def test_read_checksum_and_exact_version_refused(self):
        client = self.client()
        response = {"name": client.parent + "/versions/1", "payload": {
            "data": base64.b64encode(SENTINEL.encode()).decode(),
            "dataCrc32c": str(module.crc32c(SENTINEL.encode()))}}
        client.call = lambda *_args: response
        self.assertEqual(client.access(), SENTINEL)
        response["payload"]["dataCrc32c"] = "0"
        with self.assertRaisesRegex(module.Refusal, "checksum"):
            client.access()
        response["name"] = client.parent + "/versions/latest"
        with self.assertRaisesRegex(module.Refusal, "version"):
            client.access()

    def test_metadata_wrong_host_principal_project_or_phase_refused(self):
        base = {"project/project-id": "test-project", "instance/name": "sn118-collector-registration-signer",
                "instance/service-accounts/default/email": "sn118-collector-registration@test-project.iam.gserviceaccount.com",
                "instance/tags": '["collector-registration-armed"]',
                "project/numeric-project-id": "123456", "instance/service-accounts/default/token": '{"access_token":"TEST"}'}
        with patch.object(module.Cloud, "metadata", side_effect=lambda s: base[s]):
            self.assertEqual(module.Cloud("test-project", "registration", "generate").parent, self.client().parent)
        for key, wrong in (("project/project-id", "other-project"), ("instance/name", "other-host"),
                           ("instance/service-accounts/default/email", "other-principal"),
                           ("instance/tags", '["collector-registration-bootstrap"]'),
                           ("instance/tags", '["collector-registration-armed","collector-transfer-bootstrap"]')):
            changed = {**base, key: wrong}
            with patch.object(module.Cloud, "metadata", side_effect=lambda s: changed[s]), self.assertRaises(module.Refusal):
                module.Cloud("test-project", "registration", "generate")

    def test_redirects_refused_and_arbitrary_api_paths_refused(self):
        with self.assertRaises(module.Refusal):
            module.NoRedirect().redirect_request(None)
        with self.assertRaisesRegex(module.Refusal, "api-method"):
            self.client().call("/versions/latest:access")


if __name__ == "__main__":
    unittest.main()
