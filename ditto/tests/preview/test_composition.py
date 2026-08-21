from __future__ import annotations

import pytest

from ditto.preview.composition import CompositionError, compose
from ditto.preview.identity import preview_host, preview_id


def test_dashboard_attaches_to_prod_platform() -> None:
    plan = compose(["dashboard"])
    assert plan.dashboard and not plan.backroom
    assert plan.stack is False
    assert plan.attach_prod_api is True
    assert plan.localnet_validator is False


def test_backroom_requires_isolated_stack() -> None:
    with pytest.raises(CompositionError, match="write control plane"):
        compose(["backroom"])
    plan = compose(["backroom", "stack"])
    assert plan.backroom and plan.stack
    assert plan.attach_prod_api is False


def test_stack_implies_one_localnet_validator_and_frontends() -> None:
    plan = compose(["stack"])
    assert plan.stack
    assert plan.dashboard and plan.backroom
    assert plan.localnet_validator is True
    assert plan.attach_prod_api is False
    assert plan.copy_database is False


def test_stack_copy_implies_stack() -> None:
    plan = compose(["stack-copy"])
    assert plan.stack and plan.copy_database
    assert "stack" in plan.profiles


def test_preview_platform_cannot_attach_to_prod() -> None:
    with pytest.raises(CompositionError, match="cannot attach to production"):
        compose(["stack"], attach_prod_api=True)
    with pytest.raises(CompositionError, match="cannot attach to production"):
        compose(["dashboard", "stack"], attach_prod_api=True)


def test_unknown_and_empty_profiles_fail() -> None:
    with pytest.raises(CompositionError, match="at least one"):
        compose([])
    with pytest.raises(CompositionError, match="unknown"):
        compose(["dashboard", "staging"])


def test_preview_id_uses_branch_and_sha() -> None:
    identity = preview_id("feat/Preview_URLs", "abcdef1234567890")
    assert identity.startswith("feat-preview-urls-")
    assert identity.endswith("abcdef12")
    host = preview_host(identity, "dash")
    assert host.endswith(".preview.dittobench.ai")
    assert "dash-" in host
    with pytest.raises(ValueError, match="hexadecimal SHA"):
        preview_id("feat/x", "not-a-sha-deadbeef")
