"""Lineage references beyond the same hotkey's prior version.

``lineage_reference_mode`` widens the reference a screening attempt is diffed
against: an earlier upload paid by the same coldkey (``same_owner``) or a
released, already-screened base the submission declares (``released_base``).
The default keeps the shipped same-hotkey-version behaviour.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import (
    AgentStatus,
    ArtifactFetchAudit,
    QueuePolicySettingsRevision,
    ScreeningAttempt,
    ScreeningReviewEvent,
)
from ditto.db.queries.artifact_fetch_audit import ENDPOINT_PUBLIC_ARTIFACT
from ditto.screener_policy_state import effective_screening_policy_version
from ditto.tests.api_server.endpoints.test_screener import (
    _AUTH_HEADER,
    _CLAIM_URL,
    _MINER_HOTKEY,
    _SCREENER_HOTKEY,
    _authenticate_screener_client,  # noqa: F401  (autouse: claim admission)
    _install_chain,
    _install_db,
    _seed_agent,
    _seed_hetzner_primary,
)
from ditto.tests.api_server.endpoints.test_screener_review_lineage import (
    _install_objects,
    _tarball,
)
from ditto_screening_protocol import ReviewLineagePacket

_OWNER_HOTKEY = _MINER_HOTKEY
_BASE_HOTKEY = "5BaseMiner" + "b" * 38
_COLDKEY = "5Coldkey" + "c" * 40
_DOCKERFILE = 'FROM rust:1\nCOPY . .\nCMD ["agent"]\n'
_BASE_MAIN = "fn main() {\n    answer();\n}\n\nfn answer() {\n    model();\n}\n"
_HELPERS = "".join(f"fn helper_{n}() {{ model(); }}\n" for n in range(40))


@pytest.fixture(autouse=True)
async def _open_primary_admission(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim-admission precondition the screener claim tests seed."""
    from ditto.db.queries import screener_provider_settings as provider_settings

    monkeypatch.setattr(
        provider_settings,
        "DEFAULT_SCREENER_PROVIDER_SETTINGS",
        provider_settings.DEFAULT_SCREENER_PROVIDER_SETTINGS.model_copy(
            update={"primary_node_id": "subnet-screener-1"}
        ),
    )
    await _seed_hetzner_primary(session_maker)


async def _set_mode(maker: async_sessionmaker[AsyncSession], mode: str) -> None:
    async with maker() as session, session.begin():
        session.add(
            QueuePolicySettingsRevision(
                parent_revision=0,
                scope="*",
                settings={"lineage_reference_mode": mode},
                checksum="ab" * 32,
                reason=f"lineage reference mode {mode}",
                actor="test",
            )
        )


async def _screened(
    maker: async_sessionmaker[AsyncSession],
    *,
    files: dict[str, str | bytes],
    objects: dict[str, bytes],
    hotkey: str,
    name: str,
    version: int,
    created_at: datetime,
    status: AgentStatus = AgentStatus.SCORED,
    coldkey: str | None = None,
    policy: int | None = None,
    public_at: datetime | None = None,
) -> tuple[UUID, str]:
    """An agent that passed screening (with one automated finding) under ``policy``."""
    policy = policy or effective_screening_policy_version()
    blob = _tarball(files)
    sha = hashlib.sha256(blob).hexdigest()
    agent_id = await _seed_agent(
        maker,
        status=status,
        name=name,
        version=version,
        sha256=sha,
        miner_hotkey=hotkey,
        miner_coldkey=coldkey,
        screening_policy_version=policy,
        created_at=created_at,
    )
    objects[f"{agent_id}/agent.tar.gz"] = blob
    attempt_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=sha,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=policy,
                status="passed",
                started_at=created_at,
                deadline=created_at + timedelta(minutes=30),
                finished_at=created_at + timedelta(minutes=5),
            )
        )
        await session.flush()
        session.add(
            ScreeningReviewEvent(
                event_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                event_kind="automated",
                artifact_sha256=sha,
                policy_version=policy,
                actor=f"screener:{_SCREENER_HOTKEY}",
                reviewer_model="openai/gpt-5.6-sol",
                outcome="pass",
                effective_decision="pass",
                reason_code=None,
                reason="the model authors every graded value",
                prior_agent_status="screening",
                next_agent_status="screening_passed",
                evidence={},
                created_at=created_at + timedelta(minutes=5),
            )
        )
        if public_at is not None:
            session.add(
                ArtifactFetchAudit(
                    agent_id=agent_id,
                    endpoint=ENDPOINT_PUBLIC_ARTIFACT,
                    requester_kind="public",
                    requester_id=None,
                    artifact_sha256=sha,
                    fetched_at=public_at,
                )
            )
    return agent_id, sha


