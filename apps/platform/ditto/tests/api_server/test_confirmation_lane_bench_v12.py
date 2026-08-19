"""The confirmation lane, exercised as one unit on a post-v9 epoch.

The lane has roughly eight independent boundaries, each of which carried its
own copy of "bench 9". Every previous repair fixed the boundary that was
visible, the layer above started accepting the new epoch, and the next layer
down still refused -- so the lane looked repaired and stayed dead. These tests
walk the epoch through the profile, the frozen checksums, the rebuilt evidence
root, and the ranking scalar, so widening any single boundary alone cannot make
them pass.

``ditto_screening_protocol.bench_v9.CONFIRMATION_BENCH_VERSIONS`` is the source
of the epoch set; the parametrization reads it rather than restating it, so a
future epoch is covered the moment the evidence alias carries it forward.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ditto.api_models.confirmation_bundles import (
    CONFIRMATION_BENCH_VERSIONS,
    ConfirmationBundleMode,
)
from ditto.api_server.confirmation_evidence import (
    ConfirmationEvidenceError,
    rebuild_confirmation_evidence,
)
from ditto.db.queries.score_ranking import official_composites
from ditto.tests.confirmation_evidence_fixtures import (
    ARTIFACT_SHA256,
    unsigned_report,
    verification_profile,
)


@dataclass(frozen=True)
class _FinalRow:
    """Minimal structural stand-in for the ranking module's row protocol."""

    agent_id: UUID
    miner_hotkey: str
    first_seen: datetime
    composite: float
    bench_version: int
    v9_confirmation: dict[str, int] | None = None
    emission_owner_root: str | None = None
    eligible: bool = True


_DEEP_HISTORY_EPOCHS = [
    version for version in CONFIRMATION_BENCH_VERSIONS if version != 9
]
_SETTINGS_CHECKSUM = "c" * 64
_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _verified(bench_version: int):
    profile = verification_profile(bench_version)
    return rebuild_confirmation_evidence(
        unsigned_report(bench_version=bench_version),
        artifact_sha256=ARTIFACT_SHA256,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        settings_revision=3,
        settings_checksum=_SETTINGS_CHECKSUM,
        retest_generation=0,
        mode=ConfirmationBundleMode.SHADOW,
        profile=profile,
    )


@pytest.mark.parametrize("bench_version", CONFIRMATION_BENCH_VERSIONS)
def test_every_confirmable_epoch_rebuilds_its_own_signed_root(
    bench_version: int,
) -> None:
    """The root records the run's epoch, not a constant.

    Platform is the only computer of ``evidence_sha256``, and the validator
    rebuilds this exact root and compares digests before it will sign. The two
    sides are therefore bit-for-bit paired; both now read the epoch off the
    same leased execution profile.
    """
    verified = _verified(bench_version)

    assert verified.root.bench_version == bench_version
    assert verified.root.longmemeval.evidence.bench_version == bench_version
    assert verified.root.inference_ablation.evidence.bench_version == bench_version
    assert verified.root.embedding_ablation.evidence.bench_version == bench_version


def test_the_epoch_is_inside_the_evidence_digest() -> None:
    """A receipt from one epoch can never be replayed as another's."""
    digests = {
        version: _verified(version).evidence_sha256
        for version in CONFIRMATION_BENCH_VERSIONS
    }

    assert len(set(digests.values())) == len(digests)


@pytest.mark.parametrize("bench_version", _DEEP_HISTORY_EPOCHS)
def test_deep_history_profiles_have_their_own_frozen_checksums(
    bench_version: int,
) -> None:
    """Python and Go must derive the same LongMem and ablation checksums.

    Go marshals its real ``BenchVersion`` (and the two ``omitempty``
    deep-history floors) into both digests. Pinning 9 on the Python side made
    the languages agree only for v9; for any later epoch the validator claimed
    the ticket and then died on "confirmation LongMem profile checksum
    mismatch". The Go half of this agreement is asserted directly against
    Platform-minted bytes in
    ``services/dittobench-api/cmd/dittobench-api/confirmation_profile_agreement_test.go``.
    """
    v9 = verification_profile(9)
    later = verification_profile(bench_version)

    assert later.bench_version == bench_version
    assert later.longmem_min_history_sessions >= 55
    assert later.longmem_min_history_bytes >= 400_000
    assert later.longmem_checksum() != v9.longmem_checksum()
    assert later.ablation_checksum() != v9.ablation_checksum()
    assert later.checksum() != v9.checksum()
    # The payload is what Go re-digests as the outer profile checksum, so the
    # additive keys must be present for a deep-history epoch and absent for v9.
    payload = later.payload()
    assert payload["bench_version"] == bench_version
    assert "longmem_min_history_sessions" in payload
    assert "bench_version" not in v9.payload()
    assert "longmem_min_history_sessions" not in v9.payload()


def test_a_v9_profile_declaring_deep_history_floors_is_refused() -> None:
    contradictory = replace(verification_profile(9), longmem_min_history_sessions=55)

    with pytest.raises(ConfirmationEvidenceError, match="does not define"):
        contradictory.validate()


@pytest.mark.parametrize("bench_version", CONFIRMATION_BENCH_VERSIONS)
def test_a_confirmed_row_ranks_on_its_confirmed_quality(
    bench_version: int,
) -> None:
    """Ranking must not fall back to the unconfirmed composite.

    ``official_composites`` is the one score every ranking surface cuts on.
    Its SQL twin already generalized over confirmation-capable epochs, so a
    literal here made the ledger say "confirmed" while the Python ranking said
    "provisional" -- silently discarding ``applied_factor_bps``, which is the
    entire point of the anti-emulation lane.
    """
    row = _FinalRow(
        agent_id=UUID("3" * 32),
        miner_hotkey="5" + "a" * 47,
        first_seen=_BASE,
        composite=0.8,
        bench_version=bench_version,
        v9_confirmation={"full_effective_micros": 750_000},
    )

    scores = official_composites(
        [row],
        quorum={row.agent_id: [1.0, 1.0, 1.0]},
        completed_waves={row.agent_id: {10: 1.0}},
        continual_mean_active=True,
    )

    assert scores[row.agent_id] == pytest.approx(0.75)
