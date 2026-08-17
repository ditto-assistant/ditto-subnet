// Scoring formulas, ported verbatim-in-behavior from the original single-file
// dashboard (dashboard-refactor-notes/monolith.html — source line numbers are
// cited per function). All consensus constants arrive from API payloads
// (/public/leaderboard fold, /public/weights, /public/bench/rollout,
// /public/bench/config|glossary); per the original's comment (3476–3478) a
// drifting hardcoded value "is a lie about the rule miners are actually being
// judged by", so nothing here bakes in a fold parameter. The original closed
// over module state (rolloutSettledView, desiredBench, …); these ports take
// that state as explicit arguments and stay pure.

import { fx } from "./format";
import type {
  ChainWeight,
  ChainWeightInfo,
  ChainWeightsSnapshot,
  CompositeBreakdown,
  EmissionsFold,
  LeaderboardPayload,
  RolloutState,
  TokenEfficiency,
} from "../types";

// ── Display composite (monolith 3448–3473) ───────────────────

/** The fields displayComposite reads. Structural so both wire entries and
 * consensus rows qualify (official_composite is not on every payload type). */
export interface CompositeCarrier {
  composite: number;
  official_composite?: number | null;
  settled_composite?: number | null;
}

/**
 * True while the API reports an open rollout (active < desired) on the
 * authoritative view. Mid-rollout the pool mixes settled active-version
 * medians with desired-version medians, two incomparable scales, so the board
 * ranks by each agent's settled active-version composite instead.
 *
 * Verbatim from render() (4791–4793):
 *   rolloutSettledView = data.selection_mode !== "historical" &&
 *     Number(data.active_bench_version) > 0 &&
 *     Number(data.desired_bench_version) > Number(data.active_bench_version);
 */
export function rolloutSettledView(data: LeaderboardPayload): boolean {
  return (
    data.selection_mode !== "historical" &&
    Number(data.active_bench_version) > 0 &&
    Number(data.desired_bench_version) > Number(data.active_bench_version)
  );
}

/**
 * The composite the board ranks and displays for a row. Identical to the
 * authoritative composite except mid-rollout (settledView), where an agent
 * already flipped to the desired version still ranks by its settled
 * active-version median so the ordering stays on one comparable scale.
 *
 * Verbatim from the original (3470–3473):
 *   function displayComposite(e) {
 *     if (rolloutSettledView && e.settled_composite != null) return e.settled_composite;
 *     return e.official_composite != null ? e.official_composite : e.composite;
 *   }
 */
export function displayComposite(e: CompositeCarrier, settledView = false): number {
  if (settledView && e.settled_composite != null) return e.settled_composite;
  return e.official_composite != null ? e.official_composite : e.composite;
}

/** Whether the exposed efficiency arithmetic is the score authority. Platform
 * exposes `effective_composite` for audit before activation, including neutral
 * factors whose arithmetic equals the official score, so numeric equality is
 * deliberately not used as an activation signal. */
export function efficiencyFoldIsApplied(e: { efficiency_fold_applied?: boolean }): boolean {
  return e.efficiency_fold_applied === true;
}

export type EfficiencyBoardStatusTone = "applied" | "projected" | "dormant";

export interface EfficiencyBoardStatus {
  tone: EfficiencyBoardStatusTone;
  headline: string;
  detail: string;
  /** Qualified cohort members, and the minimum before any factor is assigned. */
  cohortSize: number;
  nMin: number;
  /** Neutral reference cost, present only once a cohort actually formed. */
  referenceTokens: number | null;
  minimumFactor: number | null;
  maximumFactor: number | null;
}

/** Describe the board-level efficiency state in the terms an operator asks in.
 *
 * The failure this exists to prevent: with no cohort, an adjustment that is
 * switched off and one that is switched on both render as an empty column, so
 * "is it on?" and "is it doing anything?" collapse into the same blank. They
 * have different fixes — one is a settings flip, the other needs qualifying
 * evidence — so the copy distinguishes them explicitly. A dormant cohort is
 * reported as the normal fail-closed outcome it is, never as an error.
 */
export function efficiencyBoardStatus(
  state: {
    active?: boolean;
    preview?: boolean;
    cohort_size?: number | null;
    n_min?: number | null;
    reference_p25_tokens?: number | null;
    minimum_factor?: number | null;
    maximum_factor?: number | null;
  } | null,
): EfficiencyBoardStatus | null {
  if (!state) return null;
  const cohortSize = Math.max(0, Number(state.cohort_size) || 0);
  const nMin = Math.max(0, Number(state.n_min) || 0);
  const referenceTokens =
    state.reference_p25_tokens == null ? null : Number(state.reference_p25_tokens);
  const common = {
    cohortSize,
    nMin,
    referenceTokens: Number.isFinite(referenceTokens as number) ? referenceTokens : null,
    minimumFactor: state.minimum_factor == null ? null : Number(state.minimum_factor),
    maximumFactor: state.maximum_factor == null ? null : Number(state.maximum_factor),
  };
  if (cohortSize < nMin || cohortSize === 0) {
    return {
      ...common,
      tone: "dormant",
      headline: "Token efficiency is not adjusting any score",
      detail:
        cohortSize === 0
          ? "No agent has qualified for the cohort yet. Curve v3 costs an agent only on " +
            "independently confirmed runs, so a submission scored by the quorum alone " +
            "carries no efficiency evidence and is left untouched rather than guessed at."
          : cohortSize +
            " of the " +
            nMin +
            " agents required have qualified, so no reference cost is set and every " +
            "score is passed through unchanged.",
    };
  }
  if (state.active === true) {
    return {
      ...common,
      tone: "applied",
      headline: "Token efficiency is active as the quality tie-break",
      detail:
        "Quality remains the primary score, so a lower-quality agent never passes a " +
        "higher-quality agent. Among exact quality ties, each of the " +
        cohortSize +
        " qualified agents is ordered against the cohort's 25th-percentile cost: " +
        "cheaper earns headroom and dearer receives a tie-break penalty.",
    };
  }
  return {
    ...common,
    tone: "projected",
    headline: "Token efficiency is switched off — these are projections",
    detail:
      "The platform recomputed what the adjustment would do to the " +
      cohortSize +
      " qualified agents. Nothing here moves a rank, a crown, or an emission " +
      "until the fold is switched on.",
  };
}

