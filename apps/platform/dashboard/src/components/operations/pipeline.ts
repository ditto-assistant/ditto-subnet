// Pure pipeline-board logic (monolith 7905–7974 + the column fold inside
// renderPipelineBoard 7976–8106). The original closed over activeBench /
// currentBench module state; these take the resolved active version as an
// argument and stay pure.
import type { FleetEntry, FleetReport } from "../../types/fleet";
import type { BenchmarkProgress, PipelineEntry } from "../../types/pipeline";

/** Ops-feed fields beyond the shared wire type (the feed superset carries
 * the queue gate the shared PipelineEntry does not declare). */
export interface PipelineEntryExt extends PipelineEntry {
  validator_queue_gate?: string | null;
}

export interface IndexedEntry {
  entry: PipelineEntryExt;
  index: number;
  /** Stable across polls, so the lane's <For> can keep a card's DOM node when
   * the 5s snapshot re-reports the same submission. `index` is the position
   * in the snapshot and shifts whenever anything is admitted ahead of it, so
   * it cannot be the identity — the submission id is. */
  key: string;
}

/** A lane row's identity. One agent_id appears at most once per lane, so it
 * keys the row; a snapshot row without one falls back to its position, which
 * is no worse than the positional identity <For> had before. */
function entryKey(entry: PipelineEntryExt, index: number): string {
  const id = entry.agent_id;
  return id == null || id === "" ? "i:" + index : "a:" + String(id);
}

/** Waiting-lane order: validator queue rank ascending, unranked last
 * (7905–7908). */
export function validatorQueueCompare(a: IndexedEntry, b: IndexedEntry): number {
  return (
    Number(a.entry.validator_queue_rank || Number.MAX_SAFE_INTEGER) -
    Number(b.entry.validator_queue_rank || Number.MAX_SAFE_INTEGER)
  );
}

/** Whether a live benchmark slot belongs to the queue the board describes
 * (7910–7915): current-or-newer versions only; with no known active version
 * any positive version counts. */
export function queueRelevantBenchmark(
  progress: BenchmarkProgress | null | undefined,
  activeVersion: number | null,
): boolean {
  const version = Number(progress && progress.bench_version);
  const active = Number(activeVersion);
  return (
    Number.isInteger(version) &&
    version > 0 &&
    (!Number.isInteger(active) || active <= 0 || version >= active)
  );
}

/** Which work-in-progress column a submission renders in (7917–7921).
 * Scored/live lifecycle membership is handled independently below because a
 * continual retest adds live work without replacing the canonical score. */
export function pipelineBoardStage(entry: PipelineEntry, activeVersion: number | null): string {
  return (entry.active_benchmarks || []).some((progress) =>
    queueRelevantBenchmark(progress, activeVersion),
  )
    ? "evaluating"
    : String(entry.status || "");
}

export interface QueueGate {
  label: string;
  title: string;
  aria: string;
}

/** Why a waiting submission cannot be leased on the next poll, from the
 * API's validator_queue_gate. Null when nothing holds it, which is the ONLY
 * state that may ever be badged "Up next" (7940–7956). */
export const QUEUE_GATES: Record<string, QueueGate> = {
  previous_generation: {
    label: "Prev gen",
    title:
      "Submitted before the current benchmark version. Previous-generation work is served only " +
      "after every current-era submission has a validator, so this is not advancing yet.",
    aria: "previous benchmark generation, waiting for the current era to finish",
  },
  owner_serialized: {
    label: "Owner queued",
    title:
      "Another submission from the same owner is using their validator slot, so this one does " +
      "not get a turn while anybody else is waiting. It is not stuck: a validator that finds no " +
      "other owner's work eligible may still lease it rather than idle, up to the per-owner " +
      "limit on the queue policy board. Rotating hotkeys does not add a slot.",
    aria: "queued behind another submission from the same owner",
  },
  not_leasable: {
    label: "Not queued",
    title:
      "Not currently eligible for validator assignment: withdrawn from the queue, not admitted " +
      "to this benchmark era, or missing its dataset or screened image.",
    aria: "not currently eligible for validator assignment",
  },
};

export function queueGateLabel(entry: PipelineEntryExt): QueueGate | null {
  const gate = entry.validator_queue_gate;
  return (gate != null && QUEUE_GATES[gate]) || null;
}

export interface RescoreState {
  targetVersion: number;
  sourceVersion: number | null;
  /** target > source: an inherited-cohort qualification on the next bench. */
  isQualification: boolean;
}

