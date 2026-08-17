"""Anti-copy moderation gate for the score-write path.

SN118 artifacts are downloadable, so the central threat is copying: download
the current best harness and resubmit it (verbatim or lightly tweaked) to
dethrone the original. The KOTH+ATH fold is *supposed* to defeat a verbatim copy
by tying the incumbent and never clearing the fixed composite-point margin, with
first-seen protecting the original — but on bench_version 7 a verbatim copy does
not tie: two normalized-identical artifacts measured 0.0768 apart (see
``_DEFAULT_SCORE_TOL``), so it can clear the margin in either direction on
benchmark noise alone. That makes this gate, not the fold, the load-bearing
defense. This gate adds cheap signals against a *lightly-tweaked*
copy that nudges its score just past the incumbent: such a submission matches on
the *lexical* fingerprint channel — a
reference-aware sketch of the tarball text (:mod:`ditto.api_server.fingerprint`,
official starter-kit scaffolding subtracted before sketching), which survives
reindent/reformat/localized-edit and junk-file padding, compared by Jaccard
(edit-in-place) or containment (padded copy). The *structural* sketch of the
crate's AST shape (computed by dittobench, arriving on the score report) and the
prompt sketch corroborate a hold's audit reason but never trigger one: both are
whole-crate, so they saturate between independent starter-kit derivatives until
they are reference-aware too. Tarball-size proximity is a fallback for rows with
no comparable fingerprints only.

The gate's rules answer "did this artifact arrive carrying another owner's
custom surface". They do not, on their own, answer whether the miner had to
*copy* to get it — and on SN118 the subnet itself is a lawful source. Kings'
source is published on an unauthenticated route once the operator-set embargo
lapses (``disclosure``/``embargo_hours``, see
:mod:`ditto.db.queries.artifact_release_settings`), the subnet owner has
confirmed that building on a released agent is permitted, and a published
codebase spreads across unrelated miners within days. Matching content the
platform handed out is therefore not evidence of anything, so a fired signal is
*withdrawn* — recorded, not held — when the candidate had no opportunity to copy
the reference. See :class:`NoCopyOpportunity` and the withdrawal section of
:func:`evaluate_duplicate_signals`. Copying an artifact still inside its embargo
window is untouched: that is precisely what this gate is for.

This is **moderation, not weight logic** — it decides only whether a suspicious
high-scorer is held in ``ath_pending_review`` for human review (see
:func:`ditto.db.queries.agents.resolve_review`), never who the champion is. The
KOTH fold itself lives in the validator. The function is pure and deterministic
so re-scoring the same agent yields the same verdict.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ditto.api_server.fingerprint import content_similarity

if TYPE_CHECKING:
    from uuid import UUID

    from ditto.db.queries.scores import LedgerRow, RejectedArtifact

# Score-proximity tolerance for the two rules that still use one: the
# *inconclusive* branch of rule 2 and the legacy size fallback (rule 3). It is
# deliberately NOT used by the lexical near-duplicate match itself — see rule 2.
#
# History and why it no longer gates the fingerprint match. This started as
# "a challenger scoring within this of the incumbent it surpasses is a hair past",
# sized as ~3σ of between-seed composite noise on the DittoBench v2 target of
# σ ≤ 0.01 (BENCHMARK-V2 §6.2, B8; it was 0.02 for v1). That noise model is
# falsified by production. On bench_version 7, two *normalized-identical*
# artifacts — ditto-v7 (69ad... uploaded 2026-07-25T12:26Z, composite 0.914322)
# and dittoTop-v0 (uploaded 50 minutes later, composite 0.837497) — scored
# 0.0768 apart, with per-agent composite_stderr in the 0.016–0.020 band rather
# than ≤ 0.01. Identical code therefore routinely lands ~2.5x outside this
# window, so using score proximity as a precondition on fingerprint evidence made
# detection probability *inversely* proportional to how lucky a copy got: the
# luckier the re-roll, the further the score drifted, the less likely the copy was
# ever compared. dittoTop-v0 was never held for exactly this reason.
#
# Where it survives, score proximity is a co-signal on weak or absent evidence
# (archive size, or an uncomparable fingerprint pair), not a gate on strong
# evidence — see the rule 2 inconclusive branch and rule 3. Widening it there
# would only add holds whose sole other signal is tarball size, so it stays at
# the historical value pending the KOTH-parameter validation task.
_DEFAULT_SCORE_TOL = 0.03
# A tweaked copy differs from the original by at most a few edited lines, so its
# gzipped tarball size barely moves. 8 KiB comfortably covers small edits.
_DEFAULT_SIZE_TOL = 8192
# Content-fingerprint (shingle-sketch) thresholds. Jaccard catches a copy edited
# in place; containment (overlap coefficient) catches one padded with junk files
# to dilute Jaccard. Containment's bar is higher because it is the more
# false-positive-prone of the two on shared reference-harness scaffolding — only a
# near-total subset should trip it. Both are paired with score proximity below, and
# a hold only routes to *human* review, so these are deliberately conservative
# signals, not an autoban. They want tuning against a real score/similarity corpus
# (see the subnet's KOTH-parameter validation task).
_DEFAULT_JACCARD_TOL = 0.75
_DEFAULT_CONTAINMENT_TOL = 0.95
# Resubmission thresholds (:func:`evaluate_rejected_resubmission`). Deliberately
# NOT the copy thresholds above, which is the one thing the rule got wrong when
# it shipped: it reused them "so one lexical bar governs the codebase", but the
# two rules ask their question of different populations. The copy bar is set
# against *independent miners*, whose reference-subtracted residuals overlap at a
# 99.9th percentile of 0.625 Jaccard (see the measured distributions in
# :mod:`ditto.db.queries.similarity_grouping`), so 0.75 there means "this
# artifact carries somebody else's custom surface" and is genuinely rare. The
# resubmission rule compares an artifact against *the same owner's own rejected
# version*, where near-total overlap is the definition of the population rather
# than evidence about it: a miner who deletes the cited code and re-uploads is
# still ~95% their own prior artifact. At 0.75 the rule therefore fires on every
# remediation attempt, which is what production showed — see the corpus in
# :func:`evaluate_rejected_resubmission`.
#
# 0.98 is the near-identity bar the rule's actual question wants ("is this the
# artifact we rejected, re-uploaded with cosmetic edits") and it is set from the
# 2026-08-17 corpus: every hold an operator cleared measured at or below 0.977
# Jaccard, and the two that were materially unchanged measured 0.992 and 1.000.
_DEFAULT_RESUBMISSION_JACCARD_TOL = 0.98
# Containment sits at the same bar rather than above it, unlike the copy pair.
# The copy rule keeps containment looser because there it is the weaker of the
# two measures; here it is direction-guarded (see
# :func:`_resubmission_lexical_match`) so only the padding direction reaches it,
# and that direction should be near-total to mean anything.
_DEFAULT_RESUBMISSION_CONTAINMENT_TOL = 0.98
# Structural (AST) thresholds gate the ADVISORY structural annotation on a hold
# (see _structural_note): higher than the lexical ones because the structural
# sketch discards identifiers + formatting, so two independent crates built on the
# same reference harness share far more of their parse-tree shape than their text.
_DEFAULT_STRUCTURAL_JACCARD_TOL = 0.85
_DEFAULT_STRUCTURAL_CONTAINMENT_TOL = 0.98
# prompt overlap above which a hold's audit reason notes the corroboration.
# Advisory only: the prompt sketch (``compute_prompt_fingerprint``) does *not* hold
# an agent on its own — honest agents on the same reference harness share
# scaffolding prompts (the convergent case in ``ditto.anticopy.calibration`` scores
# ~0.8 there), and the signals orthogonal to that convergence (behavioral /
# code-embedding) are not built yet. So the prompt fingerprint runs in shadow mode:
# it enriches the moderator's audit trail on a hold another rule already fired, and
# its per-agent sketch is stored for calibration, but the active fusion hold waits
# on an orthogonal signal (the code-embedding or behavioral channel).
_PROMPT_ADVISORY_TOL = 0.5


@dataclass(frozen=True)
class PublicSourceRelease:
    """One earlier artifact whose source the subnet itself published.

    ``available_at`` is the instant the platform began serving that artifact's
    tarball on the unauthenticated public route — i.e. the moment its content
    stopped being anyone's secret. The caller derives it (see
    :func:`ditto.db.queries.artifact_release.list_public_source_releases`); the
    gate only compares it against the candidate's upload time, so this module
    stays pure and has no opinion about how release is earned.
    """

    agent_id: UUID
    available_at: datetime


@dataclass(frozen=True)
class NoCopyOpportunity:
    """Why a fired copy signal was withdrawn instead of opening a hold.

    Recorded rather than discarded: the *match* is still real telemetry for
    leaderboard-integrity questions, and an operator asking "did anyone
    reproduce this artifact" must still be able to find the answer. What the
    match is not, in these two cases, is evidence that this miner took anything
    from the named reference.
    """

    kind: str
    """Why the signal carries no accusation:

    ``public_release``
        the content was already published by the subnet before this upload;
    ``self_lineage``
        the shared surface predates *the matched reference* in this owner's own
        history, so this owner held it before the reference existed;
    ``self_origin``
        this owner holds the **earliest** artifact in the whole matching
        cluster, so no member of that cluster can be where the surface came
        from. Strictly stronger than ``self_lineage``: it is an argument about
        the entire cluster rather than about one pair.
    """

    matched_agent_id: UUID
    """The earlier submission the hold would have named as ``duplicate_of``."""

    source_agent_id: UUID
    """The artifact that accounts for the shared content lawfully: the published
    one, or this owner's own earlier generation."""

    source_available_at: datetime | None
    """When ``source_agent_id`` became publicly downloadable (``public_release``
    only; ``None`` for the two owner-history kinds)."""

    signal: str
    """Which copy rule fired: ``exact_byte``, ``normalized_source``, ``lexical``
    or ``size_fallback``."""

    detail: str
    """Operator-readable one-line explanation, safe for the public audit feed."""