export interface CurveV3ScoreAdjustment {
  quality: number;
  factor: number;
  adjusted: number;
  mode: "downside" | "neutral" | "headroom";
}

/** Curve-v3 factor bounds, mirroring the platform's efficiency-bonus policy
 * (`minimum_factor` / `maximum_factor`). A factor resting on a bound is worth
 * calling out: it means the cohort reference, not the agent's own cost, is what
 * is now limiting the tie-break. */
export const CURVE_V3_MIN_FACTOR = 0.85;
export const CURVE_V3_MAX_FACTOR = 1.1;

/** Reproduce a factor-curve score transform (Bench v9+) from public provenance.
 *
 * This is display-only arithmetic: the API's `effective_composite` is the
 * exact-quality secondary key when the fold is active. Authoritative quality
 * remains primary. Legacy v1/v2 bonuses deliberately do not enter this helper
 * because their historical replay remains unchanged. Bench v10 inherits the
 * v9 transform, so the gate is a floor, not an exact version. Curve v4 uses
 * the asymptotic headroom form and accepts factors outside [0.85, 1.10].
 */
export function curveV3ScoreAdjustment(e: {
  bench_version?: number | null;
  pre_efficiency_composite?: number | null;
  efficiency_factor?: number | null;
  efficiency_curve_version?: number | null;
}): CurveV3ScoreAdjustment | null {
  if (
    Number(e.bench_version) < 9 ||
    !Number.isFinite(Number(e.bench_version)) ||
    e.pre_efficiency_composite == null ||
    e.efficiency_factor == null
  ) {
    return null;
  }
  const quality = Number(e.pre_efficiency_composite);
  const factor = Number(e.efficiency_factor);
  const curveVersion = Number(e.efficiency_curve_version);
  const unbounded = Number.isFinite(curveVersion) && curveVersion >= 4;
  if (
    !Number.isFinite(quality) ||
    !Number.isFinite(factor) ||
    quality < 0 ||
    quality > 1 ||
    factor <= 0 ||
    (!unbounded && (factor < CURVE_V3_MIN_FACTOR || factor > CURVE_V3_MAX_FACTOR))
  ) {
    return null;
  }
  const adjusted =
    factor <= 1
      ? quality * factor
      : unbounded
        ? quality + (1 - quality) * (1 - 1 / factor)
        : quality + (factor - 1) * (1 - quality);
  return {
    quality,
    factor,
    adjusted,
    mode: factor < 1 ? "downside" : factor > 1 ? "headroom" : "neutral",
  };
}

export interface EfficiencyTieBreakChip {
  label: string;
  direction: "up" | "down" | "neutral";
  /** The factor is resting on `minimum_factor` / `maximum_factor`. */
  atBound: boolean;
}

/** Chip copy for the curve-v3 tie-break: which way it moves you, and by how
 * much — never the raw tie-break value.
 *
 * The absolute tie-break value is not a number a reader can use. It lives on
 * the efficiency-adjusted scale, it is consulted only inside an exact-quality
 * tier, and printed beside a 0.997 quality score it reads as a contradiction
 * rather than as a tie-break: a floored agent shows `0.847` next to `0.997`
 * and the honest reading of the row becomes "one of these is wrong". Worse,
 * the two directions are not comparable in that unit — at quality 0.997 a
 * −15% factor moves the value by −0.14955 while +10% moves it by +0.00030,
 * because downside multiplies quality and upside only closes remaining
 * headroom. Printing both as bare values invites a 500x misreading.
 *
 * The factor is what a miner can actually act on: it is bounded, it is
 * comparable between agents, and its sign is the whole content of a tie-break.
 * The exact value stays in the tooltip and the calculation breakdown, where a
 * precise number is expected and has room to be explained.
 */
export function efficiencyTieBreakChipLabel(
  e: { efficiency_factor?: number | null; efficiency_curve_version?: number | null },
  opts: { applied: boolean },
): EfficiencyTieBreakChip | null {
  if (e.efficiency_factor == null) return null;
  const factor = Number(e.efficiency_factor);
  if (!Number.isFinite(factor)) return null;
  const prefix = opts.applied ? "efficiency tie-break" : "efficiency tie-break preview";
  const magnitude = Math.abs(factor - 1) * 100;
  // Below the displayed precision the arrow would assert a direction the
  // number cannot support, so it reads as neutral instead.
  if (magnitude < 0.05) {
    return { label: prefix + " · neutral", direction: "neutral", atBound: false };
  }
  const up = factor > 1;
  const curveVersion = Number(e.efficiency_curve_version);
  const unbounded = Number.isFinite(curveVersion) && curveVersion >= 4;
  // Curve v4 does not apply the [0.85, 1.10] envelope; only v3 (or an
  // omitted version, which the board still treats as v3) can sit on a bound.
  const atBound =
    !unbounded &&
    (factor <= CURVE_V3_MIN_FACTOR ||
      (factor >= CURVE_V3_MAX_FACTOR && factor <= CURVE_V3_MAX_FACTOR + 1e-12));
  const bound = atBound ? (up ? " (cap)" : " (floor)") : "";
  return {
    label: prefix + " " + (up ? "▲" : "▼") + " " + magnitude.toFixed(1) + "%" + bound,
    direction: up ? "up" : "down",
    atBound,
  };
}

// ── Dethrone band + floor (monolith 3486–3540) ───────────────

/** Band-decay fold parameters (arrive on the emissions fold; kept structural
 * because older payloads omit them entirely). */
