// Benchmark rollout / authority state, ported verbatim-in-behavior from the
// original single-file dashboard (dashboard-refactor-notes/monolith.html —
// source line numbers cited per function). Weights follow ONE version at a
// time: the whole ledger flips only once ranked_quorum_agents reaches
// min_ranked_quorum_agents at the desired version. Everything here reads the
// versions from API payloads; nothing promotes the rollout target to
// "active" on its own.

import type { RolloutState } from "../types";

export interface BenchAuthorityState {
  active: number | null;
  desired: number | null;
  rolling: boolean;
}

/**
 * Which version holds authority, and whether a rollout is genuinely in
 * flight. The authority state NEVER promotes the rollout target: an
 * in-flight desired version stays `desired`, and only reaching "activated"
 * stops `rolling` among the transition states (a "superseded" rollout was
 * never rolling toward activation at all). Missing desired falls back to
 * active. Verbatim (3807–3816):
 *   var active = Number(activeValue) || null;
 *   var desired = Number(desiredValue) || active;
 *   return { active: active, desired: desired,
 *     rolling: !!(active && desired && desired > active &&
 *       (status === "collecting" || status === "blocked_ineligible")) };
 */
export function benchmarkAuthorityState(
  activeValue: unknown,
  desiredValue: unknown,
  status: string | null | undefined,
): BenchAuthorityState {
  const active = Number(activeValue) || null;
  const desired = Number(desiredValue) || active;
  return {
    active,
    desired,
    rolling: !!(
      active &&
      desired &&
      desired > active &&
      (status === "collecting" || status === "blocked_ineligible")
    ),
  };
}

export interface LeaderboardBenchState {
  active: number | null;
  desired: number | null;
  selected: number | null;
}

/**
 * The leaderboard's own version view: `selected` (what the board shows) is
 * independent of active/desired, so a historical view can select v5 while
 * v6 is active and v7 rolling out — and the rollout target never overwrites
 * the current bench. Verbatim (3818–3829):
 *   var active = Number(activeValue) || null;
 *   var desired = Number(desiredValue) || active;
 *   var selected = selectionMode === "historical"
 *     ? Number(currentValue) || null
 *     : active;
 *   return { active: active, desired: desired,
 *     selected: selected || Number(currentValue) || Number(fallbackValue) || null };
 */
export function leaderboardBenchState(
  selectionMode: string | null | undefined,
  currentValue: unknown,
  activeValue: unknown,
  desiredValue: unknown,
  fallbackValue: unknown,
): LeaderboardBenchState {
  const active = Number(activeValue) || null;
  const desired = Number(desiredValue) || active;
  const selected = selectionMode === "historical" ? Number(currentValue) || null : active;
  return {
    active,
    desired,
    selected: selected || Number(currentValue) || Number(fallbackValue) || null,
  };
}

/**
 * The SETTLED benchmark version — the baseline "old" is measured against
 * (3799–3801): `Number(activeBench) || Number(currentBench) || null`.
 * During a rollout the active version is the baseline; tagging it "old"
 * would misread as stale data.
 */
export function settledBenchVersion(activeBench: unknown, currentBench: unknown): number | null {
  return Number(activeBench) || Number(currentBench) || null;
}

/** The version shown in headers/subtitles (3803–3805) — same resolution as
 * settledBenchVersion, kept as its own name to match the original. */
export function benchmarkDisplayVersion(
  activeBench: unknown,
  currentBench: unknown,
): number | null {
  return Number(activeBench) || Number(currentBench) || null;
}

/**
 * The DittoBench header badge text (renderBenchBadge, 3831–3849): names the
 * rollout transition instead of a bare "latest" claim —
 *   rolling: "DittoBench v{active} → v{desired} rollout"
 *   settled: "DittoBench v{active}" (+ " · older runs marked" when older
 *            runs share the board)
 * Empty string when no active version is known yet (the badge stays hidden
 * rather than showing a hardcoded — possibly wrong — version).
 */
export function benchBadgeLabel(
  authority: BenchAuthorityState,
  benchHasOlderRuns: boolean,
): string {
  const active = authority.active;
  const desired = authority.desired;
  if (!active) return "";
  return authority.rolling
    ? "DittoBench v" + active + " → v" + desired + " rollout"
    : "DittoBench v" + active + (benchHasOlderRuns ? " · older runs marked" : "");
}

export interface RolloutStripState {
  active: number;
  desired: number;
  status: string;
  /** An open rollout is in progress (weights will eventually flip). */
  rolling: boolean;
  /** Still collecting scores at the desired version. */
  collecting: boolean;
}

/**
 * The rollout strip's derived flags (renderRollout, 3721–3735). A superseded
 * rollout still carries its old desired_version, so it must not be described
 * as "rolling out": nothing is collecting for it and weights will never move
 * to it. Verbatim:
 *   var status = String(state.status || "inactive");
 *   var rolling = desired > active && status !== "superseded";
 *   var collecting = status === "collecting" || status === "blocked_ineligible";
 * Null when either version is missing (the strip hides).
 */
export function rolloutStripState(
  state: RolloutState | null | undefined,
): RolloutStripState | null {
  const active = Number(state && state.active_version);
  const desired = Number(state && state.desired_version);
  if (!state || !active || !desired) return null;
  const status = String(state.status || "inactive");
  return {
    active,
    desired,
    status,
    rolling: desired > active && status !== "superseded",
    collecting: status === "collecting" || status === "blocked_ineligible",
  };
}

/**
 * The contract version an open rollout is still collecting for — the memory
 * timeline marks that era's band "open" (4348–4356):
 *   var rolloutStatus = String(lastRollout.status || "");
 *   var desiredVersion = Number(lastRollout.desired_version);
 *   if (rolloutStatus !== "activated" && rolloutStatus !== "superseded" && …)
 *     openVersion = desiredVersion;
 * Null when no rollout is open (activated and superseded eras are settled;
 * the chart caller still checks the band exists before marking it).
 */
export function rolloutCollectingVersion(state: RolloutState | null | undefined): number | null {
  if (!state) return null;
  const status = String(state.status || "");
  const desired = Number(state.desired_version);
  if (!Number.isFinite(desired) || !desired) return null;
  return status !== "activated" && status !== "superseded" ? desired : null;
}