@dataclass(frozen=True)
class ReviewDecision:
    """Outcome of the anti-copy gate for one just-scored agent.

    ``held`` routes the agent to ``ath_pending_review`` with ``duplicate_of``
    (the earlier submission it appears to copy) and ``reason`` recorded as the
    moderation audit trail. ``held=False`` lets the normal ``evaluating ->
    scored`` transition proceed.

    ``no_copy_opportunity`` is populated when a copy signal fired but was
    withdrawn because the candidate could not have taken the shared content from
    the named reference. It never accompanies ``held=True``, and it deliberately
    leaves ``duplicate_of`` / ``reason`` unset: those two columns are the *hold*
    record and render on the public board as an accusation, which is exactly
    what a withdrawn signal must not produce. The caller appends the annotation
    to the immutable audit chain instead.
    """

    held: bool
    duplicate_of: UUID | None = None
    reason: str | None = None
    no_copy_opportunity: NoCopyOpportunity | None = None


_NOT_HELD = ReviewDecision(held=False)


def _utc(dt: datetime) -> datetime:
    """Normalize database timestamps for deterministic chronology comparisons."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _fingerprint_versions_incompatible(a: dict | None, b: dict | None) -> bool:
    """Whether two present sketches use incomparable algorithms or corpora."""
    return bool(
        a
        and b
        and a.get("v") is not None
        and b.get("v") is not None
        and (a.get("v") != b.get("v") or a.get("corpus") != b.get("corpus"))
    )


def _fingerprint_corpora_incompatible(a: dict | None, b: dict | None) -> bool:
    """Whether equal-version sketches subtract different reference corpora.

    A corpus mismatch is transition metadata, not evidence that either submission
    copied the other.  It commonly occurs while a refreshed official starter-kit
    corpus is being backfilled.  The pair must be skipped rather than sent to ATH
    review; exact-byte comparison still runs before this check and same-corpus
    lexical comparisons remain active.
    """
    return bool(
        a
        and b
        and a.get("v") is not None
        and a.get("v") == b.get("v")
        and a.get("corpus") != b.get("corpus")
    )


def _structural_note(
    structural_fingerprint: dict | None,
    e: LedgerRow,
    *,
    jaccard_tol: float,
    containment_tol: float,
) -> str:
    """Advisory structural-overlap suffix for a hold's audit reason.

    The structural (AST-shape) sketch arrives from dittobench computed over the
    WHOLE crate — ``astfp`` performs no reference subtraction — so on
    starter-kit-derived submissions it saturates between independent miners
    exactly like the pre-reference lexical channel did (measured on the audited
    corpus: 12 of the 66 current holds sit at/above the structural thresholds,
    concentrated in the smallest-edit submissions, which the lexical
    residual-cardinality floor deliberately routes past the lexical check).
    Until dittobench ships reference-subtracted structural sketches this
    channel corroborates a hold that already fired; it never triggers one.
    """
    j, c = content_similarity(structural_fingerprint, e.structural_fingerprint)
    if j >= jaccard_tol or c >= containment_tol:
        return f"; structural jaccard {j:.3f}, containment {c:.3f}"
    return ""


def _prompt_note(prompt_fingerprint: dict | None, e: LedgerRow) -> str:
    """Shadow-mode the prompt fingerprint suffix for a hold's audit reason.

    Returns ``"; prompt jaccard X.XXX"`` when the just-scored agent and the matched
    agent ``e`` both carry a prompt sketch overlapping at or above
    ``_PROMPT_ADVISORY_TOL``, else ``""``. Observability only — this never affects
    whether the agent is held.
    """
    j, c = content_similarity(prompt_fingerprint, e.prompt_fingerprint)
    if max(j, c) >= _PROMPT_ADVISORY_TOL:
        return f"; prompt jaccard {j:.3f}, containment {c:.3f}"
    return ""


def _lexical_strength(
    a: dict | None,
    b: dict | None,
    *,
    jaccard_tol: float,
    containment_tol: float,
) -> float:
    """Threshold-normalized closeness of the lexical channel for one pair.

    Rule 2 fires on ``jaccard >= jaccard_tol OR containment >= containment_tol``,
    two measures on different scales. Dividing each by its own bar and taking
    the larger collapses that disjunction into one comparable scalar: ``>= 1.0``
    is exactly "this pair would trigger", and between two pairs the larger value
    is the closer match *in the terms the rule itself uses*. Comparing raw
    Jaccard against raw containment would not be meaningful; comparing the two
    normalized margins is.

    Returns ``0.0`` for an uncomparable pair (missing / cross-version /
    cross-corpus sketch), which is what :func:`content_similarity` already
    reports and what "no evidence" should score.
    """
    j, c = content_similarity(a, b)
    return max(j / jaccard_tol, c / containment_tol)


def _residual_cardinality(sketch: dict | None) -> int | None:
    """True residual shingle count a lexical sketch reports, or ``None``.

    ``compute_content_fingerprint`` stores the exact post-reference cardinality
    alongside the bottom-``k`` sample precisely so a consumer can reason about
    the two sets' *sizes* without the sketch's estimation error — the sample is
    an estimate, ``card`` is not.
    """
    if not sketch:
        return None
    card = sketch.get("card")
    if card is None:
        return None
    return int(card)


def _resubmission_lexical_match(
    candidate: dict | None,
    rejected: dict | None,
    *,
    jaccard_tol: float,
    containment_tol: float,
) -> tuple[float, float] | None:
    """``(jaccard, containment)`` when the resubmission rule's lexical channel fires.

    Returns ``None`` when it does not, so the caller never has to re-derive which
    channel spoke.

    Two things separate this from :func:`_lexical_strength`, and both come from
    the resubmission question being asked of a different population — the same
    owner's own rejected artifact — than the copy question.

    **Near-identity, not overlap.** See ``_DEFAULT_RESUBMISSION_JACCARD_TOL``.

    **Containment is direction-guarded.** Containment is ``|A n B| / min(|A|,
    |B|)``, i.e. "is the smaller residual a subset of the larger". In the copy
    gate the smaller side is the *original* and the larger is a copy padded with
    junk to dilute Jaccard, so containment catches an attack. Here the direction
    inverts and takes the meaning with it: an honest remediation *removes* the
    cited code, which makes the candidate the smaller residual and a near-subset
    of the artifact it was cut from — so containment goes to ~1.0 *because* the
    miner complied. It is not weak evidence in that direction, it is evidence
    pointing backwards, and it produced the rule's worst false positives
    (Crown-v11-v3 measured containment 0.996 on a 574-line deletion an operator
    then cleared as a textbook remediation). So containment may fire only when
    the candidate is the same size or larger — the padded-re-upload direction,
    where every shingle of the rejected artifact survived and bulk was added on
    top. When either sketch omits ``card`` the direction is unknown and the
    channel stays silent; Jaccard still covers the pair.
    """
    j, c = content_similarity(candidate, rejected)
    if j >= jaccard_tol:
        return (j, c)
    candidate_card = _residual_cardinality(candidate)
    rejected_card = _residual_cardinality(rejected)
    if (
        candidate_card is not None
        and rejected_card is not None
        and candidate_card >= rejected_card
        and c >= containment_tol
    ):
        return (j, c)
    return None


def _size_proximity(a: int | None, b: int | None, *, size_tol: int) -> float:
    """Threshold-normalized closeness of the legacy size fallback for one pair.

    Same convention as :func:`_lexical_strength` — ``>= 1.0`` is exactly "this
    pair would trigger rule 3's size test", and larger is closer — expressed as
    ``size_tol / delta`` because for size the *smaller* delta is the closer
    match. A zero delta is clamped to one byte rather than dividing by zero; the
    resulting large finite number orders correctly against every other pair,
    which is all the comparison needs.
    """
    if a is None or b is None:
        return 0.0
    return size_tol / max(abs(a - b), 1)


def _lexical_scorer(
    sketch: dict | None, *, jaccard_tol: float, containment_tol: float
) -> Callable[[LedgerRow], float]:
    """Bind one lexical sketch, scoring any ledger row against it.

    A factory rather than an inline closure because the bound sketch changes per
    reference inside the rule loop, and a lambda over a loop variable is a bug
    waiting for a refactor to reorder the calls.
    """

    def score(row: LedgerRow) -> float:
        return _lexical_strength(
            sketch,
            row.content_fingerprint,
            jaccard_tol=jaccard_tol,
            containment_tol=containment_tol,
        )

    return score


def _size_scorer(
    size_bytes: int | None, *, size_tol: int
) -> Callable[[LedgerRow], float]:
    """Bind one tarball size, scoring any ledger row's size against it."""

    def score(row: LedgerRow) -> float:
        return _size_proximity(size_bytes, row.size_bytes, size_tol=size_tol)

    return score