export interface BandDecayParams {
  band_decay_min_bench_version?: number | null;
  band_decay_start_composite?: number | null;
  band_decay_rate?: number | null;
  /** Protocol 24: largest share of the remaining headroom the band may take. */
  ceiling_headroom_share?: number | null;
  /** Whether the fleet has activated that cap; absent/false keeps the old band. */
  ceiling_band_clamp_active?: boolean | null;
}

export interface BenchVersioned {
  bench_version?: number | null;
}

/**
 * v6+ exponential hysteresis decay. Verbatim from the original (3486–3498):
 *   var version = Number.isFinite(challengerVersion)
 *     ? Math.min(championVersion, challengerVersion) : championVersion;
 *   if (!Number.isFinite(version) || !Number.isFinite(minVersion) || version < minVersion ||
 *       !Number.isFinite(start) || !Number.isFinite(rate) || rate <= 0) return 1;
 *   var boundedChampion = Math.min(Math.max(championComposite, start), 1);
 *   return Math.exp(-rate * (boundedChampion - start));
 */
export function dethroneBandScale(
  emissions: BandDecayParams | null | undefined,
  championEntry: BenchVersioned | null | undefined,
  championComposite: number,
  challengerEntry?: BenchVersioned | null,
): number {
  const minVersion = Number(emissions && emissions.band_decay_min_bench_version);
  const start = Number(emissions && emissions.band_decay_start_composite);
  const rate = Number(emissions && emissions.band_decay_rate);
  const championVersion = Number(championEntry && championEntry.bench_version);
  const challengerVersion = Number(challengerEntry && challengerEntry.bench_version);
  const version = Number.isFinite(challengerVersion)
    ? Math.min(championVersion, challengerVersion)
    : championVersion;
  if (
    !Number.isFinite(version) ||
    !Number.isFinite(minVersion) ||
    version < minVersion ||
    !Number.isFinite(start) ||
    !Number.isFinite(rate) ||
    rate <= 0
  )
    return 1;
  const boundedChampion = Math.min(Math.max(championComposite, start), 1);
  return Math.exp(-rate * (boundedChampion - start));
}

/**
 * Protocol 24: the band never asks for more than `ceiling_headroom_share` of
 * what is left above the incumbent, so the published floor stays below a
 * perfect score instead of climbing past it on a saturated benchmark. Mirrors
 * `_ceiling_capped_band` in ditto/validator/weights.py; inert (returns the
 * band unchanged) until the fleet activates the cap.
 *
 * The displayed floor is a quality composite, so the ceiling here is 1 — the
 * efficiency-adjusted ceiling the fold uses is not part of this copy.
 */
export function ceilingCappedMargin(
  emissions: BandDecayParams | null | undefined,
  band: number,
  championComposite: number,
): number {
  if (!emissions || emissions.ceiling_band_clamp_active !== true) return band;
  const share = Number(emissions.ceiling_headroom_share);
  if (!Number.isFinite(share) || share <= 0) return band;
  const headroom = 1 - championComposite;
  if (!(headroom > 0)) return 0;
  return Math.min(band, share * headroom);
}

export interface DethroneFloor {
  /** The champion's displayed composite (the settled one mid-rollout). */
  champComposite: number;
  /** dethroneBandScale result (1 when decay does not apply). */
  scale: number;
  /** margin × scale — the effective flat hysteresis term. */
  effectiveMargin: number;
  /** champComposite + effectiveMargin. A floor, not a guarantee. */
  floor: number;
  /** dethrone_z when finite and > 0, else null (no statistical band copy). */
  z: number | null;
}

/**
 * "Beat this to contend." The validator's rule (ditto/validator/weights.py
 * _beats) takes the LARGER of the flat margin and dethrone_z ×
 * √(SE_challenger² + SE_champion²); only the margin term is knowable before a
 * challenger is scored, so the result is published as a floor, never as a
 * sufficient score.
 *
 * The floor is ADDITIVE — verbatim from renderDethroneFloor (3524–3527):
 *   var scale = dethroneBandScale(emissions, championEntry, champComposite);
 *   var effectiveMargin = margin * scale;
 *   var floor = champComposite + effectiveMargin;
 *   var z = emissions.dethrone_z;
 * (never `champComposite * (1 + margin)` — the old regression suite banned
 * that literal from the source).
 */
export function dethroneFloor(
  emissions:
    | (BandDecayParams & { margin?: number | null; dethrone_z?: number | null })
    | null
    | undefined,
  championEntry: (CompositeCarrier & BenchVersioned) | null | undefined,
  settledView = false,
): DethroneFloor | null {
  if (!emissions || !championEntry) return null;
  const champComposite = displayComposite(championEntry, settledView);
  const margin = emissions.margin;
  if (!Number.isFinite(champComposite) || typeof margin !== "number" || !Number.isFinite(margin))
    return null;
  const scale = dethroneBandScale(emissions, championEntry, champComposite);
  const effectiveMargin = ceilingCappedMargin(emissions, margin * scale, champComposite);
  const floor = champComposite + effectiveMargin;
  const z = emissions.dethrone_z;
  return {
    champComposite,
    scale,
    effectiveMargin,
    floor,
    z: typeof z === "number" && Number.isFinite(z) && z > 0 ? z : null,
  };
}

// ── Emissions split (monolith 3666–3692) ─────────────────────

export interface EmissionsSplit {
  /** The incumbent champion's fixed share of the miner pool. */
  championShare: number;
  /** 1 − champion_share, split across the participation tail. */
  tailShare: number;
  /** Up to this many tail recipients; null when the API omits it. */
  tailSize: number | null;
  /** Descending ranked shares (non-finite entries filtered out). */
  rankShares: number[];
  /** Flat hysteresis margin, null when non-finite. */
  margin: number | null;
  /** dethrone_z when finite and > 0, else null. */
  dethroneZ: number | null;
}

/**
 * The emissions split, from applyEmissionsCopy (3687–3691): champion takes
 * `emissions.champion_share` of the miner pool; the tail splits
 * `1 - emissions.champion_share` across up to `emissions.tail_size`
 * recipients in descending `emissions.rank_shares`. Null when the fold does
 * not carry a finite champion_share (the original falls back to generic copy).
 */