/** Why a non-evaluating-status card shows live work (7962–7974). */
export function pipelineRescoreState(
  entry: PipelineEntry,
  activeVersion: number | null,
): RescoreState | null {
  if (entry.status === "evaluating") return null;
  const targetVersion = Math.max(
    ...(entry.active_benchmarks || [])
      .filter((progress) => queueRelevantBenchmark(progress, activeVersion))
      .map((progress) => Number(progress.bench_version) || 0),
  );
  if (!isFinite(targetVersion) || targetVersion <= 0) return null;
  const sourceVersion = Number(activeVersion) || null;
  return {
    targetVersion,
    sourceVersion,
    isQualification: sourceVersion !== null && targetVersion > sourceVersion,
  };
}

/** The screening-policy chip on a waiting/screening card (6800–6806). */

export interface RescreenNoticeView {
  requiredPolicy: number;
  /** Confirmed rescreens (a completed prior screening exists). */
  count: number;
  /** Rescreens with scores on record. */
  scored: number;
}

/** The policy-rescreen notice, derived from public queue state alone
 * (renderPolicyRescreenNotice 6808–6832); null keeps the notice hidden. */
export function policyRescreenView(
  entries: PipelineEntry[],
  unavailable: boolean,
): RescreenNoticeView | null {
  const queued = unavailable
    ? []
    : entries.filter((entry) => {
        const completed = Number(entry.screening_policy_version);
        const required = Number(entry.required_screening_policy_version);
        const inScreening = entry.status === "waiting_screening" || entry.status === "screening";
        return (
          inScreening &&
          Number.isFinite(completed) &&
          Number.isFinite(required) &&
          completed < required
        );
      });
  const rescreens = queued.filter((entry) => Number(entry.screening_policy_version) > 0);
  if (!rescreens.length) return null;
  const requiredPolicy = Math.max(
    ...rescreens.map((entry) => Number(entry.required_screening_policy_version)),
  );
  const scored = rescreens.filter((entry) => Number(entry.score_count) > 0).length;
  return { requiredPolicy, count: rescreens.length, scored };
}

/** The screener currently holding this agent, from the separate screener
 * feed (activeScreenerFor 8263–8269) — the cross-feed that puts live stages
 * on Screening cards. */
export function activeScreenerFor(
  screeners: FleetReport | null | undefined,
  agentId: string | null | undefined,
): FleetEntry | null {
  const list = (screeners && screeners.screeners) || [];
  return (
    list.find((entry) => entry.active_agent_id === agentId && entry.screening_progress) || null
  );
}

export interface PipelineColumnDef {
  status: string;
  statuses: string[];
  /** DOM ids from the monolith's id ledger (operations section). */
  bodyId: string;
  countId: string;
  titleId: string;
  title: string;
  node: string;
  empty: string;
}

export const PIPELINE_COLUMNS: readonly PipelineColumnDef[] = [
  {
    status: "admission",
    statuses: ["waiting_screening", "screening"],
    bodyId: "pipeline-admission",
    countId: "pipeline-admission-count",
    titleId: "pipeline-admission-title",
    title: "Build & admission",
    node: "1",
    empty: "No submissions awaiting admission.",
  },
  {
    status: "waiting_validator",
    statuses: ["waiting_validator", "below_score_floor"],
    bodyId: "pipeline-wait-validator",
    countId: "pipeline-wait-validator-count",
    titleId: "pipeline-wait-validator-title",
    title: "Waiting for validators",
    node: "2",
    empty: "No submissions waiting.",
  },
  {
    status: "evaluating",
    statuses: ["evaluating"],
    bodyId: "pipeline-evaluating",
    countId: "pipeline-evaluating-count",
    titleId: "pipeline-evaluating-title",
    title: "Scoring",
    node: "3",
    empty: "No active evaluation.",
  },
  {
    status: "scored",
    statuses: ["scored", "live"],
    bodyId: "pipeline-scored",
    countId: "pipeline-scored-count",
    titleId: "pipeline-scored-title",
    title: "Scored & live",
    node: "4",
    empty: "No scores yet. Finalized agents will appear here.",
  },
];

export interface PipelineColumnView {
  def: PipelineColumnDef;
  items: IndexedEntry[];
  /** The headline number (authoritative status_counts, except Evaluating
   * which counts the same live slot rows the board renders). */
  displayedCount: number;
  stuckCount: number;
  hiddenCount: number;
  active: boolean;
}