def evaluate_duplicate_signals(
    *,
    agent_id: UUID,
    miner_hotkey: str,
    submitted_at: datetime,
    sha256: str,
    composite: float,
    size_bytes: int | None,
    eligible: Sequence[LedgerRow],
    eligible_history: Sequence[LedgerRow] = (),
    normalized_source_hash: str | None = None,
    content_fingerprint: dict | None = None,
    structural_fingerprint: dict | None = None,
    prompt_fingerprint: dict | None = None,
    miner_coldkey: str | None = None,
    linked_owner_hotkeys: frozenset[str] = frozenset(),
    public_source_releases: Sequence[PublicSourceRelease] = (),
    score_tol: float = _DEFAULT_SCORE_TOL,
    size_tol: int = _DEFAULT_SIZE_TOL,
    jaccard_tol: float = _DEFAULT_JACCARD_TOL,
    containment_tol: float = _DEFAULT_CONTAINMENT_TOL,
    structural_jaccard_tol: float = _DEFAULT_STRUCTURAL_JACCARD_TOL,
    structural_containment_tol: float = _DEFAULT_STRUCTURAL_CONTAINMENT_TOL,
) -> ReviewDecision:
    """Decide whether a just-scored agent should be held for copy review.

    Copying is only a threat *across* owners, so every rule ignores the agent's
    own submissions and other agents paid for by the same coldkey (an owner
    iterating through different names or hotkeys is not a copier). Legacy rows
    without payment provenance still fall back to same-hotkey exclusion.

    ``linked_owner_hotkeys`` extends that same principle to an owner who
    rotated keys, which coldkey equality cannot see. Each entry is a hotkey
    cryptographically proven to be the same operator as this miner: both
    endpoints signed the link, each with its own hotkey or the coldkey bound to
    it (see :mod:`ditto.api_server.attestation`). All copy rules skip those
    attested predecessors, including byte-identical and canonicalized-source
    matches. Copy moderation answers whether one owner took another owner's
    work; a cryptographically proven same owner cannot plagiarize itself.
    Emission-slot deduplication is a separate policy and must not be enforced by
    holding legitimate same-owner generations in copy review.

    Originality attribution also follows upload chronology,
    not score-finalization order: normalized-source and near-duplicate rules compare
    only with another miner's strictly earlier submission. Equal timestamps use the
    UUID as a deterministic tie-break. Held iff:

    1. **Exact copy** — same ``sha256``. Byte-identical resubmission.
    1b. **Exact repack** — same ``normalized_source_hash``: the same source
       canonicalized (comments/whitespace stripped, files sorted), so a reformat /
       re-comment / file rename+reorder repack that changes ``sha256`` still
       matches. Like rule 1, held on the hash equality alone — no score proximity,
       because an exact-source match is copy evidence regardless of the score it
       happened to land.
    2. **Near-duplicate lexical fingerprint** — the lexical channel
       (``content_fingerprint``, reference-aware) at least ``jaccard_tol`` Jaccard
       or ``containment_tol`` contained — survives re-indent / reformat /
       localized edits / junk-file padding. Like rules 1 and 1b this is held on the
       fingerprint alone: **no score proximity**. Score similarity is not evidence
       of copying and score dissimilarity is not evidence of independence — see
       ``_DEFAULT_SCORE_TOL`` for the production measurement that retired the
       precondition. Structural (``structural_fingerprint``, whole-crate AST from
       dittobench — measured at/above its thresholds for 12 of the 66 audited
       holds, concentrated in the smallest-edit submissions) and prompt overlap
       annotate the hold's audit reason as corroboration; neither triggers until
       reference-aware. When the two lexical sketches are *uncomparable* (different
       algorithm version) the pair is inconclusive rather than matched, and that
       branch keeps ``score_tol`` — there the tolerance triages pairs with no
       evidence either way instead of gating evidence that already exists.

    3. **Size near-duplicate fallback** — composites within ``score_tol`` and
       tarball sizes within ``size_tol``, but only when neither lexical nor
       structural fingerprints are comparable. A valid negative fingerprint is
       evidence of distinct content and must not be overridden by similar archive
       size. The fallback remains for legacy or unreadable artifacts, and keeps
       score proximity because archive size alone is too weak to hold on.

    Rules 2 and 3 check *every earlier* other-miner eligible agent, in either score
    direction, so a genuine unrelated agent scoring in between cannot mask the
    copy. A genuine improvement is never held by rule 2 as long as its
    miner-authored residual is its own: after reference subtraction, clearing
    ``jaccard_tol`` / ``containment_tol`` means sharing another miner's custom
    surface, not the shared starter kit. Rule 3 additionally requires a different
    size or a comparable fingerprint. Pure + deterministic: candidates are ordered
    by upload chronology, so the reported ``duplicate_of`` is the oldest matching
    submission.

    ``prompt_fingerprint`` participates in **shadow mode only**: when a hold
    fires for another reason, a high prompt overlap with the matched agent is
    appended to the audit reason (``_PROMPT_ADVISORY_TOL``). It never creates a hold
    on its own — a prompt match alone is not copy evidence, because honest agents on
    the same reference harness share scaffolding prompts. The active prompt-fusion
    hold is deferred until an orthogonal-to-convergence signal (behavioral /
    code-embedding) exists to corroborate it.

    **No-copy-opportunity withdrawal.** Every rule above answers "did this
    submission arrive carrying another owner's custom surface". None of them
    answers the prior question: *could this miner have obtained that surface
    without copying the named reference*. Two ways it can, both of which the
    subnet creates itself, and in both the hold names a miner who did nothing
    wrong:

    * ``public_source_releases`` — SN118 publishes the source of chain-confirmed
      kings once the operator-set embargo lapses (see
      :mod:`ditto.db.queries.artifact_release_settings`). Content the subnet
      itself put on an unauthenticated download route before this artifact was
      uploaded is not a secret this miner stole, and the subnet owner has
      confirmed building on a released agent is permitted. A published artifact
      withdraws the hold only when it accounts for the match **at least as
      well** as the reference does, in both directions (see
      :func:`_withdraw_ranked`). A copy that resembles a still-embargoed
      artifact more closely than any published one, or that shares with the
      reference something no published artifact contains, took something that
      was never published, and is still held.
    * the candidate's own earlier generation — when a published
      codebase spreads, the *nearest* earlier match is frequently the newest
      recipient rather than the originator, so the originator's next
      generation gets held against its own downstream copy. An owner row that
      predates the reference and matches at least as strongly is proof the
      shared surface was in this owner's hands before the reference existed,
      which no copy of the reference could produce.

    **Earliest-source attribution.** A signal that survives every withdrawal is
    a real finding, but it still has to name the right party. ``eligible`` holds
    one representative row per owner, so the reference a rule fires on is the
    nearest *visible* earlier artifact — which, on a codebase that has spread,
    is routinely an intermediate recipient rather than the source. Naming it
    turns a true finding into a false accusation against whoever is merely
    closest in the ledger. red-dragon v18 was held as a duplicate of astrion-v9
    v1, an owner with two submissions total, while red-dragon's own v17 had
    carried the complete shared module set two days before astrion existed.

    ``eligible_history`` closes that gap. It carries the *full* set of earlier
    finalized generations — every owner's, not just each owner's representative
    row — and is used for attribution and alibi **only**:

    * no rule triggers on it, so this parameter can never open a hold that
      ``eligible`` alone would not have opened;
    * ``self_lineage`` withdrawal sees the owner's whole history rather than the
      single representative row that survived owner reduction, which is what
      makes the alibi work for an owner whose best generation is not its
      earliest;
    * before a surviving hold is returned, ``duplicate_of`` is re-pointed at the
      **earliest** artifact in the matching cluster — the members of which are
      scored against the *candidate* on the same threshold-normalized scale the
      rule triggered on. If that earliest member belongs to this owner, nothing
      in the cluster can be the source and the signal is withdrawn as
      ``self_origin`` instead.

    Callers that pass only ``eligible`` get exactly the pre-existing behaviour:
    the cluster is then the representative rows alone, whose earliest member is
    the row the rule already selected.

    A withdrawal never suppresses evidence: the decision carries
    :class:`NoCopyOpportunity` so the caller records the match on the immutable
    audit chain. Withdrawal is per-pair — a candidate can be exempt against one
    reference and still held against another it matches more closely — and every
    remaining reference is checked before the withdrawal is returned. Rule 2's
    *inconclusive* branch is deliberately untouched: it fires on the absence of a
    comparison, so there is no match for a published artifact to account for.
    """
    other_miners = [
        e
        for e in eligible
        if e.agent_id != agent_id
        and e.miner_hotkey != miner_hotkey
        and not (miner_coldkey is not None and e.miner_coldkey == miner_coldkey)
    ]
    submitted_key = (_utc(submitted_at), agent_id.int)
    earlier_others = sorted(
        (
            e
            for e in other_miners
            if (_utc(e.first_seen), e.agent_id.int) < submitted_key
        ),
        key=lambda e: (_utc(e.first_seen), e.agent_id.int),
    )
    # Every copy rule drops hotkeys cryptographically proven to be the same
    # operator. Emission-slot allocation is enforced elsewhere; this gate only
    # answers whether a submission copied a different owner.
    earlier_unattested = (
        earlier_others
        if not linked_owner_hotkeys
        else [e for e in earlier_others if e.miner_hotkey not in linked_owner_hotkeys]
    )

    # Artifacts the subnet itself had already published when this one was
    # uploaded. Ordered oldest-first so the withdrawal names the earliest
    # publication that accounts for the match, which is the one a miner would
    # have found first and the one that makes the record easiest to audit.
    #
    # Deliberately NOT restricted to other owners: the question is whether the
    # content was public, and public is public whoever published it.
    released_at: dict[UUID, datetime] = {
        release.agent_id: _utc(release.available_at)
        for release in public_source_releases
    }
    published_before = sorted(
        (
            e
            for e in eligible
            if e.agent_id != agent_id
            and (available := released_at.get(e.agent_id)) is not None
            and available <= _utc(submitted_at)
        ),
        key=lambda e: (released_at[e.agent_id], e.agent_id.int),
    )
    # Every earlier finalized generation the caller could show us, deduplicated
    # by agent id: the owner-reduced ledger plus, when the caller loaded it, the
    # full per-generation history. Attribution and alibi read this; no rule
    # triggers on it, so a caller that passes no history keeps today's decisions
    # exactly.
    history_by_id: dict[UUID, LedgerRow] = {}
    for row in (*eligible, *eligible_history):
        if row.agent_id == agent_id:
            continue
        if (_utc(row.first_seen), row.agent_id.int) >= submitted_key:
            continue
        history_by_id.setdefault(row.agent_id, row)
    earlier_history = sorted(
        history_by_id.values(), key=lambda e: (_utc(e.first_seen), e.agent_id.int)
    )

    def _same_owner(e: LedgerRow) -> bool:
        """Whether ``e`` belongs to the operator that submitted the candidate.

        The same three linkage facts the copy rules use to drop a row from the
        *suspect* set — identical hotkey, identical payment-time coldkey, or a
        cryptographically attested key rotation. Payment-coldkey equality is the
        weak one: ``ditto.db.queries.ownership`` is explicit that a shared
        payment coldkey is evidence of common control and not proof of it. It is
        used here anyway, for two reasons. It is the same evidence, read the same
        way, that already excludes a same-coldkey row from being *accused* — so
        declining it here would mean one linkage standard for suspicion and a
        stricter one for exculpation, applied against the same operator. And it
        is fail-open by construction: on this path the only thing owner linkage
        can do is withdraw a hold, and a withdrawal is still written to the
        public audit chain as :class:`NoCopyOpportunity` rather than discarded.
        Over-linking therefore costs a recorded non-accusation; under-linking
        costs a public false accusation against a miner who shipped the code
        first. Linkage is deliberately one hop and never chained through a third
        party's coldkey (see ``MAX_DEPTH`` in that module for why walking further
        over-links).
        """
        return (
            e.miner_hotkey == miner_hotkey
            or (miner_coldkey is not None and e.miner_coldkey == miner_coldkey)
            or e.miner_hotkey in linked_owner_hotkeys
        )

    # This owner's earlier generations. These are the rows the copy rules
    # exclude from `other_miners`. Excluded as *suspects*, they are still
    # admissible as *alibis*: content this owner already shipped cannot have been
    # taken from a submission that did not exist yet.
    own_earlier = [e for e in earlier_history if _same_owner(e)]
    # The first withdrawn signal, returned only if no other reference holds.
    withdrawn: NoCopyOpportunity | None = None

    def _withdraw_equality(
        e: LedgerRow, *, signal: str, matches: str
    ) -> NoCopyOpportunity | None:
        """Withdraw an equality match (rule 1 / 1b) that the subnet published.

        Equality admits no "at least as close" ordering — identical is
        identical — so a published artifact carrying the same hash is a complete
        account of how the candidate came by it. Only the published artifact's
        own hash is consulted; a same-owner alibi is not offered here, because an
        exact-hash match with another owner is the one signal that survives every
        innocent explanation this gate knows how to check.
        """
        for published in published_before:
            same = (
                published.sha256 == sha256
                if signal == "exact_byte"
                else published.normalized_source_hash == normalized_source_hash
            )
            if not same:
                continue
            available = released_at[published.agent_id]
            return NoCopyOpportunity(
                kind="public_release",
                matched_agent_id=e.agent_id,
                source_agent_id=published.agent_id,
                source_available_at=available,
                signal=signal,
                detail=(
                    f"{matches} of agent {e.agent_id}, but agent "
                    f"{published.agent_id} carries the same artifact and was "
                    f"published by the subnet at {available.isoformat()}, "
                    "before this submission was uploaded"
                ),
            )
        return None

    def _withdraw_ranked(
        e: LedgerRow,
        *,
        signal: str,
        strength_to_candidate: Callable[[LedgerRow], float],
        strength_to_reference: Callable[[LedgerRow], float],
        reference_strength: float,
    ) -> NoCopyOpportunity | None:
        """Withdraw a graded match some other row accounts for at least as well.

        Both callables score a row on the same threshold-normalized scale the
        rule triggered on, so every comparison here is against
        ``reference_strength`` — how well the reference itself explains the
        candidate. A candidate row ``s`` withdraws the hold only when *both* hold:

        ``strength_to_candidate(s) >= reference_strength``
            the candidate resembles ``s`` at least as much as it resembles the
            reference, so ``s`` is a plausible place it got the content;
        ``strength_to_reference(s) >= reference_strength``
            ``s`` covers the reference's own surface at least as well as the
            candidate does, so what the candidate shares with the reference was
            already in ``s``.

        The second condition is what the first cannot express. A candidate that
        absorbed *two* codebases — one published, one still private — resembles
        each of them completely, so the first test ties and the published one
        would wrongly excuse the private one. It does not survive the second
        test: a published artifact that shares nothing with the embargoed
        reference explains nothing about why the candidate matches it.

        A published row must additionally trigger against the candidate in its
        own right (``>= 1.0``); an own-lineage row must additionally predate the
        reference, which is the whole of its argument — content shipped before
        the reference existed cannot have come from it.
        """
        for published in published_before:
            to_candidate = strength_to_candidate(published)
            if to_candidate < 1.0 or to_candidate < reference_strength:
                continue
            if strength_to_reference(published) < reference_strength:
                continue
            available = released_at[published.agent_id]
            return NoCopyOpportunity(
                kind="public_release",
                matched_agent_id=e.agent_id,
                source_agent_id=published.agent_id,
                source_available_at=available,
                signal=signal,
                detail=(
                    f"matched agent {e.agent_id}, but subnet-published agent "
                    f"{published.agent_id} (public from {available.isoformat()}) "
                    f"accounts for the shared content at least as well "
                    f"({to_candidate:.3f} vs {reference_strength:.3f} of the "
                    "trigger threshold)"
                ),
            )
        for own in own_earlier:
            if (_utc(own.first_seen), own.agent_id.int) >= (
                _utc(e.first_seen),
                e.agent_id.int,
            ):
                continue
            to_candidate = strength_to_candidate(own)
            if to_candidate < reference_strength:
                continue
            if strength_to_reference(own) < reference_strength:
                continue
            return NoCopyOpportunity(
                kind="self_lineage",
                matched_agent_id=e.agent_id,
                source_agent_id=own.agent_id,
                source_available_at=None,
                signal=signal,
                detail=(
                    f"matched agent {e.agent_id}, but this owner's own earlier "
                    f"agent {own.agent_id} already carried the shared content "
                    f"({to_candidate:.3f} vs {reference_strength:.3f} of the "
                    "trigger threshold) and predates the match, so the shared "
                    "surface originates here"
                ),
            )
        return None

    def _attribute(
        e: LedgerRow,
        *,
        signal: str,
        in_cluster: Callable[[LedgerRow], bool],
        reason_for: Callable[[LedgerRow], str],
    ) -> ReviewDecision | NoCopyOpportunity:
        """Point a surviving hold at the earliest artifact that carries the match.

        ``e`` is the row the rule fired on: the nearest earlier match among the
        one-row-per-owner ledger. That is an artifact of what the caller could
        see, not a finding about who originated anything — on a codebase that has
        spread it is usually an intermediate recipient. ``in_cluster`` re-runs the
        rule's own trigger test over the full history, so the cluster is every
        earlier artifact carrying this match, and the earliest of them is the
        only member that could be the source.

        Cluster membership is deliberately about **one** shared surface, not
        about resembling the candidate: a member must match the candidate *and*
        cover the reference's own surface at least as well as the candidate does
        — the same second test :func:`_withdraw_ranked` applies, and for the same
        reason. A candidate that absorbed two codebases, one published and one
        still embargoed, matches each of them completely; without that test the
        embargoed finding would be re-pointed at the earlier published artifact,
        which shares nothing with it. That would both accuse the wrong operator
        and launder the finding into a lawful one.

        Returns the hold naming that earliest member, or — when the earliest
        member is this owner's own — a ``self_origin`` withdrawal: if nobody in
        the cluster predates this operator, no member of it can be where the
        operator got the code.
        """
        earliest = e
        for row in earlier_history:
            if row.agent_id == e.agent_id or in_cluster(row):
                earliest = row
                break
        if _same_owner(earliest):
            return NoCopyOpportunity(
                kind="self_origin",
                matched_agent_id=e.agent_id,
                source_agent_id=earliest.agent_id,
                source_available_at=None,
                signal=signal,
                detail=(
                    f"matched agent {e.agent_id}, but this owner's own agent "
                    f"{earliest.agent_id} is the earliest artifact carrying the "
                    "shared content, so no earlier submission by another owner "
                    "can be its source"
                ),
            )
        reason = reason_for(earliest)
        if earliest.agent_id != e.agent_id:
            reason += (
                f"; earliest artifact in the matching cluster "
                f"(nearest earlier match was agent {e.agent_id})"
            )
        return ReviewDecision(held=True, duplicate_of=earliest.agent_id, reason=reason)

    def _lexical_cluster_for(
        reference: LedgerRow, *, reference_strength: float
    ) -> Callable[[LedgerRow], bool]:
        """Bind one reference, testing membership of *its* lexical cluster.

        A factory rather than an inline closure for the reason
        :func:`_lexical_scorer` is one: the bound reference changes per iteration
        of the rule loop.
        """

        def member(row: LedgerRow) -> bool:
            if _fingerprint_corpora_incompatible(
                content_fingerprint, row.content_fingerprint
            ) or _fingerprint_versions_incompatible(
                content_fingerprint, row.content_fingerprint
            ):
                return False
            j, c = content_similarity(content_fingerprint, row.content_fingerprint)
            if j < jaccard_tol and c < containment_tol:
                return False
            return (
                _lexical_strength(
                    reference.content_fingerprint,
                    row.content_fingerprint,
                    jaccard_tol=jaccard_tol,
                    containment_tol=containment_tol,
                )
                >= reference_strength
            )

        return member

    def _lexical_reason(target: LedgerRow) -> str:
        j, c = content_similarity(content_fingerprint, target.content_fingerprint)
        return (
            f"content near-duplicate of agent {target.agent_id}: "
            f"composite delta {abs(composite - target.composite):.4f}, "
            f"jaccard {j:.3f}, containment {c:.3f}"
            + _structural_note(
                structural_fingerprint,
                target,
                jaccard_tol=structural_jaccard_tol,
                containment_tol=structural_containment_tol,
            )
            + _prompt_note(prompt_fingerprint, target)
        )

    # 1. Exact byte-identical copy of another miner's eligible artifact.
    # This is a defense-in-depth mirror of the separate admission-time exact-byte
    # guard. It uses the same upload chronology so a later-finalized row can never
    # become the retroactive original of an earlier submission.
    for e in earlier_unattested:
        if e.sha256 == sha256:
            released = _withdraw_equality(
                e, signal="exact_byte", matches="exact sha256 match"
            )
            if released is None:
                outcome = _attribute(
                    e,
                    signal="exact_byte",
                    in_cluster=lambda row: row.sha256 == sha256,
                    reason_for=lambda t: f"exact sha256 match of agent {t.agent_id}",
                )
                if isinstance(outcome, ReviewDecision):
                    return outcome
                released = outcome
            withdrawn = withdrawn or released

    # 1b. Exact-repack copy: same canonicalized source (comments/whitespace
    #     stripped, files sorted) even when the tarball bytes differ. An equality
    #     match, held unconditionally like sha256 — no score-proximity requirement.
    #     Both hashes must be present (null = "no repack match", never a hit).
    if normalized_source_hash is not None:
        for e in earlier_unattested:
            if e.normalized_source_hash == normalized_source_hash:
                released = _withdraw_equality(
                    e,
                    signal="normalized_source",
                    matches="normalized-source (repack) match",
                )
                if released is None:
                    outcome = _attribute(
                        e,
                        signal="normalized_source",
                        in_cluster=lambda row: (
                            row.normalized_source_hash == normalized_source_hash
                        ),
                        reason_for=lambda t: (
                            f"normalized-source (repack) match of agent {t.agent_id}"
                        ),
                    )
                    if isinstance(outcome, ReviewDecision):
                        return outcome
                    released = outcome
                withdrawn = withdrawn or released

    # 2. Near-dup fingerprint: a matching lexical sketch, on its own.
    #    Checked before the size rule because a fingerprint is the stronger,
    #    size-independent signal. The structural (whole-crate AST) channel
    #    saturates between independent starter-kit derivatives — astfp performs
    #    no reference subtraction — so it corroborates the audit reason instead
    #    of triggering until its sketches are reference-aware.
    #
    #    There is deliberately NO score-proximity precondition here. Two
    #    normalized-identical bench_version 7 artifacts (ditto-v7 / dittoTop-v0)
    #    scored 0.0768 apart in production, so a 0.03 window skipped the
    #    comparison precisely when the copy got the luckiest re-roll. See
    #    ``_DEFAULT_SCORE_TOL``. The composite delta is still reported in the
    #    audit reason as context for the reviewer.
    for e in earlier_unattested:
        # A refreshed starter-kit corpus changes what is subtracted from the
        # sketch.  During the bounded metadata backfill, old and new corpus IDs
        # coexist.  That pair is incomparable, but incomparability is not copy
        # evidence and must not itself create an operator hold.
        if _fingerprint_corpora_incompatible(
            content_fingerprint, e.content_fingerprint
        ):
            continue
        if _fingerprint_versions_incompatible(
            content_fingerprint, e.content_fingerprint
        ):
            # Absence of evidence, not evidence: the sketches cannot be compared
            # at all, so nothing here says "copy". Score proximity stays on this
            # branch as triage — it decides which uncomparable pairs are worth an
            # operator's time, rather than gating a fingerprint match that already
            # stands on its own. Without it, every cross-version pair in the
            # ledger would open an inconclusive review during an algorithm
            # rollout.
            if abs(composite - e.composite) > score_tol:
                continue
            return ReviewDecision(
                held=True,
                duplicate_of=e.agent_id,
                reason=(
                    f"anti-copy comparison inconclusive with agent {e.agent_id}: "
                    "incompatible lexical fingerprint version or corpus; "
                    "individual operator review required"
                ),
            )
        lex_j, lex_c = content_similarity(content_fingerprint, e.content_fingerprint)
        if lex_j >= jaccard_tol or lex_c >= containment_tol:
            released = _withdraw_ranked(
                e,
                signal="lexical",
                strength_to_candidate=_lexical_scorer(
                    content_fingerprint,
                    jaccard_tol=jaccard_tol,
                    containment_tol=containment_tol,
                ),
                strength_to_reference=_lexical_scorer(
                    e.content_fingerprint,
                    jaccard_tol=jaccard_tol,
                    containment_tol=containment_tol,
                ),
                reference_strength=max(lex_j / jaccard_tol, lex_c / containment_tol),
            )
            if released is not None:
                withdrawn = withdrawn or released
                continue
            outcome = _attribute(
                e,
                signal="lexical",
                in_cluster=_lexical_cluster_for(
                    e,
                    reference_strength=max(
                        lex_j / jaccard_tol, lex_c / containment_tol
                    ),
                ),
                reason_for=_lexical_reason,
            )
            if isinstance(outcome, ReviewDecision):
                return outcome
            withdrawn = withdrawn or outcome

    # 3. Size near-dup of another miner: a legacy fallback only when both rows
    #    predate every lexical/structural fingerprint. A versioned empty sketch is
    #    affirmative evidence that reference subtraction found too little custom
    #    surface; cross-version sketches likewise must fail open during backfill.
    if size_bytes is not None:
        candidate_size = size_bytes

        def _size_cluster(row: LedgerRow) -> bool:
            """Whether ``row`` trips the same legacy size test as the trigger."""
            return (
                row.size_bytes is not None
                and abs(composite - row.composite) <= score_tol
                and abs(candidate_size - row.size_bytes) <= size_tol
                and content_fingerprint is None
                and row.content_fingerprint is None
                and structural_fingerprint is None
                and row.structural_fingerprint is None
            )

        def _size_cluster_for(
            reference: LedgerRow, *, reference_strength: float
        ) -> Callable[[LedgerRow], bool]:
            """Bind one reference, testing membership of *its* size cluster."""

            def member(row: LedgerRow) -> bool:
                return (
                    _size_cluster(row)
                    and _size_proximity(
                        reference.size_bytes, row.size_bytes, size_tol=size_tol
                    )
                    >= reference_strength
                )

            return member

        def _size_reason(target: LedgerRow) -> str:
            delta = abs(candidate_size - (target.size_bytes or 0))
            return (
                f"near-duplicate of agent {target.agent_id}: "
                f"composite delta {abs(composite - target.composite):.4f}, "
                f"size delta {delta}B" + _prompt_note(prompt_fingerprint, target)
            )

        for e in earlier_unattested:
            if _size_cluster(e):
                released = _withdraw_ranked(
                    e,
                    signal="size_fallback",
                    strength_to_candidate=_size_scorer(size_bytes, size_tol=size_tol),
                    strength_to_reference=_size_scorer(e.size_bytes, size_tol=size_tol),
                    reference_strength=_size_proximity(
                        size_bytes, e.size_bytes, size_tol=size_tol
                    ),
                )
                if released is not None:
                    withdrawn = withdrawn or released
                    continue
                outcome = _attribute(
                    e,
                    signal="size_fallback",
                    in_cluster=_size_cluster_for(
                        e,
                        reference_strength=_size_proximity(
                            candidate_size, e.size_bytes, size_tol=size_tol
                        ),
                    ),
                    reason_for=_size_reason,
                )
                if isinstance(outcome, ReviewDecision):
                    return outcome
                withdrawn = withdrawn or outcome

    if withdrawn is not None:
        return ReviewDecision(held=False, no_copy_opportunity=withdrawn)
    return _NOT_HELD