export function emissionsSplit(emissions: EmissionsFold | null | undefined): EmissionsSplit | null {
  if (!emissions) return null;
  const share = emissions.champion_share;
  if (typeof share !== "number" || !Number.isFinite(share)) return null;
  const tailSize =
    typeof emissions.tail_size === "number" && Number.isFinite(emissions.tail_size)
      ? emissions.tail_size
      : null;
  const rankShares = Array.isArray(emissions.rank_shares)
    ? emissions.rank_shares.filter((value) => Number.isFinite(value))
    : [];
  const margin =
    typeof emissions.margin === "number" && Number.isFinite(emissions.margin)
      ? emissions.margin
      : null;
  const z = emissions.dethrone_z;
  return {
    championShare: share,
    tailShare: 1 - share,
    tailSize,
    rankShares,
    margin,
    dethroneZ: typeof z === "number" && Number.isFinite(z) && z > 0 ? z : null,
  };
}

// ── Rollout quorum readers (monolith 3746–3759) ──────────────

export interface RolloutQuorum {
  /** state.ranked_quorum_agents, read raw (may be null — callers gate on
   * Number.isFinite exactly like the original). */
  ready: number | null;
  /** state.min_ranked_quorum_agents, read raw. */
  needed: number | null;
  /** Number(state.priority_cohort_size) || 5. */
  prioritySize: number;
  /** Number(state.cohort_size) || 0. */
  cohortSize: number;
  /** Members in the first-five priority cohort with a complete per-agent
   * quorum (Number(position) <= prioritySize && Number(score_count) >= 3). */
  priorityReady: number;
  /** state.cohort_ready_count || 0 (as read at 3769). */
  cohortReadyCount: number;
}

/**
 * Quorum/cohort gates from renderRollout — the Number() coercions are the
 * contract (3746–3759):
 *   var ready = state.ranked_quorum_agents;
 *   var needed = state.min_ranked_quorum_agents;
 *   var prioritySize = Number(state.priority_cohort_size) || 5;
 *   var cohortSize = Number(state.cohort_size) || 0;
 *   var priorityReady = Array.isArray(state.members)
 *     ? state.members.filter(function (member) {
 *         return Number(member.position) <= prioritySize && Number(member.score_count) >= 3;
 *       }).length : 0;
 * Semantics (3699–3704): weights follow ONE version; the ledger flips only
 * when ranked_quorum_agents >= min_ranked_quorum_agents at the desired
 * version; per-agent quorum is 3 scores; the first-five priority cohort is a
 * fleet-wide barrier before the bounded inherited cohort.
 */
export function rolloutQuorum(state: RolloutState | null | undefined): RolloutQuorum {
  const prioritySize = Number(state && state.priority_cohort_size) || 5;
  const cohortSize = Number(state && state.cohort_size) || 0;
  const priorityReady =
    state && Array.isArray(state.members)
      ? state.members.filter(
          (member) => Number(member.position) <= prioritySize && Number(member.score_count) >= 3,
        ).length
      : 0;
  return {
    ready: state?.ranked_quorum_agents ?? null,
    needed: state?.min_ranked_quorum_agents ?? null,
    prioritySize,
    cohortSize,
    priorityReady,
    cohortReadyCount: (state && state.cohort_ready_count) || 0,
  };
}

// ── Cohort median (monolith 7197–7201) ───────────────────────

/**
 * Verbatim:
 *   function cohortMedian(scores) {
 *     var values = scores.map(function (score) { return Number(score.composite); })
 *       .sort(function (a, b) { return a - b; });
 *     var middle = Math.floor(values.length / 2);
 *     return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
 *   }
 * (Empty input yields NaN, exactly as the original; callers never pass one.)
 */
export function cohortMedian(scores: ReadonlyArray<{ composite: number | string }>): number {
  const values = scores.map((score) => Number(score.composite)).sort((a, b) => a - b);
  const middle = Math.floor(values.length / 2);
  return values.length % 2
    ? (values[middle] as number)
    : ((values[middle - 1] as number) + (values[middle] as number)) / 2;
}

// ── Eligibility predicates + unranked gate (monolith 5558–5572) ──

export interface EligibilityFlags {
  /** Missing counts as eligible (older APIs omit it). */
  eligible?: boolean;
  /** Missing counts as finalized. */
  finalized?: boolean;
  /** Strict === true means registered; null/missing is UNKNOWN, not false. */
  registered?: boolean | null;
  /** Cases scored; n >= 100 distinguishes a zero-scoring full run. */
  n?: number | null;
}

/** isEligible(e) = `!e || e.eligible !== false` (5562). */
export function isEligible(e: EligibilityFlags | null | undefined): boolean {
  return !e || e.eligible !== false;
}

/** isFinalized(e) = `!e || e.finalized !== false` (5563). */
export function isFinalized(e: EligibilityFlags | null | undefined): boolean {
  return !e || e.finalized !== false;
}

/** isRegistered(e) = `!!e && e.registered === true` (5564). */
export function isRegistered(e: EligibilityFlags | null | undefined): boolean {
  return !!e && e.registered === true;
}

/**
 * Why an unranked run is unranked. Verbatim gate (5569–5572):
 *   return (e && e.n != null && e.n >= 100) ? "zero" : "provisional";
 * A full run (n >= 100) with no rank scored a non-positive composite;
 * mirrors the backend's two-gate rule so the badge never mislabels a
 * zero-scoring full run as a small run. Null for eligible entries.
 */
export function unrankedKind(
  e: EligibilityFlags | null | undefined,
): "zero" | "provisional" | null {
  if (isEligible(e)) return null;
  return e && e.n != null && e.n >= 100 ? "zero" : "provisional";
}

// ── Sort / dual rank (monolith 4794–4811) ────────────────────

