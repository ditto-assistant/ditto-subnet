"""Copy-hold court family triage against real Postgres and stored artifacts."""

from __future__ import annotations

import hashlib
import io
import tarfile
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.copy_court_family import TRIAGE_REVISION
from ditto.api_server.copy_hold_court import CopyHoldCourt
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.db.models import (
    Agent,
    AgentStatus,
    ArtifactFetchAudit,
    AthCopyCourtRecommendation,
    AthReview,
    CopyCourtSettingsRevision,
    ScreeningAttempt,
)
from ditto.db.queries.artifact_fetch_audit import ENDPOINT_PUBLIC_ARTIFACT

_T0 = datetime(2026, 10, 28, 12, tzinfo=UTC)

_BASE_FILES = {
    "app/__init__.py": "",
    "app/config.py": "MAX_TOKENS = 4096\nTIMEOUT_SECONDS = 30\nRETRIES = 3\n",
    "app/store.py": (
        "def save_note(store, note):\n"
        "    store.append({'text': note.text, 'saved_at': note.saved_at})\n"
        "    return len(store)\n"
    ),
    "app/action.py": (
        "SYSTEM = 'Answer from the records you were given and cite them.'\n"
        "def plan(request, catalog):\n"
        "    return call_model(SYSTEM, request, tools=catalog)\n"
    ),
}
_MATCHED_DELTA = (
    "HINT_A = 'Prefer the newest dated record when two notes disagree on value.'\n"
    "HINT_B = 'Quote the amount exactly as the note writes it, with its unit.'\n"
    "HINT_C = 'When a ticket number appears, carry it into the final reply line.'\n"
)
_HELD_DELTA = (
    "LANE_NOTE = 'Pick the advisory brief only when every capability stays on.'\n"
    "def closure(refs):\n"
    "    return sorted({ref.number for ref in refs if ref.number is not None})\n"
)


def _tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads: list[str] = []

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        assert max_bytes > 0
        self.reads.append(key)
        if key not in self.objects:
            raise ObjectDownloadFailedError(key)
        return self.objects[key]


async def _agent(
    session: AsyncSession,
    storage: _Storage,
    *,
    hotkey: str,
    name: str,
    version: int,
    files: dict[str, str],
    created_at: datetime,
    status: AgentStatus = AgentStatus.SCORED,
    **extra: object,
) -> Agent:
    agent_id = uuid4()
    blob = _tarball(files)
    storage.objects[f"{agent_id}/agent.tar.gz"] = blob
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=hotkey,
        name=name,
        version=version,
        sha256=hashlib.sha256(blob).hexdigest(),
        status=status,
        screening_policy_version=13,
        created_at=created_at,
        **extra,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    storage: _Storage,
    *,
    held_files: dict[str, str],
    base_public: bool = True,
    screened: bool = True,
    settings: dict | None = None,
) -> tuple[UUID, UUID, UUID]:
    async with maker() as session, session.begin():
        base = await _agent(
            session,
            storage,
            hotkey="5Base",
            name="base",
            version=25,
            files=_BASE_FILES,
            created_at=_T0 - timedelta(days=3),
        )
        if base_public:
            session.add(
                ArtifactFetchAudit(
                    agent_id=base.agent_id,
                    endpoint=ENDPOINT_PUBLIC_ARTIFACT,
                    requester_kind="public",
                    requester_id=None,
                    artifact_sha256=base.sha256,
                    fetched_at=_T0 - timedelta(days=2),
                )
            )
        matched = await _agent(
            session,
            storage,
            hotkey="5Matched",
            name="matched",
            version=4,
            files={
                **_BASE_FILES,
                "app/action.py": _BASE_FILES["app/action.py"] + _MATCHED_DELTA,
            },
            created_at=_T0 - timedelta(days=1),
        )
        reason = (
            f"content near-duplicate of agent {matched.agent_id}: composite delta "
            "0.0373, jaccard 0.805, containment 0.936"
        )
        held_files = {
            **held_files,
            "BASE.md": f"Public base: base v25, sha {base.sha256}\n",
        }
        held = await _agent(
            session,
            storage,
            hotkey="5Held",
            name="held",
            version=1,
            files=held_files,
            created_at=_T0,
            status=AgentStatus.ATH_PENDING_REVIEW,
            duplicate_of=matched.agent_id,
            review_reason=reason,
        )
        if screened:
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=held.agent_id,
                    artifact_sha256=held.sha256,
                    screener_hotkey="5Screener",
                    policy_version=13,
                    status="passed",
                    started_at=_T0 + timedelta(minutes=1),
                    deadline=_T0 + timedelta(hours=1),
                    finished_at=_T0 + timedelta(minutes=20),
                )
            )
        review_id = uuid4()
        session.add(
            AthReview(
                review_id=review_id,
                agent_id=held.agent_id,
                status="pending",
                opened_at=_T0 + timedelta(hours=2),
                original_duplicate_of=matched.agent_id,
                original_reason=reason,
                original_policy_version=13,
                original_evidence={"sha256": held.sha256},
                algorithm_provenance={},
            )
        )
        session.add(
            CopyCourtSettingsRevision(
                parent_revision=0,
                settings=settings or {"mode": "shadow"},
                checksum="cc" * 32,
                reason="shadow family triage for near-duplicate holds",
                actor="operator",
            )
        )
    return held.agent_id, matched.agent_id, review_id


