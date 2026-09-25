"""Exact Platform binding tests; no private challenge bytes or provider calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ditto_screener.v13_private_adapter import PrivateExecutionUnavailable
from ditto_screener.v13_replay_private_registry import PlatformReplayPrivateRegistry
from ditto_screening_protocol.v13_private_clean_control import (
    TrustedGenerationGroup,
    compute_v13_generation_role_digest,
)


class FakePlatform:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body

    async def replay_private_inputs(self, _replay_id: UUID) -> dict[str, object]:
        return self.body


def _body() -> dict[str, object]:
    now = datetime.now(UTC)
    group = TrustedGenerationGroup(
        group_id=UUID(int=4),
        replay_id=UUID(int=1),
        target_agent_id=UUID(int=2),
        target_attempt_id=UUID(int=3),
        target_artifact_sha256="a" * 64,
        target_image_sha256="b" * 64,
        control_agent_id=UUID(int=5),
        control_attempt_id=UUID(int=6),
        control_artifact_sha256="c" * 64,
        control_image_sha256="d" * 64,
        approval_id=UUID(int=7),
        approval_receipt_sha256="9" * 64,
        profile_sha256="e" * 64,
        started_at=now - timedelta(minutes=20),
        target_receipt_sha256="0" * 64,
        control_receipt_sha256="0" * 64,
    )
    group = group.model_copy(
        update={
            "target_receipt_sha256": compute_v13_generation_role_digest(
                group, "target"
            ),
            "control_receipt_sha256": compute_v13_generation_role_digest(
                group, "known_benign"
            ),
        }
    )

    def package(role: str) -> dict[str, object]:
        target = role == "target"
        return {
            "group_id": str(group.group_id),
            "role": role,
            "agent_id": str(
                group.target_agent_id if target else group.control_agent_id
            ),
            "attempt_id": str(
                group.target_attempt_id if target else group.control_attempt_id
            ),
            "artifact_sha256": (
                group.target_artifact_sha256
                if target
                else group.control_artifact_sha256
            ),
            "image_sha256": (
                group.target_image_sha256 if target else group.control_image_sha256
            ),
            "profile_sha256": group.profile_sha256,
            "generation_receipt_sha256": (
                group.target_receipt_sha256 if target else group.control_receipt_sha256
            ),
            "manifest_sha256": ("5" if target else "6") * 64,
            "pair_inventory_sha256": "7" * 64,
            "registrar_actor": "test:curator",
            "registered_at": (now - timedelta(minutes=10)).isoformat(),
        }

    def image(role: str) -> dict[str, object]:
        target = role == "target"
        return {
            "role": role,
            "agent_id": str(
                group.target_agent_id if target else group.control_agent_id
            ),
            "attempt_id": str(
                group.target_attempt_id if target else group.control_attempt_id
            ),
            "artifact_sha256": (
                group.target_artifact_sha256
                if target
                else group.control_artifact_sha256
            ),
            "image_sha256": (
                group.target_image_sha256 if target else group.control_image_sha256
            ),
            "image_id": "sha256:" + ("8" if target else "9") * 64,
            "size_bytes": 1024,
            "verified_at": (now - timedelta(minutes=25)).isoformat(),
            "committed_at": (now - timedelta(hours=1)).isoformat(),
            "url": f"https://storage.example/{role}",
        }

    return {
        "replay_id": str(group.replay_id),
        "lease_started_at": (now - timedelta(minutes=5)).isoformat(),
        "lease_deadline": (now + timedelta(minutes=25)).isoformat(),
        "group": group.model_dump(mode="json"),
        "approval": {
            "approval_id": str(group.approval_id),
            "agent_id": str(group.control_agent_id),
            "attempt_id": str(group.control_attempt_id),
            "artifact_sha256": group.control_artifact_sha256,
            "image_sha256": group.control_image_sha256,
            "profile_sha256": group.profile_sha256,
            "approved_at": (now - timedelta(minutes=30)).isoformat(),
            "actor": "test:independent-reviewer",
            "approval_receipt_sha256": group.approval_receipt_sha256,
        },
        "target_package": package("target"),
        "control_package": package("known_benign"),
        "target_image": image("target"),
        "control_image": image("known_benign"),
        "urls_expire_at": (now + timedelta(minutes=5)).isoformat(),
    }


@pytest.mark.asyncio
async def test_registry_resolves_exact_replay_and_control_images() -> None:
    registry = PlatformReplayPrivateRegistry(
        FakePlatform(_body()),  # type: ignore[arg-type] - synthetic Platform transport
        replay_id=UUID(int=1),
        group_id=UUID(int=4),
    )
    snapshot = await registry.snapshot()
    assert snapshot.pair_inventory_sha256 == "7" * 64
    image = await registry.resolve(
        role="target",
        agent_id=UUID(int=2),
        attempt_id=UUID(int=3),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
    )
    assert image.image_id == "sha256:" + "8" * 64
    with pytest.raises(PrivateExecutionUnavailable):
        await registry.resolve(
            role="target",
            agent_id=UUID(int=2),
            attempt_id=UUID(int=3),
            artifact_sha256="a" * 64,
            image_sha256="f" * 64,
        )


@pytest.mark.asyncio
async def test_registry_rejects_mismatched_pair_inventory() -> None:
    body = _body()
    body["control_package"]["pair_inventory_sha256"] = "f" * 64
    registry = PlatformReplayPrivateRegistry(
        FakePlatform(body),  # type: ignore[arg-type] - synthetic Platform transport
        replay_id=UUID(int=1),
        group_id=UUID(int=4),
    )
    with pytest.raises(PrivateExecutionUnavailable):
        await registry.snapshot()