/**
 * Board ordering — verbatim comparator from render() (4799–4805):
 *   var af = isFinalized(a), bf = isFinalized(b);
 *   if (af !== bf) return af ? -1 : 1;
 *   var ae = isEligible(a), be = isEligible(b);
 *   if (ae !== be) return ae ? -1 : 1;
 *   return displayComposite(b) - displayComposite(a);
 */
export function boardEntryCompare<T extends CompositeCarrier & EligibilityFlags>(
  a: T,
  b: T,
  settledView = false,
): number {
  const af = isFinalized(a);
  const bf = isFinalized(b);
  if (af !== bf) return af ? -1 : 1;
  const ae = isEligible(a);
  const be = isEligible(b);
  if (ae !== be) return ae ? -1 : 1;
  return displayComposite(b, settledView) - displayComposite(a, settledView);
}

/**
 * Sort and assign display ranks (4806–4811): dual counters — eligible rows
 * take ++finalRank (finalized) or ++provisionalRank; ineligible rows get
 * rank = null. Pure: returns shallow copies, never mutates the input (the
 * original wrote e.rank in place).
 */
export function rankEntries<T extends CompositeCarrier & EligibilityFlags>(
  entries: readonly T[] | null | undefined,
  settledView = false,
): (T & { rank: number | null })[] {
  let finalRank = 0;
  let provisionalRank = 0;
  return (entries || [])
    .slice()
    .sort((a, b) => boardEntryCompare(a, b, settledView))
    .map((e) => ({
      ...e,
      rank: isEligible(e) ? (isFinalized(e) ? ++finalRank : ++provisionalRank) : null,
    }));
}

/**
 * Older-run flag (map §Sort/rank; monolith 4849–4851): "old" means below the
 * SETTLED benchmark version — during a rollout the active version is the
 * baseline, and tagging it "old" misreads as stale data.
 *   isFinalized(e) && (e.bench_version == null || e.bench_version < settledBenchVersion())
 * (`x < null` coerces null to 0 in the original; `?? 0` preserves that.)
 */
export function isOlderRun(
  e: (EligibilityFlags & BenchVersioned) | null | undefined,
  settledVersion: number | null,
): boolean {
  return (
    isFinalized(e) && (!e || e.bench_version == null || e.bench_version < (settledVersion ?? 0))
  );
}

// ── Quorum coercions (recurring pattern; monolith 5620, 5664, 6104) ──

/**
 * The recurring quorum coercion: `Math.max(1, Number(value) || 3)` —
 * renderConsensus reads it from r.quorum, rolloutChip from e.score_quorum.
 */
export function scoreQuorum(value: unknown): number {
  return Math.max(1, Number(value) || 3);
}

export interface ContinualAggregate {
  aggregate_method?: string | null;
  confirmation_seed_depth?: number | null;
  retained_sample_count?: number | null;
  completed_wave_count?: number | null;
  aggregate_sample_count?: number | null;
}

/**
 * Retained continual-retest waves for the chip (5618):
 *   Number(e.retained_sample_count == null ? e.completed_wave_count : e.retained_sample_count) || 0
 */
export function continualWaves(e: ContinualAggregate): number {
  return (
    Number(e.retained_sample_count == null ? e.completed_wave_count : e.retained_sample_count) || 0
  );
}

/**
 * Samples inside the continual mean (5620):
 *   var samples = Number(e.aggregate_sample_count) || (3 + waves);
 * (the original three validator scores plus the retained per-seed samples).
 */
export function continualSampleCount(e: ContinualAggregate): number {
  return Number(e.aggregate_sample_count) || 3 + continualWaves(e);
}

// ── Chain-observation champion (monolith 4120–4195) ──────────

/**
 * Vector champion ordering (4142–4145): weight value descending, then uid
 * ascending as the deterministic tiebreak:
 *   return b.value - a.value || a.uid - b.uid;
 */
export function chainChampionCompare(a: ChainWeight, b: ChainWeight): number {
  return b.value - a.value || a.uid - b.uid;
}

/** The top revealed choice of one vector's miner weights (null when empty). */
export function vectorChampion(weights: readonly ChainWeight[]): ChainWeight | null {
  return weights.slice().sort(chainChampionCompare)[0] ?? null;
}

export interface ChainWeightFold {
  /** Per-miner-hotkey support counts (weighted / champion / vectors). */
  byHotkey: Record<string, ChainWeightInfo>;
  /** How many vectors crowned each hotkey their top choice. */
  championCounts: Record<string, number>;
  /** Total revealed miner-bearing vectors. */
  minerVectors: number;
  /** Champion hotkeys, most-crowned first (count desc, then localeCompare). */
  leaders: string[];
}

/**
 * The pure fold of applyChainWeights (4130–4165): per revealed vector,
 * positive non-owner miner weights are kept, empty vectors are skipped, the
 * vector champion is the chainChampionCompare winner, and every touched
 * hotkey accumulates { weighted, champion } with the final vectors total
 * stamped on all of them. `share` additionally accumulates each vector's
 * normalized weight (value / vector miner total) and lands as the mean over
 * ALL miner-bearing vectors, so a miner two of twelve validators point at
 * cannot read as heavier than one all twelve point at. Null when the
 * snapshot has no vectors array (chain unreachable — the original hides the
 * panel).
 */
