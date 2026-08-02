from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("submission-lab.py")
README = SCRIPT.parent.parent / "README.md"
SPEC = importlib.util.spec_from_file_location("submission_lab", SCRIPT)
assert SPEC and SPEC.loader
LAB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LAB
SPEC.loader.exec_module(LAB)


def digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def passport(
    path: Path,
    *,
    tamper: bool = False,
    omit_signed_memory: bool = False,
    bad_counts: bool = False,
    bad_link: bool = False,
    blank_memory: bool = False,
) -> str:
    files = {
        "memories.jsonl": (
            b'{"id":"m1","timestamp":"2026-01-01T00:00:00Z","prompt":"","response":"","session_id":"s"}\n'
            if blank_memory
            else b'{"id":"m1","timestamp":"2026-01-01T00:00:00Z","prompt":"What should we call my blue kayak?","response":"Let us call it Glacier Finch.","session_id":"s"}\n'
        ),
        "subjects.json": b'[{"id":"sub1","subject_text":"Blue kayak","description":"The user named their blue kayak Glacier Finch."}]',
        "subject_links.json": (
            b'[{"subject_id":"missing","pair_id":"m1"}]'
            if bad_link
            else b'[{"subject_id":"sub1","pair_id":"m1"}]'
        ),
        "README.md": b"test",
    }
    with tempfile.TemporaryDirectory() as key_directory:
        key_root = Path(key_directory)
        private_path = key_root / "private.pem"
        public_path = key_root / "public.der"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(private_path),
                "-pubout",
                "-outform",
                "DER",
                "-out",
                str(public_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public_key = public_path.read_bytes()[-32:]
        manifest = {
            "issuedAt": "2026-01-02T00:00:00Z",
            "subject": {"userId": "user-1", "issuer": LAB.PASSPORT_ISSUER},
            "@context": LAB.PASSPORT_CONTEXT,
            "@type": LAB.PASSPORT_TYPE,
            "version": LAB.PASSPORT_VERSION,
            "issuer": LAB.PASSPORT_ISSUER,
            "algorithm": LAB.PASSPORT_ALGORITHM,
            "kid": "kid-1",
            "publicKey": base64.urlsafe_b64encode(public_key).rstrip(b"=").decode(),
            "source": "ditto",
            "files": [
                {"path": name, "size": len(data), "sha256": digest(data)}
                for name, data in files.items()
                if not (omit_signed_memory and name == "memories.jsonl")
            ],
            "counts": {
                "memories": 2 if bad_counts else 1,
                "subjects": 1,
                "subject_links": 1,
                "sessions": 1,
            },
        }
        manifest_raw = json.dumps(manifest, separators=(",", ":")).encode()
        manifest_path = key_root / "manifest.json"
        signature_path = key_root / "signature.bin"
        manifest_path.write_bytes(manifest_raw)
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(private_path),
                "-rawin",
                "-in",
                str(manifest_path),
                "-out",
                str(signature_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        signature = (
            base64.urlsafe_b64encode(signature_path.read_bytes()).rstrip(b"=").decode()
        )
        signed_raw = (
            b'{"signature":'
            + json.dumps(signature).encode()
            + b',"kid":"kid-1","algorithm":"ed25519","manifest":'
            + manifest_raw
            + b"}"
        )
        if tamper:
            files["memories.jsonl"] += b"tampered"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", manifest_raw)
            archive.writestr("signature.json", signed_raw)
            for name, data in files.items():
                archive.writestr(name, data)
        return base64.urlsafe_b64encode(public_key).rstrip(b"=").decode()


def synthetic_passport(
    pairs: list[dict[str, object]],
    subjects: list[dict[str, object]] | None = None,
    links: list[dict[str, object]] | None = None,
) -> LAB.PassportData:
    subjects = subjects or []
    links = links or []
    return LAB.PassportData(
        user_id="isolated-user",
        origin_user_id="origin-user",
        issuer=LAB.PASSPORT_ISSUER,
        kid="kid-1",
        public_key="unused",
        seed_payload={
            "user_id": "isolated-user",
            "wave": 0,
            "pairs": pairs,
            "subjects": subjects,
            "links": links,
        },
        counts={
            "memories": len(pairs),
            "subjects": len(subjects),
            "links": len(links),
        },
    )


class SubmissionLabTest(unittest.TestCase):
    def test_passport_converts_to_seed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path)
            loaded = LAB.load_passport(path)
            self.assertEqual(loaded.user_id, "user-1")
            self.assertEqual(loaded.counts, {"memories": 1, "subjects": 1, "links": 1})
            self.assertEqual(loaded.seed_payload["pairs"][0]["pair_id"], "m1")
            self.assertEqual(
                loaded.seed_payload["subjects"][0]["description_text"],
                "The user named their blue kayak Glacier Finch.",
            )

    def test_passport_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path, tamper=True)
            with self.assertRaisesRegex(
                LAB.LabError, "manifest (size|digest) mismatch"
            ):
                LAB.load_passport(path)

    def test_passport_rejects_unsigned_required_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path, omit_signed_memory=True)
            with self.assertRaisesRegex(LAB.LabError, "not covered by the signature"):
                LAB.load_passport(path)

    def test_passport_rejects_manifest_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path, bad_counts=True)
            with self.assertRaisesRegex(LAB.LabError, "manifest count mismatch"):
                LAB.load_passport(path)

    def test_submission_rejects_path_traversal(self) -> None:
        for unsafe_path in ("../escape", "./../escape", "/escape"):
            with self.subTest(path=unsafe_path):
                with tempfile.TemporaryDirectory() as directory:
                    archive_path = Path(directory) / "submission.tar.gz"
                    with tarfile.open(archive_path, "w:gz") as archive:
                        info = tarfile.TarInfo(unsafe_path)
                        info.size = 1
                        archive.addfile(info, io.BytesIO(b"x"))
                    with self.assertRaisesRegex(LAB.LabError, "unsafe archive path"):
                        LAB.safe_extract_submission(
                            archive_path, Path(directory) / "out"
                        )

    def test_submission_accepts_standard_dot_root_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                root = tarfile.TarInfo(".")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                for name, content in (
                    ("./Cargo.toml", b"[package]\nname='submission'\n"),
                    ("./src/main.rs", b"fn main() {}\n"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            source = LAB.safe_extract_submission(archive_path, Path(directory) / "out")
            self.assertEqual(
                (source / "Cargo.toml").read_text(),
                "[package]\nname='submission'\n",
            )
            self.assertEqual((source / "src/main.rs").read_text(), "fn main() {}\n")

    def test_submission_accepts_supported_tar_compressions(self) -> None:
        for mode, suffix in (("w", ".tar"), ("w:bz2", ".tar.bz2"), ("w:xz", ".tar.xz")):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    archive_path = Path(directory) / ("submission" + suffix)
                    content = b"[package]\nname='submission'\n"
                    with tarfile.open(archive_path, mode) as archive:
                        info = tarfile.TarInfo("Cargo.toml")
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                    source = LAB.safe_extract_submission(
                        archive_path, Path(directory) / "out"
                    )
                    self.assertEqual((source / "Cargo.toml").read_bytes(), content)

    def test_submission_resolves_single_wrapped_cargo_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name, content in (
                    ("harness/Cargo.toml", b"[package]\nname='wrapped'\n"),
                    ("harness/src/main.rs", b"fn main() {}\n"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            source = LAB.safe_extract_submission(archive_path, Path(directory) / "out")
            self.assertEqual(source.name, "harness")
            self.assertTrue((source / "Cargo.toml").is_file())

    def test_submission_rejects_ambiguous_or_missing_cargo_root(self) -> None:
        for paths in (
            ("README.md",),
            ("one/Cargo.toml", "two/README.md"),
        ):
            with self.subTest(paths=paths):
                with tempfile.TemporaryDirectory() as directory:
                    archive_path = Path(directory) / "submission.tar.gz"
                    with tarfile.open(archive_path, "w:gz") as archive:
                        for name in paths:
                            content = b"content"
                            info = tarfile.TarInfo(name)
                            info.size = len(content)
                            archive.addfile(info, io.BytesIO(content))
                    with self.assertRaisesRegex(LAB.LabError, "no Cargo.toml"):
                        LAB.safe_extract_submission(
                            archive_path, Path(directory) / "out"
                        )

    def test_custom_command_uses_extraction_root_without_cargo_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                content = b"print('ok')\n"
                info = tarfile.TarInfo("wrapped/main.py")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            source = LAB.safe_extract_submission(
                archive_path,
                Path(directory) / "out",
                require_cargo_root=False,
            )
            self.assertEqual(source.name, "source")
            self.assertTrue((source / "wrapped/main.py").is_file())

    def test_submission_rejects_duplicate_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name, content in (
                    ("same.txt", b"first"),
                    ("./same.txt", b"second"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            with self.assertRaisesRegex(LAB.LabError, "duplicate archive path"):
                LAB.safe_extract_submission(archive_path, Path(directory) / "out")

    def test_submission_streaming_guard_rejects_high_ratio_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            content = b"x" * 4096
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("large.txt")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            self.assertLess(archive_path.stat().st_size, len(content))
            previous_limit = LAB.MAX_EXPANDED_BYTES
            LAB.MAX_EXPANDED_BYTES = 1024
            try:
                with self.assertRaisesRegex(
                    LAB.LabError, "decompressed tar stream exceeds"
                ):
                    LAB.safe_extract_submission(archive_path, Path(directory) / "out")
            finally:
                LAB.MAX_EXPANDED_BYTES = previous_limit
            self.assertFalse((Path(directory) / "out").exists())

    def test_submission_pax_metadata_counts_toward_stream_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            with tarfile.open(
                archive_path,
                "w:gz",
                format=tarfile.PAX_FORMAT,
                pax_headers={"comment": "x" * 8192},
            ) as archive:
                content = b"[package]\nname='submission'\n"
                info = tarfile.TarInfo("Cargo.toml")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            previous_limit = LAB.MAX_EXPANDED_BYTES
            LAB.MAX_EXPANDED_BYTES = 1024
            try:
                with self.assertRaisesRegex(
                    LAB.LabError, "decompressed tar stream exceeds"
                ):
                    LAB.safe_extract_submission(archive_path, Path(directory) / "out")
            finally:
                LAB.MAX_EXPANDED_BYTES = previous_limit
            self.assertFalse((Path(directory) / "out").exists())

    def test_submission_rejects_file_directory_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "submission.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                parent = tarfile.TarInfo("src")
                parent.size = 1
                archive.addfile(parent, io.BytesIO(b"x"))
                child = tarfile.TarInfo("src/main.rs")
                child.size = 1
                archive.addfile(child, io.BytesIO(b"y"))
            with self.assertRaisesRegex(LAB.LabError, "file/directory path collision"):
                LAB.safe_extract_submission(archive_path, Path(directory) / "out")

    def test_submission_digest_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.tar.gz"
            path.write_bytes(b"artifact")
            expected = hashlib.sha256(b"artifact").hexdigest()
            self.assertEqual(LAB.verify_submission_digest(path, expected), expected)
            with self.assertRaisesRegex(LAB.LabError, "SHA-256 mismatch"):
                LAB.verify_submission_digest(path, "0" * 64)

    def test_directory_rejects_submission_digest_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LAB.LabError, "cannot pin a mutable"):
                LAB.validate_submission_digest_options(
                    Path(directory), "0" * 64, allow_unverified=False
                )

    def test_passport_rejects_broken_subject_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path, bad_link=True)
            with self.assertRaisesRegex(LAB.LabError, "missing subject"):
                LAB.load_passport(path)

    def test_passport_omits_empty_memories_from_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path, blank_memory=True)
            loaded = LAB.load_passport(path)
            self.assertEqual(loaded.seed_payload["pairs"], [])
            self.assertEqual(loaded.seed_payload["links"], [])
            self.assertEqual(loaded.counts["memories"], 0)

    def test_agent_url_is_local_by_default_and_accepts_run_path(self) -> None:
        self.assertEqual(
            LAB.normalize_agent_url("http://127.0.0.1:8080/run"),
            "http://127.0.0.1:8080",
        )
        with self.assertRaisesRegex(LAB.LabError, "non-loopback"):
            LAB.normalize_agent_url("https://example.com/run")

    def test_remote_agent_requires_https_even_when_allowed(self) -> None:
        with self.assertRaisesRegex(LAB.LabError, "must use HTTPS"):
            LAB.normalize_agent_url("http://example.com/run", allow_remote=True)
        self.assertEqual(
            LAB.normalize_agent_url("https://example.com/run", allow_remote=True),
            "https://example.com",
        )

    def test_verification_base_url_requires_safe_origin(self) -> None:
        self.assertEqual(
            LAB.normalize_verification_base_url("https://api.heyditto.ai/"),
            "https://api.heyditto.ai",
        )
        self.assertEqual(
            LAB.normalize_verification_base_url("http://127.0.0.1:3400"),
            "http://127.0.0.1:3400",
        )
        with self.assertRaisesRegex(LAB.LabError, "must use HTTPS"):
            LAB.normalize_verification_base_url("http://api.example.com")
        with self.assertRaisesRegex(LAB.LabError, "must not contain a path"):
            LAB.normalize_verification_base_url("https://api.example.com/v1")

    def test_seed_waves_keep_linked_subjects_with_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path)
            loaded = LAB.load_passport(path, "isolated-user")
            waves = LAB.seed_waves(loaded, 1)
            self.assertEqual(len(waves), 1)
            self.assertEqual(waves[0]["user_id"], "isolated-user")
            self.assertEqual(waves[0]["subjects"][0]["id"], "sub1")
            self.assertEqual(waves[0]["links"][0]["pair_id"], "m1")

    def test_bounded_sample_preserves_order_and_filters_graph(self) -> None:
        pairs = [
            {
                "pair_id": f"p{index}",
                "session_id": "",
                "timestamp": "2026-01-01T00:00:00Z",
                "prompt": f"prompt {index}",
                "response": f"response {index}",
            }
            for index in range(3)
        ]
        subjects = [
            {
                "id": subject_id,
                "subject_text": subject_id,
                "description_text": "",
            }
            for subject_id in ("s0", "s1", "s2", "unlinked")
        ]
        links = [
            {"subject_id": f"s{index}", "pair_id": f"p{index}"} for index in range(3)
        ]
        loaded = synthetic_passport(pairs, subjects, links)
        bounded = LAB.limit_passport_pairs(loaded, 2)
        self.assertEqual(
            [pair["pair_id"] for pair in bounded.seed_payload["pairs"]],
            ["p0", "p1"],
        )
        self.assertEqual(
            [subject["id"] for subject in bounded.seed_payload["subjects"]],
            ["s0", "s1"],
        )
        self.assertEqual(
            [link["pair_id"] for link in bounded.seed_payload["links"]],
            ["p0", "p1"],
        )
        self.assertIs(LAB.limit_passport_pairs(loaded, None), loaded)

    def test_complete_validation_and_authority_precede_sampling(self) -> None:
        loaded = synthetic_passport([])
        events: list[str] = []

        def load(*args: object, **kwargs: object) -> LAB.PassportData:
            events.append("manifest")
            return loaded

        def verify(*args: object, **kwargs: object) -> str:
            events.append("authority")
            return "active"

        def limit_pairs(
            value: LAB.PassportData, max_pairs: int | None
        ) -> LAB.PassportData:
            events.append("sample")
            self.assertIs(value, loaded)
            self.assertEqual(max_pairs, 1)
            return value

        with (
            mock.patch.object(LAB, "load_passport", side_effect=load),
            mock.patch.object(LAB, "verify_passport_authority", side_effect=verify),
            mock.patch.object(LAB, "limit_passport_pairs", side_effect=limit_pairs),
        ):
            scoped, status = LAB.load_verified_passport(
                Path("synthetic.zip"),
                "isolated",
                verification_key=None,
                trust_embedded_key=False,
                verification_base_url=LAB.PASSPORT_VERIFICATION_BASE_URL,
                max_pairs=1,
            )
        self.assertIs(scoped, loaded)
        self.assertEqual(status, "active")
        self.assertEqual(events, ["manifest", "authority", "sample"])

    def test_max_pairs_must_be_positive(self) -> None:
        self.assertEqual(LAB.validate_max_pairs(None), None)
        self.assertEqual(LAB.validate_max_pairs(1), 1)
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(LAB.LabError, "must be positive"):
                    LAB.validate_max_pairs(value)

    def test_seed_waves_split_on_serialized_byte_budget(self) -> None:
        pairs = [
            {
                "pair_id": f"p{index}",
                "session_id": "",
                "timestamp": "2026-01-01T00:00:00Z",
                "prompt": "x" * 180,
                "response": "y" * 180,
            }
            for index in range(2)
        ]
        subjects = [
            {
                "id": f"s{index}",
                "subject_text": f"Subject {index}",
                "description_text": "linked",
            }
            for index in range(2)
        ]
        links = [
            {"subject_id": f"s{index}", "pair_id": f"p{index}"} for index in range(2)
        ]
        loaded = synthetic_passport(pairs, subjects, links)
        one_pair_budget = LAB._serialized_request_size(
            {
                "user_id": loaded.user_id,
                "wave": 0,
                "pairs": [pairs[0]],
                "subjects": [subjects[0]],
                "links": [links[0]],
            }
        )
        waves = LAB.seed_waves(
            loaded,
            batch_size=10,
            max_request_bytes=one_pair_budget,
        )
        self.assertEqual(len(waves), 2)
        for index, wave in enumerate(waves):
            self.assertLessEqual(LAB._serialized_request_size(wave), one_pair_budget)
            self.assertEqual(wave["pairs"][0]["pair_id"], f"p{index}")
            self.assertEqual(wave["subjects"][0]["id"], f"s{index}")
            self.assertEqual(wave["links"][0]["pair_id"], f"p{index}")

    def test_seed_waves_reject_single_oversize_pair(self) -> None:
        loaded = synthetic_passport(
            [
                {
                    "pair_id": "oversize",
                    "session_id": "",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "prompt": "x" * 500,
                    "response": "",
                }
            ]
        )
        with self.assertRaisesRegex(LAB.LabError, "seed pair.*exceeds"):
            LAB.seed_waves(loaded, batch_size=10, max_request_bytes=100)

    def test_seed_agent_requires_exact_acknowledged_counts(self) -> None:
        loaded = synthetic_passport(
            [
                {
                    "pair_id": "p1",
                    "session_id": "",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "prompt": "hello",
                    "response": "world",
                }
            ]
        )
        original = LAB.request_json
        LAB.request_json = lambda *args, **kwargs: {
            "pairs": 0,
            "subjects": 0,
            "links": 0,
        }
        try:
            with self.assertRaisesRegex(LAB.LabError, "expected 1"):
                LAB.seed_agent("http://127.0.0.1:1", loaded, 10)
        finally:
            LAB.request_json = original

    def test_history_limit_is_twenty_messages_or_ten_exchanges(self) -> None:
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
            for index in range(24)
        ]
        transcript = LAB.format_chat_transcript(history)
        self.assertEqual(LAB.MAX_HISTORY_MESSAGES, 20)
        self.assertNotIn("m3\n", transcript)
        self.assertIn("m4", transcript)
        self.assertIn("m23", transcript)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("last 20 messages (up to 10 user/assistant\nexchanges)", readme)

    def test_authority_accepts_matching_operator_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            public_key = passport(path)
            loaded = LAB.load_passport(path)
            self.assertEqual(
                LAB.verify_passport_authority(loaded, verification_key=public_key),
                "operator-supplied",
            )

    def test_authority_rejects_wrong_operator_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path)
            loaded = LAB.load_passport(path)
            wrong_key = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
            with self.assertRaisesRegex(LAB.LabError, "does not match"):
                LAB.verify_passport_authority(loaded, verification_key=wrong_key)

    def test_child_environment_requires_explicit_secret_passthrough(self) -> None:
        secret_name = "SUBMISSION_LAB_TEST_SECRET"
        old_value = LAB.os.environ.get(secret_name)
        LAB.os.environ[secret_name] = "not-for-children-by-default"
        try:
            self.assertNotIn(secret_name, LAB.child_environment([]))
            self.assertEqual(
                LAB.child_environment([secret_name])[secret_name],
                "not-for-children-by-default",
            )
        finally:
            if old_value is None:
                LAB.os.environ.pop(secret_name, None)
            else:
                LAB.os.environ[secret_name] = old_value

    def test_run_response_accepts_v3_answer_alias(self) -> None:
        response = LAB.validate_run_response({"answer": "remembered", "tool_calls": []})
        self.assertEqual(response["final_text"], "remembered")

    def test_run_response_rejects_malformed_tool_calls(self) -> None:
        with self.assertRaisesRegex(LAB.LabError, "tool call 0"):
            LAB.validate_run_response({"final_text": "x", "tool_calls": [{}]})

    def test_seed_errors_are_safely_categorized(self) -> None:
        self.assertEqual(
            LAB.classify_seed_error(
                LAB.LabError("agent HTTP 500: private backend detail")
            ),
            "agent_http_5xx",
        )
        self.assertEqual(
            LAB.classify_seed_error(LAB.LabError("agent request timed out")),
            "agent_timeout",
        )
        self.assertEqual(
            LAB.classify_seed_error(
                LAB.LabError(
                    "agent HTTP 500: embedding batch 1 failed after 3 attempts"
                )
            ),
            "seed_embedding_failed",
        )

    def test_chat_is_gated_while_seed_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passport.zip"
            passport(path)
            state = LAB.LabState(
                agent_url="http://127.0.0.1:1",
                passport=LAB.load_passport(path),
                token="test-token",
                port=0,
                allow_remote_agent=False,
                memory_scope="bounded_sample",
            )
            server = LAB.ThreadingHTTPServer(("127.0.0.1", 0), LAB.handler_for(state))
            state.port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{state.port}"
            try:
                meta = json.loads(urllib.request.urlopen(base + "/api/meta").read())
                self.assertEqual(meta["seed_status"], "seeding")
                self.assertEqual(meta["memory_scope"], "bounded_sample")
                self.assertNotIn("counts", meta)
                request = urllib.request.Request(
                    base + "/api/chat",
                    data=b'{"message":"hello","history":[]}',
                    headers={
                        "content-type": "application/json",
                        "x-ditto-lab-token": state.token,
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code, 503)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
