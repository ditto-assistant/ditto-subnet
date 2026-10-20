from importlib import import_module
from pathlib import Path

import pytest

from ditto.api_models.screener_review_settings import (
    PolicyManifestProfile,
    policy_manifest_digest,
)


@pytest.mark.parametrize("profile", ["core", "l1", "l1_l2"])
def test_builtin_source_review_manifest_digests_match_worker(
    monkeypatch: pytest.MonkeyPatch, profile: PolicyManifestProfile
) -> None:
    # Exercise the worker's actual manifest builder so future module changes
    # cannot leave Platform marking every fresh worker as stale again.
    repo_root = Path(__file__).resolve().parents[5]
    monkeypatch.syspath_prepend(str(repo_root / "workers" / "screener"))
    builtin_policy_manifest = import_module(
        "ditto_screener.policy"
    ).builtin_policy_manifest
    rotation_id = "policy-v11-global-topdown"
    assert (
        policy_manifest_digest(profile, rotation_id)
        == builtin_policy_manifest(profile, rotation_id).digest
    )