export function foldChainWeights(
  snapshot: ChainWeightsSnapshot | null | undefined,
): ChainWeightFold | null {
  if (!snapshot || !Array.isArray(snapshot.vectors)) return null;
  const owner = snapshot.owner_hotkey || "";
  const byHotkey: Record<string, ChainWeightInfo> = {};
  const championCounts: Record<string, number> = {};
  let minerVectors = 0;
  snapshot.vectors.forEach((vector) => {
    const minerWeights = (vector.weights || []).filter(
      (weight) => weight && weight.value > 0 && weight.hotkey !== owner,
    );
    const total = minerWeights.reduce((sum, weight) => sum + weight.value, 0);
    if (!total) return;
    minerVectors += 1;
    const champion = vectorChampion(minerWeights) as ChainWeight;
    championCounts[champion.hotkey] = (championCounts[champion.hotkey] || 0) + 1;
    minerWeights.forEach((weight) => {
      const info = (byHotkey[weight.hotkey] = byHotkey[weight.hotkey] || {
        weighted: 0,
        champion: 0,
        vectors: 0,
        share: 0,
      });
      info.weighted += 1;
      info.share += weight.value / total;
      if (weight.hotkey === champion.hotkey) info.champion += 1;
    });
  });
  Object.keys(byHotkey).forEach((hotkey) => {
    const info = byHotkey[hotkey] as ChainWeightInfo;
    info.vectors = minerVectors;
    info.share = info.share / minerVectors;
  });
  const leaders = Object.keys(championCounts).sort(
    (a, b) => (championCounts[b] as number) - (championCounts[a] as number) || a.localeCompare(b),
  );
  return { byHotkey, championCounts, minerVectors, leaders };
}

// ── Per-validator revealed weight views (fleet + raw-matrix panel) ──

export interface ValidatorWeightEntry {
  uid: number;
  hotkey: string;
  /** Raw u16 chain value as revealed. */
  value: number;
  /** This destination's share of the vector's miner-weight mass (0–1). */
  share: number;
  /** True for the vector's chainChampionCompare winner. */
  top: boolean;
}

export interface ValidatorWeightView {
  validatorUid: number | null;
  validatorHotkey: string;
  /** Positive non-owner destinations, heaviest first (chainChampionCompare). */
  entries: ValidatorWeightEntry[];
}

/**
 * One view per revealed miner-bearing vector, in snapshot order: the same
 * owner/positive filter and champion ordering as foldChainWeights, kept as
 * per-validator rows instead of folded per miner. Vectors with no miner
 * weight (owner-only or empty) are skipped, matching the fold's
 * `minerVectors` accounting. Null when the snapshot has no vectors array.
 */
export function validatorWeightViews(
  snapshot: ChainWeightsSnapshot | null | undefined,
): ValidatorWeightView[] | null {
  if (!snapshot || !Array.isArray(snapshot.vectors)) return null;
  const owner = snapshot.owner_hotkey || "";
  const views: ValidatorWeightView[] = [];
  snapshot.vectors.forEach((vector) => {
    const minerWeights = (vector.weights || []).filter(
      (weight) => weight && weight.value > 0 && weight.hotkey !== owner,
    );
    const total = minerWeights.reduce((sum, weight) => sum + weight.value, 0);
    if (!total) return;
    const ordered = minerWeights.slice().sort(chainChampionCompare);
    views.push({
      validatorUid: vector.validator_uid ?? null,
      validatorHotkey: vector.validator_hotkey || "",
      entries: ordered.map((weight, index) => ({
        uid: weight.uid,
        hotkey: weight.hotkey,
        value: weight.value,
        share: weight.value / total,
        top: index === 0,
      })),
    });
  });
  return views;
}

/**
 * Verbatim chainWeightLabel (4191–4195):
 *   if (!chainWeight || !chainWeight.vectors) return "";
 *   if (chainWeight.champion) return "Validator top choice · " + champion + "/" + vectors;
 *   return "Validator support · " + weighted + "/" + vectors;
 */
export function chainWeightLabel(chainWeight: ChainWeightInfo | null | undefined): string {
  if (!chainWeight || !chainWeight.vectors) return "";
  if (chainWeight.champion)
    return "Validator top choice · " + chainWeight.champion + "/" + chainWeight.vectors;
  return "Validator support · " + chainWeight.weighted + "/" + chainWeight.vectors;
}

// ── Chip thresholds (monolith 5633–5688, 5712–5729, 6259–6274) ──

/**
 * Quality-gate chip (5633–5647): reduction = Math.max(0, 1 - multiplier),
 * shown only when it meaningfully bites (`if (!(reduction > 0.005)) return`),
 * labelled `'gates −' + (reduction * 100).toFixed(0) + '%'`. Null when hidden.
 */
export function qualityGateChipLabel(b: CompositeBreakdown | null | undefined): string | null {
  if (!b || b.benchmark_quality_multiplier == null) return null;
  const mult = Number(b.benchmark_quality_multiplier);
  const reduction = Math.max(0, 1 - mult);
  if (!(reduction > 0.005)) return null; // no meaningful gate effect
  return "gates −" + (reduction * 100).toFixed(0) + "%";
}

/**
 * Token-penalty chip (5648–5658): penalized when penalty > 0.00005; label
 * `"token −" + (penalty * 100).toFixed(1) + "%"` or "token no penalty".
 * Null when the breakdown carries no token_penalty at all.
 */
export function tokenPenaltyChipLabel(
  b: CompositeBreakdown | null | undefined,
): { label: string; penalized: boolean } | null {
  if (!b || b.token_penalty == null) return null;
  const penalty = Math.max(0, Number(b.token_penalty) || 0);
  const penalized = penalty > 0.00005;
  return {
    label: penalized ? "token −" + (penalty * 100).toFixed(1) + "%" : "token no penalty",
    penalized,
  };
}

/**
 * ±1-SE band bounds in track percent (errBand, 5682–5688):
 *   var lo = clamp01(comp - se) * 100; var hi = clamp01(comp + se) * 100;
 * width floors at 0.6% so a tiny band stays visible. Null when se is
 * missing or not > 0.
 */
export function errBandBounds(
  comp: number,
  se: number | null | undefined,
): { lo: number; hi: number; width: number } | null {
  if (se == null || !(se > 0)) return null;
  const lo = Math.max(0, Math.min(1, comp - se)) * 100;
  const hi = Math.max(0, Math.min(1, comp + se)) * 100;
  return { lo, hi, width: Math.max(0.6, hi - lo) };
}

/**
 * Whether the composite cell may attach the SE band (5586–5589): the stashed
 * stderr describes the authoritative composite, so the band shows only when
 * the displayed value IS e.composite and the aggregate is not a continual
 * mean (whose spread the band would misstate).
 */