async def _recommendations(
    maker: async_sessionmaker[AsyncSession],
) -> list[AthCopyCourtRecommendation]:
    async with maker() as session:
        return list(await session.scalars(select(AthCopyCourtRecommendation)))


async def test_released_base_hold_gets_family_clear_and_is_audited(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = _Storage()
    held_id, matched_id, review_id = await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "app/action.py": _BASE_FILES["app/action.py"] + _HELD_DELTA,
        },
    )
    court = CopyHoldCourt(session_maker=session_maker, storage=storage)
    counters = await court.tick()
    assert counters["recommended"] == 1 and counters["errors"] == 0
    [rec] = await _recommendations(session_maker)
    assert rec.triage_revision == TRIAGE_REVISION
    assert rec.verdict == "clear"
    assert rec.hold_class == "near_duplicate"
    evidence = rec.evidence
    assert evidence["basis"] == "released_base_explains_overlap"
    assert evidence["attribution"]["unexplained"] == []
    assert evidence["attribution"]["distinctive_total"] == 3
    assert evidence["attribution"]["distinctive_present_count"] == 0
    declared = [
        d for d in evidence["disclosures"] if d["relation"] == "declared_by_held"
    ]
    assert declared and declared[0]["publication_proven_by_reference_time"] is True
    assert evidence["screening"]["delta_read"] is True
    assert evidence["auto_clear_eligible"] is True
    # Shadow: nothing applied, the hold is untouched.
    assert rec.application is None
    async with session_maker() as session:
        held = await session.get(Agent, held_id)
        review = await session.get(AthReview, review_id)
        assert held is not None and held.status == AgentStatus.ATH_PENDING_REVIEW
        assert review is not None and review.status == "pending"
        audits = list(
            await session.scalars(
                select(ArtifactFetchAudit).where(
                    ArtifactFetchAudit.endpoint == "platform.copy_court_triage"
                )
            )
        )
    assert {row.agent_id for row in audits} >= {held_id, matched_id}
    # Already triaged: the next tick does not re-record (and does not crash).
    assert (await court.tick())["recommended"] == 0
    assert court.runtime().last_tick_counters["recommended"] == 0


async def test_unreleased_distinctive_code_recommends_reject_never_applies(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _Storage()
    await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "app/action.py": (
                _BASE_FILES["app/action.py"]
                + _MATCHED_DELTA
                + "OWN = 'held own line'\n"
            ),
        },
        settings={"mode": "enforce", "near_duplicate_mode": "enforce"},
    )

    async def _never(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("a reject must never reach the apply path")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.admin_ath_rulings.apply_court_clear", _never
    )
    court = CopyHoldCourt(session_maker=session_maker, storage=storage)
    await court.tick()
    [rec] = await _recommendations(session_maker)
    assert rec.verdict == "reject"
    assert rec.evidence["attribution"]["distinctive_present_count"] == 3
    assert rec.application is None


