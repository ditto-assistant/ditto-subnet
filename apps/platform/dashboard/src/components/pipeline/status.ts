// The public submission-status vocabulary (monolith activityStage 6832–6850,
// reviewEventLabel/reviewEvidenceText/reviewEvidenceHtml 6852–6881,
// policyScreeningLabel 6892–6905, validationProgress 6883–6890,
// scoreFloorAttribution 6939–6948, validationDetail 6950–7012,
// benchmarkVersionKey/Label 7185–7192, retestAttemptCounts 7259–7270 —
// current line numbers) plus the activity filter/status constants (3162,
// 3175–3184). Everything here is pure and shared by the submissions table,
// the agent evidence drawer, and (vocabulary-wise) the operations board and
// global search.
import type { ChipState } from "../ui/StatusChip";
import { agentLabel, fx } from "../../lib/format";
import type { ActivityEntry, ValidationAttempt } from "../../types/pipeline";

/** One page of the paged activity table (monolith 3123). */
export const ACTIVITY_PAGE_SIZE = 10;

/** The status whitelist, canonical order (monolith 3142–3145). URL and
 * filter state are always re-ordered against this list. */
export const ACTIVITY_STATUSES: readonly string[] = [
  "waiting_screening",
  "screening",
  "waiting_validator",
  "evaluating",
  "below_score_floor",
  "not_queued",
  "retired",
  "under_review",
  "rejected",
  "scored",
  "live",
];

/** Quick-filter → server statuses (monolith 3136–3141). */
export const ACTIVITY_FILTERS: Record<string, readonly string[]> = {
  rejected: ["rejected"],
  under_review: ["under_review"],
  waiting_validator: ["waiting_validator", "below_score_floor"],
  queued: ["waiting_screening", "screening", "waiting_validator", "below_score_floor"],
};

/** Filter names in band order (markup 2869–2873). */
export const ACTIVITY_FILTER_NAMES: readonly string[] = [
  "all",
  "rejected",
  "under_review",
  "waiting_validator",
  "queued",
  "downloadable",
];

/** Band button labels (markup 2906–2912; #623 renamed the review and
 * validator filters with the admission-vs-integrity vocabulary). */
export const ACTIVITY_FILTER_LABELS: Record<string, string> = {
  all: "All",
  rejected: "Rejected",
  under_review: "Integrity review",
  waiting_validator: "Waiting for validators",
  queued: "Queued work",
  downloadable: "Downloadable",
};

/**
 * Stage pill per status (activityStage 6832–6850). Terminal states for a
 * closed benchmark generation (#462) read as history, not failure:
 * not_queued/retired are neutral, never "bad". #623 renamed the screening
 * stages to the mechanical-admission vocabulary: screening builds a verified
 * image, and "Source integrity review" is the conditional later branch —
 * deep source review is deferred until a score qualifies.
 */
export function activityStage(status: string | null | undefined): ChipState {
  const stages: Record<string, ChipState> = {
    uploaded: ["Waiting for admission", "progress"],
    waiting_screening: ["Waiting for admission", "progress"],
    screening: ["Image build & admission", "progress"],
    screening_passed: ["Admitted", "good"],
    screening_failed: ["Admission interrupted", "warn"],
    waiting_validator: ["Waiting for validators", "progress"],
    evaluating: ["Scoring", "progress"],
    below_score_floor: ["Low-priority completion", "warn"],
    not_queued: ["Historical · not queued", ""],
    retired: ["Retired · earlier benchmark", ""],
    scored: ["Scored", "good"],
    live: ["Live", "good"],
    under_review: ["Source integrity review", "warn"],
    rejected: ["Rejected", "bad"],
  };
  return (status != null && stages[status]) || ["Pending", ""];
}

// ── Review-event evidence (#622/#636; monolith 6852–6881) ───────────────────

/** Fields the review-evidence and policy-label helpers read. */
export interface ReviewEventFields {
  review_event?: string | null;
  review_reason?: string | null;
  review_original_reason?: string | null;
  duplicate_of?: string | null;
}

/** "opened" → "Operator review", plus the reopened/cleared/rejected states
 * (#622; reviewEventLabel 6852–6860). Unknown events read as the plain hold. */
export function reviewEventLabel(entry: ReviewEventFields): string {
  const labels: Record<string, string> = {
    opened: "Operator review",
    reopened: "Review reopened",
    cleared: "Review cleared",
    rejected: "Review rejected",
  };
  return (entry.review_event != null && labels[entry.review_event]) || "Operator review";
}

export interface ReviewEvidenceNote {
  label: string;
  text: string;
}

/**
 * The stage-cell review notes (#622; reviewEvidenceHtml 6871–6881): the
 * CURRENT review reason under its event label, with the initial hold reason
 * kept as history when it differs. Empty without a review reason.
 */