export function showsCompositeErrBand(
  e: CompositeCarrier & { aggregate_method?: string | null },
  settledView = false,
): boolean {
  return (
    displayComposite(e, settledView) === e.composite && e.aggregate_method !== "continual_mean"
  );
}

/** Trend direction with the ±0.0005 dead-band (5717): up / down / flat. */
export function trendDirection(delta: number): "up" | "down" | "flat" {
  return delta > 0.0005 ? "up" : delta < -0.0005 ? "down" : "flat";
}

/**
 * Grade a 0..1 score into a colour band (6259–6264): >= 0.8 good,
 * >= 0.5 mid, else low; "" when the score is missing.
 */
export function scoreClass(v: number | null | undefined): "" | "good" | "mid" | "low" {
  if (v == null) return "";
  if (v >= 0.8) return "good";
  if (v >= 0.5) return "mid";
  return "low";
}

/**
 * A case's pass/fail verdict (6269–6274). Memory cases carry a binary
 * deterministic result; tool cases are graded continuously, so a full
 * deterministic trajectory (>= 0.999) is a pass, a fully wrong one
 * (<= 0.001) a fail, anything between partial. (The original's loose
 * comparisons coerce a null tool_score to 0 — a fail — while undefined
 * compares false both ways — partial; both are preserved.)
 */
export function caseVerdict(c: {
  kind?: string;
  correct?: boolean | null;
  tool_score?: number | null;
}): "pass" | "fail" | "partial" {
  if (c.kind === "memory")
    return c.correct === true ? "pass" : c.correct === false ? "fail" : "partial";
  const toolScore = c.tool_score;
  if (toolScore != null) {
    if (toolScore >= 0.999) return "pass";
    if (toolScore <= 0.001) return "fail";
    return "partial";
  }
  return toolScore === null ? "fail" : "partial";
}

// ── Composite equations (monolith 5810–5843) ─────────────────

/** The `title` the inline equation carries (5863), naming each factor. It is
 * the only place the equation's terms are spelled out in words. */
export const COMPOSITE_EQUATION_TITLE =
  "base accuracy × benchmark quality gates × token efficiency = final composite";

/**
 * Inline consensus-row equation (5810–5815):
 * "base × quality × token = final", with token "n/a" when the multiplier is
 * absent; "" without a breakdown. (The title copy is COMPOSITE_EQUATION_TITLE
 * above.)
 */
export function compositeEquationText(b: CompositeBreakdown | null | undefined): string {
  if (!b) return "";
  const token = b.token_efficiency_multiplier == null ? "n/a" : fx(b.token_efficiency_multiplier);
  return (
    fx(b.base_accuracy) +
    " × " +
    fx(b.benchmark_quality_multiplier) +
    " × " +
    token +
    " = " +
    fx(b.final_composite)
  );
}

/** The maximum token penalty in percent (5831): the only place 10% may be
 * assumed, and only when the API omits maximum_token_penalty. */
export function maxTokenPenaltyPct(maximumTokenPenalty: number | null | undefined): number {
  return (maximumTokenPenalty == null ? 0.1 : maximumTokenPenalty) * 100;
}

export interface StatRow {
  k: string;
  v: string;
}

/** Heading of the drill-down calculation block (5841). */
export const COMPOSITE_CALC_HEADING = "Composite calculation";

/** Closing note of the calculation block (5842) — quality gates and the
 * bounded token adjustment are separate, audited factors. */
export const COMPOSITE_CALC_NOTE =
  "The benchmark-quality multiplier combines scorer-owned behavioural and integrity gates " +
  "before token efficiency. It is shown as one audited factor so the platform cannot drift " +
  "from the benchmark scorer. Older benches retain their bounded token penalty; Bench v7/v8 " +
  "relative-efficiency awards are upside, while Bench v9+ uses a bounded factor around a " +
  "frozen cohort P25 reference. Downside multiplies quality; upside scales only remaining " +
  "quality headroom, so imperfect quality cannot become 1.000. Both are computed after " +
  "authoritative quality aggregation. Curve-v3 is only an exact-quality tie-break; " +
  "lower quality never passes higher quality. " +
  "an audit-only projection is not used for ranking or emissions.";

export function compositeCalculationHeading(e: {
  pre_efficiency_composite?: number | null;
  efficiency_bonus?: number | null;
  efficiency_factor?: number | null;
  efficiency_fold_applied?: boolean;
  effective_composite?: number | null;
  official_composite?: number | null;
}): string {
  if (e.pre_efficiency_composite == null) return COMPOSITE_CALC_HEADING;
  const hasAdjustment = e.efficiency_factor != null || e.efficiency_bonus != null;
  return hasAdjustment && !efficiencyFoldIsApplied(e)
    ? "Score provenance and efficiency projection"
    : "Score provenance and ranking fold";
}

/**
 * The drill-down "Composite calculation" rows (5817–5840), separating the
 * quality gates from the bounded token-efficiency adjustment:
 *   Tool/memory base        "0.5 × <tool> + 0.5 × <memory> = <base>"
 *   Benchmark quality gates "× <mult> (−<x.x>%)"
 *   Pre-token composite     "<value>"
 *   Token efficiency        "not applied or unavailable" | "× <mult> (−<x.x>%; max <n>%)"
 *   Final composite         "<value>"
 *   Observed token use      "<observed> / <baseline> p<pct> baseline" (when reported)
 * Null without a breakdown (the block does not render).
 */