/** The whole board fold (renderPipelineBoard 7996–8102, minus the DOM). */
export function pipelineColumnViews(
  entries: PipelineEntryExt[],
  statusCounts: Record<string, number>,
  showStuck: boolean,
  activeVersion: number | null,
): PipelineColumnView[] {
  return PIPELINE_COLUMNS.map((def) => {
    let indexed: IndexedEntry[] = [];
    entries.forEach((entry, index) => {
      // A rescore is additive state: keep the accepted canonical score in the
      // scored/live lane while also projecting its active work into Scoring.
      // Other lifecycle rows still move into Scoring while their first score
      // is being produced.
      const stage =
        def.status === "scored"
          ? String(entry.status || "")
          : pipelineBoardStage(entry, activeVersion);
      if (def.statuses.indexOf(stage) >= 0) {
        indexed.push({ entry, index, key: entryKey(entry, index) });
      }
    });
    if (def.status === "scored") {
      indexed = indexed
        .slice()
        .sort(
          (a, b) =>
            new Date(b.entry.last_scored_at || b.entry.submitted_at || 0).getTime() -
            new Date(a.entry.last_scored_at || a.entry.submitted_at || 0).getTime(),
        );
    } else if (def.status === "waiting_validator") {
      indexed = indexed.slice().sort(validatorQueueCompare);
    }
    const stuckCount = indexed.reduce(
      (n, item) => n + (item.entry.retry_state === "exhausted" ? 1 : 0),
      0,
    );
    if (def.status === "waiting_validator") {
      indexed = indexed.filter((item) =>
        showStuck ? item.entry.retry_state === "exhausted" : item.entry.retry_state !== "exhausted",
      );
    }
    const authoritativeCount = def.statuses.reduce(
      (total, status) => total + Number(statusCounts[status] || 0),
      0,
    );
    // Finalized top-five agents keep their scored/live lifecycle while a
    // continual retest is running; count the same slot rows the board
    // renders so the headline cannot claim zero while live work shows.
    const displayedCount =
      def.status === "evaluating"
        ? indexed.reduce(
            (total, item) =>
              total +
              (item.entry.active_benchmarks || []).filter((progress) =>
                queueRelevantBenchmark(progress, activeVersion),
              ).length,
            0,
          )
        : authoritativeCount;
    const visiblePopulation =
      def.status === "waiting_validator"
        ? showStuck
          ? stuckCount
          : Math.max(0, authoritativeCount - stuckCount)
        : def.status === "evaluating"
          ? indexed.length
          : authoritativeCount;
    const hiddenCount = Math.max(0, visiblePopulation - indexed.length);
    return {
      def,
      items: indexed,
      displayedCount,
      stuckCount,
      hiddenCount,
      active: indexed.length > 0,
    };
  });
}

/** Compact card version chip ("v1", "Legacy") — the aria-label keeps the long
 * "Submission v1" form (pipelineAgentVersionLabel, drift #633). */
export function pipelineAgentVersionLabel(version: number | string | null | undefined): string {
  return version == null ? "Legacy" : "v" + version;
}

// ── Source integrity review branch (weekend drift: #623/#635) ───────────────

export interface IntegrityReviewView {
  /** Authoritative status_counts.under_review, falling back to the rows the
   * activity window actually carries (renderIntegrityReviewBranch 8339–8342). */
  count: number;
  /** The rows shown on the board (first three). */
  shown: IndexedEntry[];
  /** How many more sit beyond the board's cap ("N more in Activity"). */
  moreCount: number;
}

/** Reason line for a held submission; the fallback names the branch's two
 * admission criteria rather than pretending to know which one fired. */
export function integrityReviewReason(entry: PipelineEntryExt): string {
  return entry.review_reason || entry.screening_reason || "Qualification or anomaly review";
}

export function integrityReviewView(
  entries: PipelineEntryExt[],
  statusCounts: Record<string, number>,
): IntegrityReviewView {
  const indexed: IndexedEntry[] = [];
  entries.forEach((entry, index) => {
    if (entry.status === "under_review") {
      indexed.push({ entry, index, key: entryKey(entry, index) });
    }
  });
  return {
    count: Number(statusCounts.under_review || indexed.length),
    shown: indexed.slice(0, 3),
    moreCount: Math.max(0, indexed.length - 3),
  };
}