def evaluate_rejected_resubmission(
    *,
    agent_id: UUID,
    submitted_at: datetime,
    sha256: str,
    normalized_source_hash: str | None = None,
    content_fingerprint: dict | None = None,
    rejected: Sequence[RejectedArtifact] = (),
    jaccard_tol: float = _DEFAULT_RESUBMISSION_JACCARD_TOL,
    containment_tol: float = _DEFAULT_RESUBMISSION_CONTAINMENT_TOL,
) -> ReviewDecision:
    """Hold an artifact that re-uploads one an operator already rejected.

    :func:`evaluate_duplicate_signals` answers "did this miner take *another*
    owner's work", so every rule there deliberately skips the candidate's own
    submissions and its coldkey siblings. That exclusion is correct for copy
    moderation and exactly wrong here. A rejection is a decision about an
    *artifact*, and the cheapest way to defeat it is to re-upload the same code
    under a new agent row — same owner, same hotkey, minutes later. So this rule
    inverts the ownership test: same-owner resubmission is the primary case, not
    an exemption.

    Three signals, in descending strength, mirroring rules 1 / 1b / 2 of the
    copy gate:

    1. **Exact match** — same ``sha256`` as a rejected artifact.
    2. **Exact repack** — same ``normalized_source_hash``: reformatted,
       re-commented, or files renamed and reordered, but the same source.
    3. **Near-duplicate lexical fingerprint** — reference-aware
       ``content_fingerprint`` at or above ``jaccard_tol`` Jaccard, or at or
       above ``containment_tol`` containment *in the padding direction only*
       (see :func:`_resubmission_lexical_match`), which survives re-indentation
       and localized edits.

    Where rule 3's thresholds come from
    ==================================

    Measured, not guessed, against every hold the rule opened in its first nine
    hours live (2026-08-17T02:00Z–11:00Z, 34 submissions, screening policy 9).
    It shipped reusing the copy gate's 0.75 / 0.95 and that was too sensitive by
    an order of magnitude: it opened 7 holds, 6 of them on artifacts an operator
    then read at source and found to be real revisions rather than re-uploads.
    All 7 have since been adjudicated.

        Jaccard / containment, held artifact vs the rejected one, outcome

        0.809 / 0.879   Crown-v11-Champion   evasion: engine renamed off the
                                             citation, ``derive::render(...)``
                                             substitution intact — the LOWEST
                                             pair in the corpus
        0.941 / 0.992   Crown-v11-v5         CLEARED: every cited ground
                                             deleted, composite paid to 0.688
        0.945 / 0.996   Crown-v11-v3         CLEARED: 574-line deletion of
                                             exactly the cited behaviour
        0.969 /     -   luffy11 v3           CLEARED: cited resolver compiled
                                             out under ``#[cfg(test)]``
        0.977 /     -   Aceron_v2            CLEARED: net −96 lines, both
                                             cited call sites deleted
        0.992 / 1.000   poor-miner v2        held: nothing remediated, every
                                             cited site still live pre-model
        1.000 / 1.000   Hannibal-v11-compl.  held: emission sites removed,
                                             template-keyed retrieval retained

    The distributions do not merely overlap, they are **inverted**: the single
    evasion in the corpus is the least similar pair in it, because routing
    around a reject reason means rewriting more code than complying with it
    does. No threshold on this axis separates the two classes, so raising the
    bar cannot be justified as "catching the same holds with fewer misses" —
    it is a decision to stop paying for a channel that was not discriminating.
    What the bar *can* do is confine the rule to its own stated question. At
    0.98 the rule holds exactly the two artifacts an operator found had
    remediated nothing, and releases all four it cleared by hand — 7 holds
    become 2, with no clear retained and no hand-cleared artifact stopped.

    One further contributor to the false-positive rate is *not* fixed here and
    is worth knowing about when these numbers are next re-cut: the residual
    still contains a miner's own vendored fixtures, which the reference corpus
    does not subtract because they are not starter-kit scaffolding. Crown-v11-v5
    carried a 30,522-line vendored vocabulary file among 27 byte-identical
    files, which pins Jaccard high regardless of what the miner changed in
    source. Excluding vendored/fixture paths from the lexical sketch would
    sharpen every consumer of this channel, but it changes the sketch for all
    agents and needs a fingerprint version bump plus a recompute, so it is its
    own change rather than a threshold move.

    That trade is explicit: **Crown-v11-Champion would not be held by this
    version of the rule.** It was a real catch and it is not recoverable here,
    because holding a 0.809 pair means holding every remediation any miner ever
    submits. An artifact whose engine was rewritten to evade a citation is a
    source-review finding — screening policy v9's job — and leaning on a
    duplicate-detection rule to catch it, at ~6 false holds per true one, buys
    that catch with the throughput of every honest miner recovering from a
    rejection.

    Rules 1 and 2 are untouched and stay exact. They cover the attack this rule
    was written for — re-uploading rejected code under a fresh agent row, plain
    or reformatted — and they produced no false positives in the corpus because
    a hash match is not an inference.

    No score proximity anywhere: a resubmission that scores differently is still
    the rejected artifact, and one that scores identically is no more guilty for
    it. When several rejected artifacts match, the earliest is named, so the
    audit trail points at the decision that was actually made first.

    Only ``before``-bounded rejections reach this function (see
    :func:`ditto.db.queries.scores.list_rejected_artifacts`), which keeps the
    rule prospective: an artifact uploaded before the rejection existed is not
    held by it.

    This is a *hold*, not a rejection. It routes to ``ath_pending_review`` with
    the matched ancestor named, and an operator decides whether the resemblance
    is a genuine re-upload or an honest descendant that shares scaffolding. The
    lexical channel cannot tell those apart — its own documentation notes it
    does not defeat identifier renaming or statement reordering — so it must not
    be wired to anything terminal.
    """
    if not rejected:
        return _NOT_HELD

    candidates: list[tuple[RejectedArtifact, str]] = []
    for row in rejected:
        if row.agent_id == agent_id:
            continue
        if _utc(row.first_seen) > _utc(submitted_at):
            continue
        if row.sha256 == sha256:
            candidates.append((row, "byte-identical to"))
            continue
        if (
            normalized_source_hash is not None
            and row.normalized_source_hash == normalized_source_hash
        ):
            candidates.append((row, "the same canonicalized source as"))
            continue
        fired = _resubmission_lexical_match(
            content_fingerprint,
            row.content_fingerprint,
            jaccard_tol=jaccard_tol,
            containment_tol=containment_tol,
        )
        if fired is not None:
            j, c = fired
            candidates.append(
                (
                    row,
                    "a lexical near-duplicate of "
                    f"(jaccard {j:.3f}, containment {c:.3f})",
                )
            )
    if not candidates:
        return _NOT_HELD

    match, relation = min(
        candidates, key=lambda pair: (_utc(pair[0].first_seen), pair[0].agent_id)
    )
    return ReviewDecision(
        held=True,
        duplicate_of=match.agent_id,
        reason=(
            f"Resubmission of a rejected artifact: this upload is {relation} "
            f"agent {match.agent_id} (hotkey {match.miner_hotkey}, uploaded "
            f"{_utc(match.first_seen).isoformat()}), which an operator rejected "
            "before this artifact was uploaded. Held for operator review; the "
            "lexical channel cannot distinguish a re-upload from an honest "
            "descendant that shares scaffolding."
        ),
    )