export function compositeCalculationRows(e: {
  tool_mean: number;
  memory_mean: number;
  bench_version?: number | null;
  aggregate_method?: string | null;
  pre_efficiency_composite?: number | null;
  efficiency_bonus?: number | null;
  efficiency_factor?: number | null;
  efficiency_curve_version?: number | null;
  efficiency_fold_applied?: boolean;
  effective_composite?: number | null;
  official_composite?: number | null;
  composite?: number;
  composite_breakdown?: CompositeBreakdown | null;
  token_efficiency?: TokenEfficiency | null;
}): StatRow[] | null {
  const b = e && e.composite_breakdown;
  if (!b) return null;
  const qualityReduction = Math.max(0, 1 - b.benchmark_quality_multiplier);
  const rows: StatRow[] = [
    {
      k: "Tool/memory base",
      v: "0.5 × " + fx(e.tool_mean) + " + 0.5 × " + fx(e.memory_mean) + " = " + fx(b.base_accuracy),
    },
    {
      k: "Benchmark quality gates",
      v:
        "× " +
        fx(b.benchmark_quality_multiplier) +
        " (−" +
        (qualityReduction * 100).toFixed(1) +
        "%)",
    },
    { k: "Pre-token composite", v: fx(b.pre_token_composite) },
  ];
  const hasRankingFold = e.pre_efficiency_composite != null;
  if (hasRankingFold && Number(e.bench_version) >= 7) {
    // Bench v7+ does not reuse the legacy signed token penalty. Its separate
    // epoch-frozen adjustment is shown below, after authoritative quality.
  } else if (b.token_efficiency_multiplier == null) {
    rows.push({ k: "Token efficiency", v: "not applied or unavailable" });
  } else {
    const penalty = Math.max(0, Number(b.token_penalty) || 0);
    rows.push({
      k: "Token efficiency",
      v:
        "× " +
        fx(b.token_efficiency_multiplier) +
        " (−" +
        (penalty * 100).toFixed(1) +
        "%; max " +
        maxTokenPenaltyPct(b.maximum_token_penalty).toFixed(0) +
        "%)",
    });
  }
  rows.push({
    k: hasRankingFold
      ? e.aggregate_method === "continual_mean"
        ? "Initial quorum signed composite"
        : "Signed composite"
      : "Final composite",
    v: fx(b.final_composite),
  });
  if (e.pre_efficiency_composite != null) {
    rows.push({
      k:
        e.aggregate_method === "continual_mean" ? "Continual aggregate" : "Score before efficiency",
      v: fx(e.pre_efficiency_composite),
    });
    const hasFactor = e.efficiency_factor != null;
    const hasBonus = !hasFactor && e.efficiency_bonus != null;
    const scoreAdjustment = curveV3ScoreAdjustment(e);
    if (hasFactor) {
      const factor = Number(e.efficiency_factor);
      const delta = (factor - 1) * 100;
      rows.push({
        k: "Bounded token-efficiency factor",
        v:
          "× " +
          fx(factor) +
          " (" +
          (delta >= 0 ? "+" : "−") +
          Math.abs(delta).toFixed(1) +
          "% · frozen cohort P25 reference)",
      });
      if (scoreAdjustment != null) {
        rows.push({
          k: "Bench v9+ efficiency transform",
          v:
            scoreAdjustment.mode === "headroom" && Number(e.efficiency_curve_version) >= 4
              ? fx(scoreAdjustment.quality) +
                " + (1 − " +
                fx(scoreAdjustment.quality) +
                ") × (1 − 1 / " +
                fx(scoreAdjustment.factor) +
                ") = " +
                fx(scoreAdjustment.adjusted)
              : scoreAdjustment.mode === "headroom"
                ? fx(scoreAdjustment.quality) +
                  " + (" +
                  fx(scoreAdjustment.factor) +
                  " − 1) × (1 − " +
                  fx(scoreAdjustment.quality) +
                  ") = " +
                  fx(scoreAdjustment.adjusted)
                : fx(scoreAdjustment.quality) +
                  " × " +
                  fx(scoreAdjustment.factor) +
                  " = " +
                  fx(scoreAdjustment.adjusted),
        });
      }
    } else if (hasBonus) {
      rows.push({
        k: "Relative token-efficiency bonus",
        v: "+" + (Number(e.efficiency_bonus) * 100).toFixed(1) + "% · frozen cohort award",
      });
    }
    if (hasFactor || hasBonus) {
      if (efficiencyFoldIsApplied(e)) {
        if (hasFactor) {
          rows.push({
            k: "Current quality score",
            v:
              fx(e.official_composite ?? e.composite ?? e.pre_efficiency_composite) +
              " · primary ranking key",
          });
          rows.push({
            k: "Efficiency tie-break",
            v: fx(Number(e.effective_composite)) + " · active only after exact quality equality",
          });
        } else {
          rows.push({
            k: "Folded ranking score",
            v: fx(Number(e.effective_composite)) + " · used for rank, KOTH, and emissions",
          });
        }
      } else {
        rows.push({
          k: "Efficiency projection",
          v:
            (e.effective_composite == null ? "unavailable" : fx(e.effective_composite)) +
            " · audit only; not used for rank, KOTH, or emissions",
        });
        rows.push({
          k: "Current ranking score",
          v:
            fx(e.official_composite ?? e.composite ?? e.pre_efficiency_composite) +
            " · used for rank, KOTH, and emissions",
        });
      }
    } else {
      rows.push({
        k: "Current ranking score",
        v: fx(e.official_composite ?? e.composite ?? e.pre_efficiency_composite),
      });
    }
  }
  const te = e.token_efficiency;
  if (te && te.observed_total_tokens != null) {
    const budget =
      te.baseline_total_tokens == null
        ? "baseline unavailable"
        : te.baseline_total_tokens.toLocaleString() +
          " p" +
          Math.round((te.budget_percentile ?? 0) * 100) +
          " baseline";
    rows.push({
      k: "Observed token use",
      v: te.observed_total_tokens.toLocaleString() + " / " + budget,
    });
  }
  return rows;
}

// ── Source-release embargo default (monolith 3546) ───────────

/**
 * The only defaulted consensus-adjacent literal in the file:
 *   var hours = Number(release.embargo_hours) || 48;
 * (signed download links live 5 minutes; that lives with the fetch code).
 */
export function embargoHours(
  release: { embargo_hours?: number | null } | null | undefined,
): number {
  return Number(release && release.embargo_hours) || 48;
}