async def test_unmodified_fork_of_released_base_recommends_reject_never_applies(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hfull-h3 v3 shape: released base runtime bytes plus prose only."""
    storage = _Storage()
    held_id, _matched, review_id = await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "DECLARATION.md": "Fork of base v25 with attribution.\n",
        },
        settings={"mode": "enforce", "near_duplicate_mode": "enforce"},
    )

    async def _never(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("a reject must never reach the apply path")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.admin_ath_rulings.apply_court_clear", _never
    )
    counters = await CopyHoldCourt(session_maker=session_maker, storage=storage).tick()
    assert counters["recommended"] == 1 and counters["errors"] == 0
    [rec] = await _recommendations(session_maker)
    assert rec.verdict == "reject"
    evidence = rec.evidence
    assert evidence["basis"] == "unmodified_fork_of_released_base"
    assert evidence["attribution"]["unexplained"] == []
    assert evidence["attribution"]["distinctive_present_count"] == 0
    delta = evidence["attribution"]["held_delta"]
    assert delta["runtime_changed_count"] == 0
    assert delta["added_count"] == 2  # DECLARATION.md and BASE.md, both prose
    assert evidence["auto_clear_eligible"] is False
    assert rec.application is None
    async with session_maker() as session:
        held = await session.get(Agent, held_id)
        review = await session.get(AthReview, review_id)
        assert held is not None and held.status == AgentStatus.ATH_PENDING_REVIEW
        assert review is not None and review.status == "pending"


async def test_unproven_base_escalates(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = _Storage()
    await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "app/action.py": _BASE_FILES["app/action.py"] + _HELD_DELTA,
        },
        base_public=False,
    )
    await CopyHoldCourt(session_maker=session_maker, storage=storage).tick()
    [rec] = await _recommendations(session_maker)
    assert rec.verdict == "escalate"
    assert any("missing handoff" in u for u in rec.evidence["unresolved"])


async def test_enforce_applies_only_an_eligible_clear_and_records_outcome(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _Storage()
    held_id, _matched, _review = await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "app/action.py": _BASE_FILES["app/action.py"] + _HELD_DELTA,
        },
        settings={"mode": "enforce", "near_duplicate_mode": "enforce"},
    )
    calls: list[UUID] = []

    async def _apply(_session: object, _state: object, **kwargs: object) -> dict:
        calls.append(kwargs["agent_id"])  # type: ignore[arg-type]
        assert "recommendation" in str(kwargs["reason"])
        return {"status": "applied", "detail": "hold cleared"}

    monkeypatch.setattr(
        "ditto.api_server.endpoints.admin_ath_rulings.apply_court_clear", _apply
    )
    court = CopyHoldCourt(
        session_maker=session_maker, storage=storage, app_state=object()
    )
    counters = await court.tick()
    assert counters["applied"] == 1
    assert calls == [held_id]
    [rec] = await _recommendations(session_maker)
    assert rec.application == {"status": "applied", "detail": "hold cleared"}
    assert rec.applied_at is not None


async def test_enforce_skips_a_clear_without_a_screening_delta_read(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _Storage()
    await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "app/action.py": _BASE_FILES["app/action.py"] + _HELD_DELTA,
        },
        screened=False,
        settings={"mode": "enforce", "near_duplicate_mode": "enforce"},
    )

    async def _never(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("an ineligible clear must not be applied")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.admin_ath_rulings.apply_court_clear", _never
    )
    court = CopyHoldCourt(
        session_maker=session_maker, storage=storage, app_state=object()
    )
    counters = await court.tick()
    assert counters["apply_skipped"] == 1
    [rec] = await _recommendations(session_maker)
    assert rec.verdict == "clear"
    assert rec.application is not None and rec.application["status"] == "skipped"
    assert "delta read" in rec.application["detail"]


async def test_a_recorded_hold_no_longer_starves_newer_holds(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The 2026-10-05 stall: the oldest hold already had a recommendation at
    this settings revision, its insert hit the unique key on every tick, and
    no newer hold was ever triaged."""
    storage = _Storage()
    held_id, _matched, _review = await _seed(
        session_maker,
        storage,
        held_files={
            **_BASE_FILES,
            "app/action.py": _BASE_FILES["app/action.py"] + _HELD_DELTA,
        },
    )
    async with session_maker() as session, session.begin():
        old_id, old_review = uuid4(), uuid4()
        session.add(
            Agent(
                agent_id=old_id,
                miner_hotkey="5Old",
                name="old",
                version=1,
                sha256="ab" * 32,
                status=AgentStatus.ATH_PENDING_REVIEW,
                review_reason="legacy near-copy signal",
                screening_policy_version=13,
                created_at=_T0 - timedelta(days=30),
            )
        )
        await session.flush()
        session.add(
            AthReview(
                review_id=old_review,
                agent_id=old_id,
                status="pending",
                opened_at=_T0 - timedelta(days=30),
                original_reason="legacy near-copy signal",
                original_policy_version=13,
                original_evidence={},
                algorithm_provenance={},
            )
        )
        await session.flush()
        revision = await session.scalar(select(CopyCourtSettingsRevision.revision))
        session.add(
            AthCopyCourtRecommendation(
                recommendation_id=uuid4(),
                review_id=old_review,
                agent_id=old_id,
                verdict="escalate",
                hold_class="unknown",
                reason="Matched reference agent could not be loaded.",
                citations=[],
                evidence={},
                settings_revision=revision,
                settings_checksum="cc" * 32,
            )
        )
    counters = await CopyHoldCourt(session_maker=session_maker, storage=storage).tick()
    assert counters["recommended"] == 1 and counters["errors"] == 0
    recs = await _recommendations(session_maker)
    assert {rec.agent_id for rec in recs} == {old_id, held_id}


async def test_unreadable_artifact_backs_off_without_recording(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = _Storage()
    held_id, _matched, _review = await _seed(
        session_maker,
        storage,
        held_files={**_BASE_FILES},
    )
    storage.objects.pop(f"{held_id}/agent.tar.gz")
    court = CopyHoldCourt(session_maker=session_maker, storage=storage)
    counters = await court.tick()
    assert counters["unavailable"] == 1
    assert await _recommendations(session_maker) == []
    assert court.runtime().last_hold_errors[0]["agent_id"] == str(held_id)
    reads = len(storage.reads)
    assert (await court.tick())["unavailable"] == 0
    assert len(storage.reads) == reads
