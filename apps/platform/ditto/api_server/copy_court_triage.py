"""Load evidence for one near-duplicate copy hold and run the family triage.

The court's deterministic attribution (:mod:`copy_court_family`) is pure; this
module gathers what it needs:

* the held agent, the matched agent (or, when the matched agent was uploaded
  after the held one, the matched owner's latest earlier version);
* candidate bases: SHAs/UUIDs named in either side's declaration documents,
  the matched owner's earlier versions, and the most similar agents whose
  exact SHA had a public handoff before the held upload;
* each candidate's exact-SHA public handoff (the same proof
  ``get_agent_source_release_evidence`` reads);
* the held agent's passed screening of this exact artifact (the I1-I8 read of
  its own delta) and open quarantines;
* resolved copy-hold precedents for the citation.

Storage reads happen outside any transaction, and every artifact read is
audited under the court's actor like an operator diff.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ditto.api_server.copy_court_family import (
    TRIAGE_REVISION,
    ArtifactView,
    DisclosureProof,
    FamilyContext,
    FamilyFacts,
    FamilyVerdict,
    PublicBase,
    ScreeningEvidence,
    attribute_family,
    declared_name_versions,
    declared_references,
    family_verdict,
)
from ditto.api_server.fingerprint import _kit_digests, line_similarity
from ditto.api_server.source_inspect import (
    MAX_TARBALL_BYTES,
    SourceInspectError,
    TarSourceInspector,
)
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.db.models import (
    Agent,
    ArtifactFetchAudit,
    AthReview,
    ScreeningAttempt,
    ScreeningQuarantine,
)
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_PUBLIC_ARTIFACT,
    record_artifact_fetch,
)
from ditto.db.queries.payments import get_miner_coldkeys_for_agents

logger = logging.getLogger(__name__)

ENDPOINT_COPY_COURT_TRIAGE = "platform.copy_court_triage"
# Total text budget per artifact for line attribution; larger than the operator
# diff's default so a big authored file is compared rather than omitted.
TRIAGE_TEXT_BUDGET_BYTES = 16 * 1024 * 1024
TRIAGE_MAX_TEXT_FILES = 2048
# Artifact reads per triage: held, matched, prior, plus at most this many bases.
MAX_BASES = 6
MAX_SIMILAR_PUBLIC = 4
MAX_MATCHED_LINEAGE = 64
MAX_DECLARED_NAMES = 16
PRECEDENT_LIMIT = 3


class ArtifactStore(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes: ...


class TriageUnavailableError(Exception):
    """An artifact could not be read; retry on a later tick."""


@dataclass(frozen=True)
class FamilyTriageResult:
    verdict: FamilyVerdict
    facts: FamilyFacts | None
    evidence: dict[str, Any]


def _label(agent: Agent) -> str:
    return f"{agent.name} v{agent.version}" if agent.version is not None else agent.name


async def _view(agent: Agent, storage: ArtifactStore) -> ArtifactView:
    try:
        tar_bytes = await storage.get_object(
            key=f"{agent.agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
        )
    except ObjectDownloadFailedError as error:
        raise TriageUnavailableError(
            f"artifact {agent.agent_id} is unavailable in storage"
        ) from error

    def _read() -> ArtifactView:
        if hashlib.sha256(tar_bytes).hexdigest() != agent.sha256:
            raise TriageUnavailableError(
                f"stored artifact {agent.agent_id} does not match its digest"
            )
        inspector = TarSourceInspector(tar_bytes)
        snapshot = inspector.read_text_snapshot(
            max_files=TRIAGE_MAX_TEXT_FILES,
            max_total_bytes=TRIAGE_TEXT_BUDGET_BYTES,
        )
        return ArtifactView(
            agent_id=str(agent.agent_id),
            label=_label(agent),
            sha256=agent.sha256,
            created_at=agent.created_at,
            digests=inspector.member_digests(),
            texts=snapshot.texts,
            omitted=frozenset(snapshot.omitted_paths),
        )

    try:
        return await asyncio.to_thread(_read)
    except SourceInspectError as error:
        raise TriageUnavailableError(str(error)) from error


async def _publication(
    session: AsyncSession, agents: list[Agent]
) -> dict[UUID, tuple[datetime | None, int]]:
    """Earliest exact-SHA public handoff and handoff count per agent."""
    if not agents:
        return {}
    by_id = {agent.agent_id: agent.sha256 for agent in agents}
    rows = await session.execute(
        select(
            ArtifactFetchAudit.agent_id,
            ArtifactFetchAudit.artifact_sha256,
            func.min(ArtifactFetchAudit.fetched_at),
            func.count(),
        )
        .where(
            ArtifactFetchAudit.agent_id.in_(list(by_id)),
            ArtifactFetchAudit.endpoint == ENDPOINT_PUBLIC_ARTIFACT,
            ArtifactFetchAudit.requester_kind == "public",
            ArtifactFetchAudit.requester_id.is_(None),
        )
        .group_by(ArtifactFetchAudit.agent_id, ArtifactFetchAudit.artifact_sha256)
    )
    out: dict[UUID, tuple[datetime | None, int]] = dict.fromkeys(by_id, (None, 0))
    for agent_id, sha, earliest, count in rows.tuples().all():
        if by_id.get(agent_id) == sha:
            out[agent_id] = (earliest, int(count))
    return out


async def _public_before(session: AsyncSession, at: datetime) -> list[UUID]:
    """Agents whose exact SHA had a public handoff at or before ``at``."""
    rows = await session.execute(
        select(ArtifactFetchAudit.agent_id)
        .join(Agent, Agent.agent_id == ArtifactFetchAudit.agent_id)
        .where(
            ArtifactFetchAudit.endpoint == ENDPOINT_PUBLIC_ARTIFACT,
            ArtifactFetchAudit.requester_kind == "public",
            ArtifactFetchAudit.requester_id.is_(None),
            ArtifactFetchAudit.artifact_sha256 == Agent.sha256,
            ArtifactFetchAudit.fetched_at <= at,
        )
        .group_by(ArtifactFetchAudit.agent_id)
    )
    return [row[0] for row in rows.all()]


async def _similar_public(
    session: AsyncSession, held: Agent, exclude: set[UUID]
) -> list[Agent]:
    """The public-before-upload agents most similar to the held agent."""
    public_ids = [
        agent_id
        for agent_id in await _public_before(session, held.created_at)
        if agent_id not in exclude
    ]
    held_fingerprint = await session.scalar(
        select(Agent.content_fingerprint).where(Agent.agent_id == held.agent_id)
    )
    if not public_ids or held_fingerprint is None:
        return []
    rows = (
        await session.execute(
            select(Agent.agent_id, Agent.content_fingerprint).where(
                Agent.agent_id.in_(public_ids)
            )
        )
    ).all()
    scored = sorted(
        (
            (line_similarity(held_fingerprint, fingerprint)[1], agent_id)
            for agent_id, fingerprint in rows
            if fingerprint is not None
        ),
        reverse=True,
    )
    top = [agent_id for score, agent_id in scored[:MAX_SIMILAR_PUBLIC] if score > 0]
    if not top:
        return []
    found = {
        agent.agent_id: agent
        for agent in await session.scalars(select(Agent).where(Agent.agent_id.in_(top)))
    }
    return [found[agent_id] for agent_id in top if agent_id in found]


async def _screening(session: AsyncSession, held: Agent) -> ScreeningEvidence:
    attempt = await session.scalar(
        select(ScreeningAttempt)
        .where(
            ScreeningAttempt.agent_id == held.agent_id,
            ScreeningAttempt.artifact_sha256 == held.sha256,
            ScreeningAttempt.status == "passed",
            ScreeningAttempt.build_only.is_(False),
        )
        .order_by(
            ScreeningAttempt.policy_version.desc(),
            ScreeningAttempt.started_at.desc(),
        )
        .limit(1)
    )
    open_quarantines = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreeningQuarantine)
            .where(
                ScreeningQuarantine.agent_id == held.agent_id,
                ScreeningQuarantine.status == "active",
            )
        )
        or 0
    )
    return ScreeningEvidence(
        attempt_id=str(attempt.attempt_id) if attempt is not None else None,
        policy_version=attempt.policy_version if attempt is not None else None,
        agent_policy_version=int(held.screening_policy_version or 0),
        lineage_scope=attempt.lineage_scope if attempt is not None else None,
        open_quarantines=open_quarantines,
    )


async def copy_hold_precedents(
    session: AsyncSession,
    *,
    resolution: str,
    exclude_agent_id: UUID,
    prefer: list[str],
) -> list[dict[str, Any]]:
    """Resolved copy-hold rulings to cite, from the live precedent store.

    Clears cite released-base clears; rejects cite non-public-copy rejects.
    Rulings naming one of ``prefer`` (base agent id prefixes) sort first.
    """
    stored_kind = AthReview.algorithm_provenance["review_kind"].as_string()
    copy_kind = or_(stored_kind.is_(None), stored_kind == "copy")
    patterns = (
        ["%released%", "%public base%", "%publicly released%"]
        if resolution == "clear"
        else ["%non-public%", "%unreleased%", "%embargo%"]
    )
    rows: list[tuple[AthReview, Agent]] = [
        (row[0], row[1])
        for row in (
            await session.execute(
                select(AthReview, Agent)
                .join(Agent, Agent.agent_id == AthReview.agent_id)
                .where(
                    AthReview.status == "resolved",
                    AthReview.resolution == resolution,
                    AthReview.agent_id != exclude_agent_id,
                    copy_kind,
                    or_(*(AthReview.resolution_reason.ilike(p) for p in patterns)),
                )
                .order_by(AthReview.resolved_at.desc())
                .limit(50)
            )
        ).all()
    ]

    def rank(row: tuple[AthReview, Agent]) -> int:
        reason = row[0].resolution_reason or ""
        return 0 if any(token and token in reason for token in prefer) else 1

    picked = sorted(rows, key=rank)[:PRECEDENT_LIMIT]
    return [
        {
            "kind": "resolved_ath_review",
            "review_id": str(review.review_id),
            "agent_id": str(agent.agent_id),
            "location": _label(agent),
            "resolution": review.resolution,
            "resolved_at": (
                review.resolved_at.isoformat() if review.resolved_at else None
            ),
            "excerpt": (review.resolution_reason or "")[:240],
        }
        for review, agent in picked
    ]


async def _audit(session: AsyncSession, agents: list[Agent], held: Agent) -> None:
    for agent in agents:
        await record_artifact_fetch(
            session,
            agent_id=agent.agent_id,
            endpoint=ENDPOINT_COPY_COURT_TRIAGE,
            requester_kind="admin",
            requester_id="platform:copy-hold-court",
            artifact_sha256=agent.sha256,
            detail={
                "role": "candidate" if agent.agent_id == held.agent_id else "reference",
                "held_agent_id": str(held.agent_id),
                "triage_revision": TRIAGE_REVISION,
            },
        )
    await session.commit()


async def triage_family(
    session: AsyncSession,
    storage: ArtifactStore,
    *,
    review: AthReview,
    held: Agent,
) -> FamilyTriageResult:
    """Run the deterministic family triage for one near-duplicate hold."""
    reported = (
        await session.get(Agent, review.original_duplicate_of)
        if review.original_duplicate_of is not None
        else None
    )
    if reported is None:
        raise TriageUnavailableError("matched reference agent could not be loaded")

    # Who came first: a matched agent uploaded after the held one cannot be
    # its source; its owner's latest earlier version stands in, if any.
    matched = reported
    substituted_from: str | None = None
    matched_after_held = False
    if reported.created_at >= held.created_at:
        earlier = await session.scalar(
            select(Agent)
            .where(
                Agent.miner_hotkey == reported.miner_hotkey,
                Agent.created_at < held.created_at,
            )
            .order_by((Agent.name == reported.name).desc(), Agent.created_at.desc())
            .limit(1)
        )
        if earlier is None:
            matched_after_held = True
        else:
            matched, substituted_from = earlier, str(reported.agent_id)

    coldkeys = await get_miner_coldkeys_for_agents(
        session, agent_ids={held.agent_id, matched.agent_id}
    )
    same_owner = held.miner_hotkey == matched.miner_hotkey or (
        coldkeys.get(held.agent_id) is not None
        and coldkeys.get(held.agent_id) == coldkeys.get(matched.agent_id)
    )
    held_prior = await session.scalar(
        select(Agent)
        .where(
            Agent.miner_hotkey == held.miner_hotkey,
            Agent.name == held.name,
            Agent.created_at < matched.created_at,
            Agent.agent_id != held.agent_id,
        )
        .order_by(Agent.created_at.desc())
        .limit(1)
    )
    screening = await _screening(session, held)
    matched_lineage = list(
        await session.scalars(
            select(Agent)
            .where(
                Agent.miner_hotkey == matched.miner_hotkey,
                Agent.created_at < held.created_at,
                Agent.agent_id != matched.agent_id,
            )
            .order_by(Agent.created_at.desc())
            .limit(MAX_MATCHED_LINEAGE)
        )
    )
    # Session reads end here; storage reads must not hold a transaction.
    await session.commit()

    held_view = await _view(held, storage)
    matched_view = await _view(matched, storage)

    declared_shas, declared_ids = declared_references(held_view.texts)
    matched_shas, matched_ids = declared_references(matched_view.texts)
    candidates: dict[UUID, tuple[Agent, str]] = {}

    async def add_declared(
        shas: set[str],
        ids: set[str],
        pairs: set[tuple[str, int]],
        relation: str,
    ) -> None:
        uuid_ids = []
        for text in ids:
            try:
                uuid_ids.append(UUID(text))
            except ValueError:
                continue
        named = sorted(pairs)[:MAX_DECLARED_NAMES]
        if not shas and not uuid_ids and not named:
            return
        clauses: list[ColumnElement[bool]] = [
            Agent.sha256.in_(sorted(shas)),
            Agent.agent_id.in_(uuid_ids),
        ]
        clauses.extend(
            (Agent.name == name) & (Agent.version == version) for name, version in named
        )
        rows = await session.scalars(
            select(Agent).where(or_(*clauses), Agent.created_at < held.created_at)
        )
        for agent in rows:
            if agent.agent_id not in (held.agent_id, matched.agent_id):
                candidates.setdefault(agent.agent_id, (agent, relation))

    await add_declared(
        declared_shas,
        declared_ids,
        declared_name_versions(held_view.texts),
        "declared_by_held",
    )
    await add_declared(
        matched_shas,
        matched_ids,
        declared_name_versions(matched_view.texts),
        "declared_by_matched",
    )
    for agent in matched_lineage:
        candidates.setdefault(agent.agent_id, (agent, "matched_owner_lineage"))
    similar = await _similar_public(
        session, held, exclude={held.agent_id, matched.agent_id, *candidates}
    )
    for agent in similar:
        candidates.setdefault(agent.agent_id, (agent, "similar_public"))

    publication = await _publication(
        session, [matched, *(agent for agent, _ in candidates.values())]
    )
    await session.commit()

    def proof(agent: Agent, relation: str) -> DisclosureProof:
        earliest, count = publication.get(agent.agent_id, (None, 0))
        return DisclosureProof(
            agent_id=str(agent.agent_id),
            label=_label(agent),
            sha256=agent.sha256,
            relation=relation,
            publication_at=earliest,
            handoff_count=count,
            proven=earliest is not None and earliest <= held.created_at,
        )

    matched_proof = proof(matched, "matched")
    proofs = [proof(agent, relation) for agent, relation in candidates.values()]
    # Disclosure evidence names every declared candidate and every proven one;
    # the unproven bulk of the matched owner's lineage is summarized.
    shown = [
        p
        for p in proofs
        if p.proven or p.relation in ("declared_by_held", "declared_by_matched")
    ]
    relation_rank = {
        "declared_by_held": 0,
        "declared_by_matched": 1,
        "matched_owner_lineage": 2,
        "similar_public": 3,
    }
    # Within a relation, load the candidates most similar to the held agent
    # first: an old public version of the matched lineage (the one the held
    # agent actually inherits) outranks its later, unrelated versions.
    proven_ids = [UUID(p.agent_id) for p in proofs if p.proven]
    held_fingerprint = await session.scalar(
        select(Agent.content_fingerprint).where(Agent.agent_id == held.agent_id)
    )
    similarity: dict[str, float] = {}
    if proven_ids and held_fingerprint is not None:
        for agent_id, fingerprint in (
            await session.execute(
                select(Agent.agent_id, Agent.content_fingerprint).where(
                    Agent.agent_id.in_(proven_ids)
                )
            )
        ).all():
            similarity[str(agent_id)] = line_similarity(held_fingerprint, fingerprint)[
                1
            ]
    await session.commit()
    proven_candidates = sorted(
        (p for p in proofs if p.proven),
        key=lambda p: (
            relation_rank.get(p.relation, 9),
            -similarity.get(p.agent_id, 0.0),
            p.label,
        ),
    )
    to_load = proven_candidates[:MAX_BASES]
    loaded: list[PublicBase] = []
    agents_read: list[Agent] = [held, matched]
    for candidate in to_load:
        agent = candidates[UUID(candidate.agent_id)][0]
        loaded.append(
            PublicBase(
                view=await _view(agent, storage),
                relation=candidate.relation,
                publication_at=candidate.publication_at,
            )
        )
        agents_read.append(agent)
    if matched_proof.proven:
        loaded.append(
            PublicBase(
                view=matched_view,
                relation="matched",
                publication_at=matched_proof.publication_at,
            )
        )
    prior_views: list[ArtifactView] = []
    if held_prior is not None:
        prior_views.append(await _view(held_prior, storage))
        agents_read.append(held_prior)

    kit_exact = _kit_digests()[0]
    facts = await asyncio.to_thread(
        attribute_family,
        held=held_view,
        matched=matched_view,
        bases=loaded,
        held_prior=prior_views,
        kit_digests=kit_exact,
    )
    prefer = [base.view.agent_id[:8] for base in loaded]
    clear_precedents = await copy_hold_precedents(
        session, resolution="clear", exclude_agent_id=held.agent_id, prefer=prefer
    )
    reject_precedents = await copy_hold_precedents(
        session, resolution="reject", exclude_agent_id=held.agent_id, prefer=[]
    )
    await session.commit()
    disclosures = [matched_proof, *shown]
    context = FamilyContext(
        held=held_view,
        matched=matched_view,
        matched_public=matched_proof.proven,
        same_owner=same_owner,
        matched_after_held=matched_after_held,
        substituted_from=substituted_from,
        disclosures=disclosures,
        screening=screening,
        clear_precedents=clear_precedents,
        reject_precedents=reject_precedents,
    )
    verdict = family_verdict(context, facts)
    await _audit(session, agents_read, held)
    evidence: dict[str, Any] = {
        "triage_revision": TRIAGE_REVISION,
        "basis": verdict.basis,
        "held": {
            "agent_id": str(held.agent_id),
            "label": _label(held),
            "sha256": held.sha256,
            "uploaded_at": held.created_at.isoformat(),
            "coldkey": coldkeys.get(held.agent_id),
        },
        "matched": {
            "agent_id": str(matched.agent_id),
            "label": _label(matched),
            "sha256": matched.sha256,
            "uploaded_at": matched.created_at.isoformat(),
            "coldkey": coldkeys.get(matched.agent_id),
            "reported_duplicate_of": str(reported.agent_id),
            "substituted_from": substituted_from,
            "public_before_held_upload": matched_proof.proven,
        },
        "same_owner": same_owner,
        "matched_after_held": matched_after_held,
        "held_prior": (
            {
                "agent_id": str(held_prior.agent_id),
                "label": _label(held_prior),
                "uploaded_at": held_prior.created_at.isoformat(),
            }
            if held_prior is not None
            else None
        ),
        "disclosures": [p.as_evidence() for p in disclosures],
        "unproven_candidate_count": sum(1 for p in proofs if not p.proven),
        "bases_compared": [
            {
                "agent_id": base.view.agent_id,
                "label": base.view.label,
                "relation": base.relation,
            }
            for base in loaded
        ],
        "attribution": facts.as_evidence(),
        "screening": screening.as_evidence(),
        "unresolved": verdict.unresolved,
        "auto_clear_eligible": verdict.auto_clear_eligible,
        "auto_clear_blockers": verdict.auto_clear_blockers,
    }
    return FamilyTriageResult(verdict=verdict, facts=facts, evidence=evidence)


__all__ = [
    "ENDPOINT_COPY_COURT_TRIAGE",
    "ArtifactStore",
    "FamilyTriageResult",
    "TriageUnavailableError",
    "copy_hold_precedents",
    "triage_family",
]