async def _candidate(
    maker: async_sessionmaker[AsyncSession],
    *,
    files: dict[str, str | bytes],
    objects: dict[str, bytes],
    hotkey: str = _OWNER_HOTKEY,
    name: str = "owner-agent",
    version: int = 2,
    coldkey: str | None = None,
) -> UUID:
    blob = _tarball(files)
    agent_id = await _seed_agent(
        maker,
        status=AgentStatus.UPLOADED,
        name=name,
        version=version,
        sha256=hashlib.sha256(blob).hexdigest(),
        miner_hotkey=hotkey,
        miner_coldkey=coldkey,
    )
    objects[f"{agent_id}/agent.tar.gz"] = blob
    return agent_id


async def _claim_and_fetch(
    client: httpx.AsyncClient, agent_id: UUID
) -> tuple[dict, httpx.Response]:
    claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
    assert claim.status_code == 200, claim.text
    assert claim.json()["items"], (claim.json(), dict(claim.headers))
    [item] = claim.json()["items"]
    assert item["agent_id"] == str(agent_id)
    response = await client.post(
        f"/api/v1/screener/agent/{agent_id}/review-lineage",
        headers=_AUTH_HEADER,
        params={"attempt_id": item["attempt_id"]},
    )
    return item, response


async def _attempt(
    maker: async_sessionmaker[AsyncSession], attempt_id: str
) -> ScreeningAttempt:
    async with maker() as session:
        attempt = await session.get(ScreeningAttempt, UUID(attempt_id))
    assert attempt is not None
    return attempt


