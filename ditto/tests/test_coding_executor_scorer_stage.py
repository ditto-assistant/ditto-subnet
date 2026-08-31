import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
VERIFY_PATH = ROOT / "infra/ansible/roles/coding_executor/files/verify-scorer-bundle.py"
SPEC = importlib.util.spec_from_file_location("verify_scorer_bundle", VERIFY_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFY
SPEC.loader.exec_module(VERIFY)


def test_scorer_stage_requires_cross_document_and_archive_identity() -> None:
    release = {
        "image_digest": "sha256:" + "1" * 64,
        "image_reference": (
            "ghcr.io/ditto-assistant/dittobench-coding-executor-scorer@sha256:"
            + "1" * 64
        ),
        "locked_policy_sha256": "2" * 64,
        "platform": "linux/amd64",
        "schema": "dittobench-coding-executor-scorer-release-v1",
        "scorer_contract": "1",
        "source_revision": "a" * 40,
    }
    release_raw = json.dumps(release, sort_keys=True).encode()
    bundle = dict(release)
    bundle.update(
        {
            "archive_sha256": "3" * 64,
            "image_id": "sha256:" + "5" * 64,
            "release_manifest_sha256": hashlib.sha256(release_raw).hexdigest(),
            "schema": "dittobench-coding-executor-scorer-bundle-v1",
        }
    )
    bundle_raw = json.dumps(bundle, sort_keys=True).encode()
    VERIFY.validate_documents(
        release_raw, bundle_raw, "3" * 64, hashlib.sha256(bundle_raw).hexdigest()
    )

    bundle["image_digest"] = "sha256:" + "4" * 64
    drifted = json.dumps(bundle, sort_keys=True).encode()
    try:
        VERIFY.validate_documents(
            release_raw, drifted, "3" * 64, hashlib.sha256(drifted).hexdigest()
        )
    except VERIFY.VerificationError as exc:
        assert "image_digest" in str(exc)
    else:
        raise AssertionError("cross-document scorer identity drift was accepted")