export function reviewEvidenceNotes(entry: ReviewEventFields): ReviewEvidenceNote[] {
  if (!entry.review_reason) return [];
  const notes: ReviewEvidenceNote[] = [
    { label: reviewEventLabel(entry), text: entry.review_reason },
  ];
  if (entry.review_original_reason && entry.review_original_reason !== entry.review_reason) {
    notes.push({ label: "Initial hold", text: entry.review_original_reason });
  }
  return notes;
}

/** Plain-text twin for the drawer's evidence line (reviewEvidenceText
 * 6862–6869). */
export function reviewEvidenceText(entry: ReviewEventFields): string {
  if (!entry.review_reason) return "";
  let text = reviewEventLabel(entry) + ": " + entry.review_reason;
  if (entry.review_original_reason && entry.review_original_reason !== entry.review_reason) {
    text += " Initial hold: " + entry.review_original_reason;
  }
  return text;
}

/**
 * #636: once a review has moved past its opening event, the mechanical
 * duplicate-claim reason stays out of the current-evidence channel — the
 * comparison reads as the initial one, not the live reason.
 */
export function duplicateComparisonLabel(entry: ReviewEventFields): string {
  return entry.review_event && entry.review_event !== "opened"
    ? "Initial comparison"
    : "Compared with";
}

/**
 * The screening-policy chip (policyScreeningLabel 6892–6905). #623: a
 * build-only screening pass IS the active build stage — calling the image
 * "verified" before that build finishes was both redundant and temporally
 * false, so it renders nothing; a full review during screening names the
 * deferred branch, "Source integrity review".
 */
export function policyScreeningLabel(entry: {
  status?: string | null;
  screening_build_only?: boolean | null;
  screening_policy_version?: number | null;
  required_screening_policy_version?: number | null;
}): string {
  if (entry.screening_build_only === true) return "";
  if (entry.screening_build_only === false && entry.status === "screening") {
    return "Source integrity review";
  }
  const completed = Number(entry.screening_policy_version);
  const required = Number(entry.required_screening_policy_version);
  if (!Number.isFinite(completed) || !Number.isFinite(required) || completed >= required) {
    return "";
  }
  if (completed > 0) return "Rescreen · policy v" + completed + " → v" + required;
  return "Policy v" + required + " screening";
}

/** Fields beyond the base ActivityEntry wire type that the status copy and
 * the drawer read (all optional on the wire). */
export interface ActivityStatusFields {
  active_bench_version?: number | null;
  provisional_scores?: Array<{ composite: number | string }> | null;
  validation_attempts?: ValidationAttempt[] | null;
  score_floor_agent_id?: string | null;
  score_floor_agent_name?: string | null;
  score_floor_agent_version?: number | null;
  review_event?: string | null;
  review_original_reason?: string | null;
  screening_build_only?: boolean | null;
}

export type ActivityStatusEntry = ActivityEntry & ActivityStatusFields;

/**
 * The Validation column line (validationProgress 6790–6798). #458/#462:
 * a previous-generation waiting row reads "queued last", a closed-generation
 * row reads "benchmark closed" / "Not in active benchmark queue".
 */
export function validationProgress(e: ActivityStatusEntry): string {
  const count = Math.max(0, Number(e.score_count) || 0);
  const quorum = Math.max(1, Number(e.quorum) || 3);
  if (e.status === "below_score_floor") return count + " of " + quorum + " · queued last";
  if (e.status === "not_queued") return "Not in active benchmark queue";
  if (e.status === "retired") return count + " of " + quorum + " · benchmark closed";
  const hasStarted =
    count > 0 ||
    ["waiting_validator", "evaluating", "scored", "live", "under_review"].indexOf(e.status || "") >=
      0;
  return hasStarted ? count + " of " + quorum : "Not started";
}

/** "unknown" or the positive integer version as a string (7085–7088). */
export function benchmarkVersionKey(version: unknown): string {
  const numeric = Number(version);
  return version != null && Number.isInteger(numeric) && numeric > 0 ? String(numeric) : "unknown";
}

/** "Bench v{n}" / "Bench version unknown" (7090–7092). */
export function benchmarkVersionLabel(key: string): string {
  return key === "unknown" ? "Bench version unknown" : "Bench v" + key;
}

/** Continual-retest lease tallies (retestAttemptCounts 7159–7170). */
export function retestAttemptCounts(attempts: readonly ValidationAttempt[]): {
  running: number;
  assigned: number;
  expired: number;
} {
  const retests = attempts.filter((attempt) => attempt.purpose === "continual_retest");
  return {
    running: retests.filter((attempt) => attempt.actively_running).length,
    assigned: retests.filter((attempt) => attempt.status === "issued" && !attempt.actively_running)
      .length,
    expired: retests.filter((attempt) => attempt.status === "expired").length,
  };
}

/**
 * Name the row the continuation floor was cut from (#516;
 * scoreFloorAttribution 6839–6848). The floor and the leaderboard share one
 * ordering (server-side, ditto/score_order.py): both cut on
 * official_composite, so the floor IS the score of the finalized row the
 * board ranks fifth. Naming the holder keeps the claim checkable — a miner
 * opens that submission and reads the same number back.
 */
