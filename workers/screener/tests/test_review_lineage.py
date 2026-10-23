"""Lineage-scoped review: packet fetch, fallback, and model context binding."""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from ditto_screener.config import ScreenerConfig
from ditto_screener.platform import PlatformError
from ditto_screener.policy import ScreeningOutcome
from ditto_screener.review_lineage_prompt import (
    LINEAGE_REVIEW_INSTRUCTIONS,
    lineage_dossier,
    lineage_l1_text,
)
from ditto_screener.source_review import TarSourceRepository
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    ReviewLineagePacket,
    seal_review_lineage_packet,
)
from tests.test_l2_review import _FakeHarness, _l1, _sol_agent
from tests.test_platform import _AGENT, _make_client
from tests.test_worker import (
    _decision,
    _FakeGate,
    _FakePlatform,
    _item,
    _worker,
)

_PARENT = UUID("11111111-2222-4333-8444-555555555555")


def _packet(
    agent_id: UUID = _AGENT,
    attempt_id: UUID | None = None,
    sha256: str = "de" * 32,
    *,
    scope: str = "diff",
) -> ReviewLineagePacket:
    now = datetime.now(UTC)
    parent = {
        "agent_id": _PARENT,
        "version": 9,
        "artifact_sha256": "ab" * 32,
        "status": "screening_failed",
        "screening_policy_version": 13,
        "submitted_at": now,
    }
    return seal_review_lineage_packet(
        {
            "agent_id": agent_id,
            "attempt_id": attempt_id or uuid4(),
            "artifact_sha256": sha256,
            "policy_version": 13,
            "parent": parent,
            "history": [parent],
            "scope": scope,
            "full_review_reasons": ["build-surface-changed"] if scope == "full" else [],
            "changes": [
                {
                    "path": "src/routing.rs",
                    "change": "modified",
                    "added_lines": 2,
                    "removed_lines": 98,
                    "diff_lines": ["@@ -1,3 +1,1 @@", "-fn unused_router() {}"],
                }
            ],
            "changed_file_count": 1,
            "unchanged_file_count": 150,
            "changed_line_count": 100,
            "rulings": [
                {
                    "agent_id": _PARENT,
                    "version": 9,
                    "court": "ath",
                    "resolution": "clear",
                    "reason": "routed guidance is not rejectable alone",
                    "evidence_references": ["src/settings.rs:261"],
                    "policy_version": 13,
                    "decided_at": now,
                }
            ],
            "findings": [],
        }
    )


def test_packet_digest_binds_every_field() -> None:
    packet = _packet()
    tampered = packet.model_dump(mode="json")
    tampered["rulings"][0]["resolution"] = "reject"
    with pytest.raises(ValueError, match="digest"):
        ReviewLineagePacket.model_validate(tampered)


def test_dossier_section_names_scope_history_and_never_grants_a_verdict() -> None:
    section = lineage_dossier(_packet())
    assert section["scope"] == "diff"
    assert section["rulings"][0]["evidence_references"] == ["src/settings.rs:261"]
    assert "never a verdict" in str(section["instructions"])
    full = lineage_dossier(_packet(scope="full"))
    assert "Scope is full" in str(full["instructions"])
    assert LINEAGE_REVIEW_INSTRUCTIONS in lineage_l1_text(_packet())


async def test_platform_client_refuses_a_packet_for_another_attempt(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    attempt_id = uuid4()
    other = _packet(attempt_id=uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/v1/screener/agent/{_AGENT}/review-lineage"
        assert request.url.params["attempt_id"] == str(attempt_id)
        return httpx.Response(200, json=other.model_dump(mode="json"))

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(PlatformError, match="does not match"):
            await client.get_review_lineage(
                _AGENT, attempt_id=attempt_id, artifact_sha256="de" * 32
            )


class _LineagePlatform(_FakePlatform):
    def __init__(self, packet: ReviewLineagePacket | Exception | None) -> None:
        super().__init__([])
        self.packet = packet
        self.lineage_calls: list[UUID] = []

    async def get_review_lineage(
        self, agent_id: UUID, *, attempt_id: UUID, artifact_sha256: str
    ) -> ReviewLineagePacket:
        del agent_id, artifact_sha256
        self.lineage_calls.append(attempt_id)
        if isinstance(self.packet, Exception):
            raise self.packet
        assert self.packet is not None
        return self.packet


class _RecordingGate(_FakeGate):
    def __init__(self) -> None:
        super().__init__(_decision(ScreeningOutcome.PASS))
        self.lineages: list[Any] = []

    async def screen(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.lineages.append(kwargs.get("review_lineage"))
        return await super().screen(**kwargs)


async def test_worker_passes_the_packet_to_the_gate(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    item = _item(agent_id, lineage_parent_agent_id=_PARENT)
    packet = _packet(agent_id, item.attempt_id)
    platform = _LineagePlatform(packet)
    gate = _RecordingGate()
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)
    assert platform.lineage_calls == [item.attempt_id]
    assert gate.lineages == [packet]


async def test_unavailable_packet_falls_back_to_a_full_review(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    item = _item(uuid4(), lineage_parent_agent_id=_PARENT)
    platform = _LineagePlatform(PlatformError("lineage packet rejected (409)"))
    gate = _RecordingGate()
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)
    assert gate.lineages == [None]
    assert gate.calls == [item.agent_id]


async def test_no_parent_never_fetches_a_packet(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    item = _item(uuid4())
    platform = _LineagePlatform(None)
    gate = _RecordingGate()
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)
    assert platform.lineage_calls == []
    assert gate.lineages == [None]


def _archive(tmp_path: Path) -> Path:
    path = tmp_path / "agent.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        data = b"fn main() {}\n"
        info = tarfile.TarInfo("src/main.rs")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path


async def test_l2_dossier_and_cache_key_carry_the_packet(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    agent = _sol_agent(tmp_path, _FakeHarness(), None)
    packet = _packet()
    kwargs: dict[str, Any] = {
        "artifact_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "l1_observation": _l1(),
        "policy_version": SCREENING_POLICY_VERSION,
        "deadline": None,
    }
    repository = TarSourceRepository(str(archive))
    plain, *_ = await agent._build_dossier(tmp_path, repository, **kwargs)
    scoped, *_ = await agent._build_dossier(
        tmp_path, repository, review_lineage=packet, **kwargs
    )
    assert "review_lineage" not in plain
    assert scoped["review_lineage"]["packet_sha256"] == packet.packet_sha256
    observation = _l1()
    assert agent._cache_key("ab" * 32, observation) != agent._cache_key(
        "ab" * 32, observation, lineage_digest=packet.packet_sha256
    )
    assert agent._analyst_cache_key("ab" * 32, observation) != (
        agent._analyst_cache_key(
            "ab" * 32, observation, lineage_digest=packet.packet_sha256
        )
    )