def _old(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@pytest.mark.asyncio
class TestSameOwnerReference:
    @pytest.mark.parametrize("mode", ["same_owner_version", "same_owner"])
    async def test_claim_uses_a_same_coldkey_upload_only_when_widened(
        self,
        mode: str,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        await _set_mode(session_maker, mode)
        objects: dict[str, bytes] = {}
        prior_id, _ = await _screened(
            session_maker,
            files={"Dockerfile": _DOCKERFILE, "src/main.rs": _BASE_MAIN},
            objects=objects,
            hotkey="5OtherHotkey" + "h" * 36,
            name="earlier-name",
            version=7,
            created_at=_old(2),
            coldkey=_COLDKEY,
        )
        child_id = await _candidate(
            session_maker,
            files={
                "Dockerfile": _DOCKERFILE,
                "src/main.rs": _BASE_MAIN.replace("model()", "model_v2()"),
            },
            objects=objects,
            version=1,
            coldkey=_COLDKEY,
        )
        _install_objects(app, objects)
        item, response = await _claim_and_fetch(client, child_id)
        if mode == "same_owner_version":
            assert item["lineage_parent_agent_id"] is None
            assert response.status_code == 409, response.text
            return
        assert item["lineage_parent_agent_id"] == str(prior_id)
        assert response.status_code == 200, response.text
        packet = ReviewLineagePacket.model_validate(response.json())
        assert packet.reference_kind == "same_owner"
        assert packet.scope == "diff"
        assert packet.parent.agent_id == prior_id
        attempt = await _attempt(session_maker, item["attempt_id"])
        assert attempt.lineage_reference_kind == "same_owner"
        assert attempt.lineage_full_review_reasons == []
        assert (
            attempt.lineage_changed_file_count,
            attempt.lineage_changed_line_count,
        ) == (packet.changed_file_count, packet.changed_line_count)

    async def test_a_refused_same_coldkey_upload_is_not_a_reference(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        await _set_mode(session_maker, "same_owner")
        objects: dict[str, bytes] = {}
        await _screened(
            session_maker,
            files={"src/main.rs": _BASE_MAIN},
            objects=objects,
            hotkey="5OtherHotkey" + "h" * 36,
            name="banned-name",
            version=3,
            created_at=_old(2),
            coldkey=_COLDKEY,
            status=AgentStatus.BANNED,
        )
        child_id = await _candidate(
            session_maker,
            files={"src/main.rs": _BASE_MAIN},
            objects=objects,
            version=1,
            coldkey=_COLDKEY,
        )
        _install_objects(app, objects)
        item, _response = await _claim_and_fetch(client, child_id)
        assert item["lineage_parent_agent_id"] is None


@pytest.mark.asyncio
class TestReleasedBaseReference:
    async def _setup(
        self,
        maker: async_sessionmaker[AsyncSession],
        app: FastAPI,
        *,
        mode: str,
        base_status: AgentStatus = AgentStatus.SCORED,
        base_public: bool = True,
        base_policy: int | None = None,
        with_prior: bool = True,
    ) -> tuple[UUID, UUID, UUID | None]:
        _install_db(app, maker)
        _install_chain(app)
        await _set_mode(maker, mode)
        objects: dict[str, bytes] = {}
        base_files: dict[str, str | bytes] = {
            "Dockerfile": _DOCKERFILE,
            "src/main.rs": _BASE_MAIN,
            "src/helpers.rs": _HELPERS,
        }
        base_id, base_sha = await _screened(
            maker,
            files=base_files,
            objects=objects,
            hotkey=_BASE_HOTKEY,
            name="released-base",
            version=6,
            created_at=_old(5),
            status=base_status,
            policy=base_policy,
            public_at=_old(4) if base_public else None,
        )
        prior_id = None
        if with_prior:
            # The owner's own prior version used a different build, so the
            # same-hotkey diff is full (build surface changed).
            prior_id, _ = await _screened(
                maker,
                files={
                    "Dockerfile": _DOCKERFILE.replace("rust:1", "python:3"),
                    "app.py": "print('old')\n",
                },
                objects=objects,
                hotkey=_OWNER_HOTKEY,
                name="owner-agent",
                version=1,
                created_at=_old(3),
            )
        child_id = await _candidate(
            maker,
            files={
                **base_files,
                "src/main.rs": _BASE_MAIN.replace("model()", "model_with_note()"),
                "BASE.md": f"Built on released-base v6, sha {base_sha}.\n",
            },
            objects=objects,
            version=2 if with_prior else 1,
        )
        _install_objects(app, objects)
        return base_id, child_id, prior_id

    async def test_a_declared_released_base_replaces_a_full_same_hotkey_diff(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base_id, child_id, prior_id = await self._setup(
            session_maker, app, mode="released_base"
        )
        item, response = await _claim_and_fetch(client, child_id)
        assert item["lineage_parent_agent_id"] == str(prior_id)
        assert response.status_code == 200, response.text
        packet = ReviewLineagePacket.model_validate(response.json())
        assert packet.reference_kind == "released_base"
        assert packet.scope == "diff"
        assert packet.parent.agent_id == base_id
        assert {change.path for change in packet.changes} == {"BASE.md", "src/main.rs"}
        attempt = await _attempt(session_maker, item["attempt_id"])
        assert attempt.lineage_parent_agent_id == base_id
        assert attempt.lineage_reference_kind == "released_base"
        assert attempt.lineage_full_review_reasons == []
        # A second fetch rebuilds the bound packet exactly.
        again = await client.post(
            f"/api/v1/screener/agent/{child_id}/review-lineage",
            headers=_AUTH_HEADER,
            params={"attempt_id": item["attempt_id"]},
        )
        assert again.status_code == 200, again.text
        assert again.json()["packet_sha256"] == packet.packet_sha256

    async def test_default_mode_keeps_the_full_same_hotkey_diff_and_records_why(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _base_id, child_id, prior_id = await self._setup(
            session_maker, app, mode="same_owner_version"
        )
        item, response = await _claim_and_fetch(client, child_id)
        assert response.status_code == 200, response.text
        packet = ReviewLineagePacket.model_validate(response.json())
        assert packet.reference_kind is None
        assert "reference_kind" not in {
            key for key, value in response.json().items() if value is not None
        }
        assert packet.parent.agent_id == prior_id
        assert packet.scope == "full"
        attempt = await _attempt(session_maker, item["attempt_id"])
        assert attempt.lineage_reference_kind == "same_owner_version"
        assert attempt.lineage_full_review_reasons == ["build-surface-changed"]

    @pytest.mark.parametrize("refusal", ["unpublished", "other_policy", "banned"])
    async def test_an_ineligible_base_is_never_the_reference(
        self,
        refusal: str,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _base_id, child_id, prior_id = await self._setup(
            session_maker,
            app,
            mode="released_base",
            base_public=refusal != "unpublished",
            base_policy=(
                effective_screening_policy_version() + 1
                if refusal == "other_policy"
                else None
            ),
            base_status=(
                AgentStatus.BANNED if refusal == "banned" else AgentStatus.SCORED
            ),
        )
        item, response = await _claim_and_fetch(client, child_id)
        assert response.status_code == 200, response.text
        packet = ReviewLineagePacket.model_validate(response.json())
        assert packet.parent.agent_id == prior_id
        assert packet.scope == "full"
        attempt = await _attempt(session_maker, item["attempt_id"])
        assert attempt.lineage_reference_kind == "same_owner_version"

    async def test_a_first_version_reviews_against_its_declared_base(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base_id, child_id, _prior = await self._setup(
            session_maker, app, mode="released_base", with_prior=False
        )
        item, response = await _claim_and_fetch(client, child_id)
        assert item["lineage_parent_agent_id"] is None
        assert response.status_code == 200, response.text
        packet = ReviewLineagePacket.model_validate(response.json())
        assert packet.parent.agent_id == base_id
        assert packet.reference_kind == "released_base"
        attempt = await _attempt(session_maker, item["attempt_id"])
        assert attempt.lineage_parent_agent_id == base_id

    async def test_the_smallest_diff_among_declared_bases_wins(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        await _set_mode(session_maker, "released_base")
        objects: dict[str, bytes] = {}
        far_files: dict[str, str | bytes] = {
            "Dockerfile": _DOCKERFILE,
            "src/main.rs": _BASE_MAIN,
        }
        near_files: dict[str, str | bytes] = {**far_files, "src/helpers.rs": _HELPERS}
        far_id, far_sha = await _screened(
            session_maker,
            files=far_files,
            objects=objects,
            hotkey=_BASE_HOTKEY,
            name="far-base",
            version=1,
            created_at=_old(6),
            public_at=_old(5),
        )
        near_id, near_sha = await _screened(
            session_maker,
            files=near_files,
            objects=objects,
            hotkey="5NearBase" + "n" * 39,
            name="near-base",
            version=1,
            created_at=_old(6),
            public_at=_old(5),
        )
        child_id = await _candidate(
            session_maker,
            files={
                **near_files,
                "src/main.rs": _BASE_MAIN.replace("model()", "model_v3()"),
                "DECLARATION.md": f"Bases: {far_sha} and {near_sha}\n",
            },
            objects=objects,
            version=1,
        )
        _install_objects(app, objects)
        _item, response = await _claim_and_fetch(client, child_id)
        assert response.status_code == 200, response.text
        packet = ReviewLineagePacket.model_validate(response.json())
        assert packet.parent.agent_id == near_id
        assert packet.parent.agent_id != far_id


async def test_declared_references_reads_only_declaration_documents() -> None:
    from ditto.api_server.review_lineage import declared_references

    base_id = uuid4()
    declared = declared_references(
        {
            "BASE.md": f"base {'a' * 64} agent {base_id} (named-base v21)",
            "src/hash.rs": f'const KNOWN: &str = "{"c" * 64}";',
        }
    )
    assert declared.shas == frozenset({"a" * 64})
    assert declared.agent_ids == frozenset({base_id})
    assert declared.name_versions == frozenset({("named-base", 21)})