export function scoreFloorAttribution(e: ActivityStatusEntry): string {
  const benchKey = benchmarkVersionKey(e.active_bench_version);
  const where = benchKey === "unknown" ? "" : " on " + benchmarkVersionLabel(benchKey);
  const basis =
    "That floor is the 5th-highest finalized official_composite" +
    where +
    " — the same score and the same ordering the leaderboard ranks by";
  if (!e.score_floor_agent_id) return basis + ".";
  return (
    basis +
    ", held by " +
    agentLabel(e.score_floor_agent_name, e.score_floor_agent_version) +
    ". Open that submission to read the same number back."
  );
}

/**
 * The long-form "Current progress" sentence (validationDetail 6850–6912).
 * Every branch is public-state-derived; the below_score_floor branch quotes
 * the floor through scoreFloorAttribution, never an unattributed number.
 */
export function validationDetail(e: ActivityStatusEntry): string {
  const count = Math.max(0, Number(e.score_count) || 0);
  const quorum = Math.max(1, Number(e.quorum) || 3);
  const assignments = (e.validation_attempts || []).filter(
    (attempt) =>
      attempt.status === "issued" &&
      benchmarkVersionKey(attempt.bench_version) === benchmarkVersionKey(e.active_bench_version),
  ).length;
  if (count >= quorum) {
    const retests = retestAttemptCounts(
      (e.validation_attempts || []).filter(
        (attempt) =>
          benchmarkVersionKey(attempt.bench_version) ===
          benchmarkVersionKey(e.active_bench_version),
      ),
    );
    const retestCopy: string[] = [];
    if (retests.running) retestCopy.push(retests.running + " running");
    if (retests.assigned) retestCopy.push(retests.assigned + " assigned");
    return (
      "Canonical validation complete. The official result uses the median of " +
      quorum +
      " independent scores." +
      (retestCopy.length ? " Continual top-five retesting: " + retestCopy.join(", ") + "." : "")
    );
  }
  if (e.status === "below_score_floor") {
    const provisionalScores = e.provisional_scores || [];
    const scoreFloor = Number(e.score_floor);
    if (!provisionalScores.length || !Number.isFinite(scoreFloor)) {
      return (
        "Two accepted scores are below the current same-benchmark score floor. " +
        "The final score is still queued, after other unfinished submissions."
      );
    }
    const bestPossible = provisionalScores.reduce(
      (best, score) => Math.max(best, Number(score.composite) || 0),
      0,
    );
    return (
      "Two accepted scores mean even a perfect third run could not raise the median above " +
      fx(bestPossible) +
      ", below the continuation floor of " +
      fx(scoreFloor) +
      ". " +
      scoreFloorAttribution(e) +
      " The third score is still queued at low priority so this benchmark record reaches 3 of 3."
    );
  }
  if (e.status === "not_queued") {
    return "This historical submission is retained for audit history but is not admitted to the active benchmark queue. It cannot consume validator capacity unless an explicit audited recovery admits it.";
  }
  if (e.status === "retired") {
    return (
      "This submission was entered for an earlier benchmark generation. The subnet has since moved to a newer benchmark, and validators only score the current one, so no further scores will arrive for it. Nothing here was rejected and nothing was lost: the submission, its screening record, and the " +
      count +
      " score" +
      (count === 1 ? "" : "s") +
      " it did receive stay on file and searchable. Submitting against the current benchmark starts fresh with a full quorum."
    );
  }
  if (e.status === "waiting_screening")
    return "Queued for a screener to claim under the current policy.";
  if (e.status === "screening") return "A screener is currently checking this submission.";
  if (e.status === "under_review")
    return "Automated processing is paused while an operator reviews this submission. No screener or validator is currently working on it.";
  if (e.status === "waiting_validator") {
    const waiting = Math.max(0, quorum - count - assignments);
    const assignmentCopy =
      assignments === 0
        ? "No validator is assigned yet."
        : assignments === 1
          ? "1 validator is assigned; its score is pending."
          : assignments + " validators are assigned; their scores are pending.";
    const waitingCopy = waiting
      ? " Waiting for " +
        waiting +
        (assignments ? " more " : " ") +
        (waiting === 1 ? "validator" : "validators") +
        "."
      : "";
    return count + " of " + quorum + " scores received. " + assignmentCopy + waitingCopy;
  }
  if (e.status === "evaluating" || count > 0) {
    const remaining = quorum - count;
    return (
      count +
      " of " +
      quorum +
      " scores received. Waiting for " +
      remaining +
      " more independent " +
      (remaining === 1 ? "score" : "scores") +
      "."
    );
  }
  if (e.status === "rejected")
    return "Screening completed and rejected this submission. See the screener result for the policy version and reason.";
  if (e.status === "screening_failed")
    return "Screening could not complete reliably. This is retryable and is distinct from a submission rejection.";
  return "Validation starts after the submission passes screening.";
}
