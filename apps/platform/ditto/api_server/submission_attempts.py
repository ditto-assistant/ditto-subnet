"""Pure artifact classification and admission timing; never a misconduct verdict.

Only Platform-verified bytes and payment/attestation provenance may reach this
module. Runtime equality is path independent so archive metadata and file renames
do not erase history. Fuzzy comparison uses the existing reference-aware lexical
channel, including its corpus compatibility and minimum residual safeguards.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ditto.api_models.submission_attempts import (
    AttemptControlSettings,
    AttemptGuidance,
    AttemptKind,
)
from ditto.api_server.fingerprint import (
    compute_content_fingerprint,
    content_similarity,
    reference_corpus_provenance,
)
from ditto.api_server.source_inspect import validate_upload_archive

CLASSIFIER_VERSION = 1
# Keep this list narrow: data, prompts, manifests, scripts, and opaque binaries
# are runtime inputs unless proven otherwise. Docker repairs are bounded retries,
# not certification that a Dockerfile cannot change behavior.
_PACKAGING_FILES = frozenset({"Dockerfile", ".dockerignore"})
REPAIR_REASONS = frozenset(
    {
        "docker-build",
        "health-contract",
        "seed-ack-invalid",
        "seed-memory-cap",
        "seed-exit",
        "seed-readonly-write",
        "seed-http-error",
        "seed-oversized-response",
        "seed-unreachable",
    }
)


def settings_digest(settings: AttemptControlSettings) -> str:
    values = settings.model_dump(exclude={"mode"})
    values["classifier_version"] = CLASSIFIER_VERSION
    values["reference_corpus"] = reference_corpus_provenance()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def artifact_profile(
    tar_bytes: bytes, *, compute_fingerprint: bool = True
) -> dict[str, Any]:
    """Build a bounded source profile without extracting or executing anything."""
    validate_upload_archive(tar_bytes)
    members: dict[str, str] = {}
    opaque: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            digest = hashlib.sha256()
            decoder = codecs.getincrementaldecoder("utf-8")()
            text_content = True
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
                if text_content:
                    try:
                        decoder.decode(chunk)
                        text_content = b"\x00" not in chunk
                    except UnicodeDecodeError:
                        text_content = False
            if text_content:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    text_content = False
            # Archive validation rejects duplicate paths and unsafe members.
            members[member.name.removeprefix("./")] = digest.hexdigest()
            if (
                not text_content
                and member.name.removeprefix("./") not in _PACKAGING_FILES
            ):
                opaque.append(digest.hexdigest())
    runtime = sorted(
        value for path, value in members.items() if path not in _PACKAGING_FILES
    )
    packaging = sorted(
        (path, value) for path, value in members.items() if path in _PACKAGING_FILES
    )
    return {
        "version": CLASSIFIER_VERSION,
        "sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "runtime_hash": hashlib.sha256(json.dumps(runtime).encode()).hexdigest(),
        "packaging_hash": hashlib.sha256(json.dumps(packaging).encode()).hexdigest(),
        "runtime_files": len(runtime),
        "opaque_hash": hashlib.sha256(json.dumps(sorted(opaque)).encode()).hexdigest(),
        "fingerprint": compute_content_fingerprint(tar_bytes)
        if compute_fingerprint
        else None,
    }


@dataclass(frozen=True)
class PriorAttempt:
    agent_id: UUID
    lineage_agent_id: UUID
    profile: dict[str, Any]
    classification: str
    submitted_at: datetime
    feedback_at: datetime | None
    outcome: str  # completed | repairable | infrastructure | pending
    reason_code: str | None = None
    fast_repair: bool = False
    policy_digest: str = ""


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def decide_attempt(
    profile: dict[str, Any] | None,
    history: list[PriorAttempt],
    *,
    settings: AttemptControlSettings,
    revision: int,
    now: datetime,
) -> AttemptGuidance:
    """Compare exact predecessor identities and charge only completed feedback.

    A candidate's own future infrastructure failure can never burn budget: prior
    attempts count only when their authoritative outcome has completed by now.
    No prior moderation verdict is copied to the new submission.
    """
    current = utc(now)
    older = [item for item in history if utc(item.submitted_at) < current]
    older.sort(
        key=lambda item: (utc(item.submitted_at), item.agent_id.int), reverse=True
    )
    base: dict[str, Any] = {
        "policy_revision": revision,
        "mode": settings.mode,
        "settings_digest": settings_digest(settings),
        "evaluated_at": current,
    }
    if not profile or profile.get("version") != CLASSIFIER_VERSION:
        return AttemptGuidance(
            **base,
            classification="inconclusive",
            reason=(
                "Verified artifact comparison is unavailable; "
                "no attempt penalty is applied."
            ),
        )
    if not older:
        return AttemptGuidance(
            **base,
            classification="first_submission",
            reason="No proven owner predecessor is available.",
        )

    ranked: list[tuple[float, PriorAttempt]] = []
    comparable_pair = False
    for item in older:
        if item.profile.get("version") != CLASSIFIER_VERSION:
            continue
        same_runtime = bool(profile.get("runtime_files")) and profile.get(
            "runtime_hash"
        ) == item.profile.get("runtime_hash")
        similarity, _ = content_similarity(
            profile.get("fingerprint"), item.profile.get("fingerprint")
        )
        left = profile.get("fingerprint") or {}
        right = item.profile.get("fingerprint") or {}
        comparable_pair |= bool(
            left.get("m")
            and right.get("m")
            and left.get("v") is not None
            and left.get("v") == right.get("v")
            and left.get("corpus") == right.get("corpus")
        )
        if same_runtime or similarity >= settings.lineage_jaccard:
            ranked.append((1.0 if same_runtime else similarity, item))
    if not ranked:
        return AttemptGuidance(
            **base,
            classification="material_new_work" if comparable_pair else "inconclusive",
            reason=(
                "No compatible low-delta predecessor; "
                "the current artifact receives its own review."
            ),
        )
    # Max similarity, then newest exact predecessor, then stable identity.
    similarity, reference = max(
        ranked,
        key=lambda pair: (pair[0], utc(pair[1].submitted_at), pair[1].agent_id.int),
    )
    same_runtime = bool(profile.get("runtime_files")) and (
        profile["runtime_hash"] == reference.profile.get("runtime_hash")
    )
    if (
        not same_runtime
        and profile.get("opaque_hash") is not None
        and profile["opaque_hash"] != reference.profile.get("opaque_hash")
    ):
        return AttemptGuidance(
            **base,
            classification="inconclusive",
            reference_agent_id=reference.agent_id,
            reason=(
                "Opaque runtime inputs changed; "
                "lexical overlap cannot price this change."
            ),
        )
    anchor = next(
        (item for item in older if item.agent_id == reference.lineage_agent_id), None
    )
    if not same_runtime and anchor is not None:
        anchor_similarity, _ = content_similarity(
            profile.get("fingerprint"), anchor.profile.get("fingerprint")
        )
        if anchor_similarity < settings.small_delta_jaccard:
            return AttemptGuidance(
                **base,
                classification="material_new_work",
                reference_agent_id=reference.agent_id,
                reason=(
                    "Accumulated changes are material relative to the exact "
                    "lineage anchor; start a fresh review."
                ),
            )
    feedback_available = (
        reference.feedback_at is not None and utc(reference.feedback_at) <= current
    )
    kind: AttemptKind
    if feedback_available and reference.outcome == "infrastructure" and same_runtime:
        kind = "infrastructure_retry"
    elif same_runtime and profile["packaging_hash"] != reference.profile.get(
        "packaging_hash"
    ):
        kind = "packaging_only_repair"
    elif similarity >= settings.small_delta_jaccard:
        kind = "small_source_delta"
    else:
        return AttemptGuidance(
            **base,
            classification="material_new_work",
            reference_agent_id=reference.agent_id,
            reason=(
                "Material source change; no earlier verdict or "
                "low-information budget is inherited."
            ),
        )

    lineage = reference.lineage_agent_id
    cutoff = current - timedelta(seconds=settings.window_seconds)
    family = [
        item
        for item in older
        if item.lineage_agent_id == lineage and utc(item.submitted_at) >= cutoff
    ]
    completed = [
        item
        for item in family
        if item.feedback_at is not None
        and utc(item.feedback_at) <= current
        and item.outcome in {"completed", "repairable"}
    ]
    low_information = [
        item
        for item in completed
        if item.classification in {"packaging_only_repair", "small_source_delta"}
        and not item.fast_repair
    ]
    pending = [
        item
        for item in family
        if item.outcome == "pending"
        and item.classification in {"packaging_only_repair", "small_source_delta"}
        and not item.fast_repair
    ]
    repair_count = sum(
        item.fast_repair for item in family if item.outcome != "infrastructure"
    )
    repair_remaining = max(0, settings.fast_repair_limit - repair_count)
    repairable = (
        feedback_available
        and reference.outcome == "repairable"
        and reference.reason_code in REPAIR_REASONS
    )
    fast_repair = kind != "infrastructure_retry" and repairable and repair_remaining > 0
    retry_at = None
    clear_feedback = [item for item in completed if item.feedback_at is not None]
    if (
        kind != "infrastructure_retry"
        and not fast_repair
        and clear_feedback
        and len(low_information) + len(pending) >= settings.low_information_limit
    ):
        last_feedback = max(
            utc(item.feedback_at)
            for item in clear_feedback
            if item.feedback_at is not None
        )
        if pending:
            last_feedback = max(
                last_feedback, max(utc(item.submitted_at) for item in pending)
            )
        deadline = last_feedback + timedelta(seconds=settings.cooldown_seconds)
        if deadline > current:
            retry_at = deadline
    reason = (
        "Platform, screener, or validator failure consumes no attempt budget."
        if kind == "infrastructure_retry"
        else (
            "A bounded retry is available for the previously reported "
            "build/runtime repair."
        )
        if fast_repair
        else (
            "Repeated low-information attempts must wait after completed "
            "feedback; request operator review to appeal."
        )
        if retry_at is not None
        else "This is an admission timing signal, not a misconduct verdict."
    )
    return AttemptGuidance(
        **base,
        classification=kind,
        reference_agent_id=reference.agent_id,
        lineage_agent_id=lineage,
        completed_low_information_attempts=len(low_information),
        reserved_low_information_attempts=len(pending),
        fast_repair=fast_repair,
        fast_repairs_remaining=repair_remaining - int(fast_repair),
        retry_at=retry_at,
        reason=reason,
    )
