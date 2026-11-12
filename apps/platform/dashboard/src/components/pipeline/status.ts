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
import type {
  ActivityEntry,
  DeferredReviewTrigger,
  ReviewConclusion,
  ScoringWait,
  ValidationAttempt,
} from "../../types/pipeline";

/** One page of the paged activity table (monolith 3123). */
export const ACTIVITY_PAGE_SIZE = 10;

/** The status whitelist, canonical order (monolith 3142–3145). URL and
 * filter state are always re-ordered against this list. */
export const ACTIVITY_STATUSES: readonly string[] = [
  "waiting_screening",
  "screening_failed",
  "screening",
  "waiting_validator",
  "evaluating",
  "below_score_floor",
  "not_queued",
  "retired",
  "scoring_failed",
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
  under_review: "Under review",
  waiting_validator: "Waiting for validators",
  queued: "Queued work",
  downloadable: "Source releases",
};

/** Screening failures may be infrastructure, reviewer-budget, or other
 * operator-owned outcomes. The public status alone does not identify which. */
export const SCREENING_INCOMPLETE_LABEL = "Screening incomplete";

/** The stage label for a submission whose own failures ended its scoring. */
export const SCORING_FAILED_LABEL = "Cannot finish scoring";

/** Plain language for each allowlisted agent-attributable failure code.
 *
 * Only codes the API is willing to publish appear here. An unknown code keeps
 * the terminal sentence without inventing a cause for it. */
export const TERMINAL_FAILURE_CAUSES: Record<string, string> = {
  inference_allowance_exhausted:
    "The agent used its whole inference allowance before finishing its run.",
  inference_request_rejected:
    "The platform rejected the agent's inference requests, so no run could complete.",
  model_inference_required:
    "The agent answered without the model inference the benchmark requires.",
  harness_oom:
    "The harness exceeded its published sandbox memory cap while the validator host had memory to spare.",
};

/** Whether the platform has said this artifact cannot finish scoring.
 *
 * ``scoring_failed`` is the closed-out form. A row stranded before the
 * platform began closing these out still reads ``waiting_validator`` while
 * carrying the same verdict in its retry disposition, and it must not be
 * described as waiting either. */
export function isTerminalScoringFailure(
  status: string | null | undefined,
  entry?: TerminalFailureFields | null,
): boolean {
  if (status === "scoring_failed") return true;
  if (status !== "waiting_validator" && status !== "below_score_floor") return false;
  return (
    entry?.retry_disposition === "terminal_artifact_failure" ||
    entry?.validator_retry?.disposition === "terminal_artifact_failure"
  );
}

/** The published code behind a terminal scoring failure, if any. */
export function terminalFailureCode(entry?: TerminalFailureFields | null): string | null {
  return entry?.validator_retry?.terminal_failure_code || entry?.terminal_failure_code || null;
}

/** The fields that carry a terminal scoring verdict, from either payload. */
export interface TerminalFailureFields {
  retry_disposition?: string | null;
  terminal_failure_code?: string | null;
  validator_retry?: {
    disposition?: string | null;
    terminal_failure_code?: string | null;
  } | null;
}

/** The stage label for a submission an operator withdrew from the queue. */
export const QUEUE_WITHDRAWN_LABEL = "Withdrawn · not queued";

/** The field that names an operator withdrawal, from either payload. */
export interface QueueWithdrawalFields {
  queue_withdrawn?: boolean | null;
}

/** Whether an operator withdrawal, not the era boundary, keeps a `not_queued`
 * row out of the active benchmark queue. */
export function isQueueWithdrawn(
  status: string | null | undefined,
  entry?: QueueWithdrawalFields | null,
): boolean {
  return status === "not_queued" && entry?.queue_withdrawn === true;
}

// ── Dueling-season wait (bench v16+) ────────────────────────────────────────

/** The chip a waiting row reads while no dueling season is open. */
export const SEASON_WAIT_LABEL = "Waiting for next season";

/** The fields that carry a season wait, from either payload. */
export interface ScoringWaitFields {
  scoring_wait?: ScoringWait | null;
}

/** The season wait holding a waiting row, or null when validators are the
 * only thing it waits for. */
export function seasonWait(
  status: string | null | undefined,
  entry?: ScoringWaitFields | null,
): ScoringWait | null {
  if (status !== "waiting_validator" && status !== "below_score_floor") return null;
  const wait = entry?.scoring_wait;
  return wait?.reason === "dueling_season_closed" ? wait : null;
}

/** "2h 31m" / "45m" / "1d 3h", rounded up to the minute. */
function untilHm(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  if (minutes < 60) return minutes + "m";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + "h " + (minutes % 60) + "m";
  return Math.floor(hours / 24) + "d " + (hours % 24) + "h";
}

/**
 * "about 00:45 UTC (in 2h 31m)", or "" when the estimate is missing, invalid,
 * or already past. Fixed UTC rendering: never the viewer's locale or
 * timezone; the date is named only a day or more ahead, where the clock time
 * alone would be ambiguous.
 */
export function seasonOpensText(iso: string | null | undefined, now: Date = new Date()): string {
  const at = iso == null ? NaN : new Date(iso).getTime();
  if (Number.isNaN(at) || at <= now.getTime()) return "";
  const stamp = new Date(at).toISOString();
  const day = at - now.getTime() >= 86_400_000 ? stamp.slice(0, 10) + " " : "";
  return (
    "about " + day + stamp.slice(11, 16) + " UTC (in " + untilHm((at - now.getTime()) / 1000) + ")"
  );
}

/**
 * The plain-language season sentence: "No dueling season is open for bench
 * 18. Scoring resumes when the next season opens, about 00:45 UTC (in 2h
 * 31m)." Without an estimate it names the round boundary instead. When no
 * season is scheduled for the bench at all (a rollout's source version), it
 * promises no boundary.
 */
export function seasonWaitSentence(
  benchVersion: number | null | undefined,
  opensAt: string | null | undefined,
  now: Date = new Date(),
  scheduled: boolean = true,
): string {
  const bench = benchVersion != null ? " for bench " + benchVersion : "";
  if (!scheduled) {
    return (
      "No dueling season is scheduled" +
      bench +
      " right now. Scoring resumes when a" +
      (benchVersion != null ? " bench " + benchVersion : "") +
      " season opens."
    );
  }
  const when = seasonOpensText(opensAt, now);
  return (
    "No dueling season is open" +
    bench +
    ". Scoring resumes when the next season opens, " +
    (when || "at the next generator-round boundary") +
    "."
  );
}

/**
 * Stage pill per status (activityStage 6832–6850). Terminal states for a
 * closed benchmark generation (#462) read as history, not failure:
 * not_queued/retired are neutral, never "bad". A not_queued row an operator
 * withdrew names the withdrawal, not "Historical" (SN-470). #623 renamed the
 * screening stages to the mechanical-admission vocabulary: screening builds a
 * verified image. Source review runs in full before scoring; a held row reads "Under
 * review", and only a row Platform actually deferred until a score qualified
 * (it carries deferred_review_triggers) reads "Deferred source review".
 */
export function activityStage(
  status: string | null | undefined,
  entry?: DeferredReviewFields & TerminalFailureFields & ScoringWaitFields & QueueWithdrawalFields,
): ChipState {
  if (isTerminalScoringFailure(status, entry)) return [SCORING_FAILED_LABEL, "bad"];
  // Validators turn the work away until a season opens, so "waiting for
  // validators" would name the wrong thing.
  if (seasonWait(status, entry)) return [SEASON_WAIT_LABEL, "progress"];
  if (isQueueWithdrawn(status, entry)) return [QUEUE_WITHDRAWN_LABEL, ""];
  const stages: Record<string, ChipState> = {
    uploaded: ["Waiting for admission", "progress"],
    waiting_screening: ["Waiting for admission", "progress"],
    screening: ["Image build & admission", "progress"],
    screening_passed: ["Admitted", "good"],
    screening_failed: [SCREENING_INCOMPLETE_LABEL, "warn"],
    waiting_validator: ["Waiting for validators", "progress"],
    evaluating: ["Scoring", "progress"],
    below_score_floor: ["Low-priority completion", "warn"],
    not_queued: ["Historical · not queued", ""],
    retired: ["Retired · earlier benchmark", ""],
    scoring_failed: [SCORING_FAILED_LABEL, "bad"],
    scored: ["Scored", "good"],
    live: ["Live", "good"],
    under_review: ["Under review", "warn"],
    rejected: ["Rejected", "bad"],
  };
  // #562: entering the branch is not a finding. Only an adverse automated
  // signal (or a hold with no automated conclusion, e.g. a copy review) keeps
  // "warn"; a review that ended without a finding is neutral, and one that
  // has not reported yet is in progress.
  if (status === "under_review") {
    const label = isDeferredReview(entry) ? "Deferred source review" : "Under review";
    const conclusion = entry?.review_conclusion;
    if (
      conclusion === "no_finding" ||
      conclusion === "budget_exhausted" ||
      conclusion === "not_completed"
    ) {
      return [label, ""];
    }
    if (conclusion === "pending") return [label, "progress"];
    return [label, "warn"];
  }
  return (status != null && stages[status]) || ["Pending", ""];
}

/** Public deferred-review fields Platform projects onto a held row (#562). */
export interface DeferredReviewFields {
  status?: string | null;
  deferred_review_triggers?: readonly DeferredReviewTrigger[] | null;
  review_conclusion?: ReviewConclusion | null;
}

/** True only for a hold Platform deferred until a score qualified (#562). */
export function isDeferredReview(entry: DeferredReviewFields | undefined): boolean {
  return (entry?.deferred_review_triggers?.length ?? 0) > 0;
}

/** Why a submission entered the deferred branch, in the miner's words. */
export const DEFERRED_REVIEW_TRIGGER_LABELS: Record<DeferredReviewTrigger, string> = {
  top_five: "Score qualified (top 5)",
  anomaly: "Anomaly hold",
};

/** What the automated source review concluded, as a short clause. */
export const REVIEW_CONCLUSION_LABELS: Record<ReviewConclusion, string> = {
  pending: "automated review pending",
  // No review completed with a recorded conclusion (no recorded audit, or a
  // preflight hold). Claim only that: not whether or how far review ran, and
  // never a budget.
  not_completed: "automated review did not complete \u2014 no finding recorded",
  no_finding: "automated review inconclusive \u2014 no finding",
  // Only for an exact recorded budget-exhaustion audit.
  budget_exhausted: "automated review ran out of budget \u2014 no finding",
  adverse_signal: "automated review raised a concern",
};

/** True when a recorded automated review ran and ended with no finding. */
export function isSourceReviewIncomplete(entry: DeferredReviewFields): boolean {
  return (
    entry.status === "under_review" &&
    (entry.review_conclusion === "no_finding" || entry.review_conclusion === "budget_exhausted")
  );
}

/**
 * The trigger and automated conclusion shown beside the chip without opening
 * the drawer (#562), e.g. "Score qualified (top 5) · automated review
 * incomplete — no finding". Empty for rows that are not held or that carry
 * neither field (older API, copy holds).
 */
export function deferredReviewSummary(entry: DeferredReviewFields): string {
  if (entry.status !== "under_review") return "";
  const parts = (entry.deferred_review_triggers ?? [])
    .map((trigger) => DEFERRED_REVIEW_TRIGGER_LABELS[trigger])
    .filter(Boolean);
  const conclusion = entry.review_conclusion
    ? REVIEW_CONCLUSION_LABELS[entry.review_conclusion]
    : undefined;
  if (conclusion) parts.push(conclusion);
  return parts.join(" \u00b7 ");
}

// ── Review-event evidence (#622/#636; monolith 6852–6881) ───────────────────

/** Fields the review-evidence and policy-label helpers read. */
export interface ReviewEventFields {
  review_event?: string | null;
  review_reason?: string | null;
  review_original_reason?: string | null;
  duplicate_of?: string | null;
  miner_hotkey?: string | null;
  duplicate_hotkey?: string | null;
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
 * Same miner re-uploaded or renamed a previously rejected row. The public
 * comparison must say that — "Compared with Zeus_v11 v6" reads like a
 * different owner's agent.
 */
export function sameMinerDuplicate(entry: ReviewEventFields): boolean {
  return Boolean(
    entry.miner_hotkey && entry.duplicate_hotkey && entry.miner_hotkey === entry.duplicate_hotkey,
  );
}

/**
 * #636: once a review has moved past its opening event, the mechanical
 * duplicate-claim reason stays out of the current-evidence channel — the
 * comparison reads as the initial one, not the live reason. Same-miner
 * holds name the rejected ancestor instead of implying a cross-owner copy.
 */
export function duplicateComparisonLabel(entry: ReviewEventFields): string {
  if (sameMinerDuplicate(entry)) return "Previously rejected as";
  return entry.review_event && entry.review_event !== "opened"
    ? "Initial comparison"
    : "Compared with";
}

/**
 * The screening-policy chip (policyScreeningLabel 6892–6905). #623: a
 * build-only screening pass IS the active build stage — calling the image
 * "verified" before that build finishes was both redundant and temporally
 * false, so it renders nothing; a full review during screening reads "Full
 * source review".
 */
export function policyScreeningLabel(entry: {
  status?: string | null;
  screening_build_only?: boolean | null;
  screening_policy_version?: number | null;
  required_screening_policy_version?: number | null;
}): string {
  if (entry.screening_build_only === true) return "";
  if (entry.screening_build_only === false && entry.status === "screening") {
    return "Full source review";
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

export type ActivityStatusEntry = ActivityEntry &
  ActivityStatusFields &
  TerminalFailureFields &
  ScoringWaitFields;

/**
 * The Validation column line (validationProgress 6790–6798). #458/#462:
 * a previous-generation waiting row reads "queued last", a closed-generation
 * row reads "benchmark closed" / "Not in active benchmark queue".
 */
export function validationProgress(e: ActivityStatusEntry): string {
  const count = Math.max(0, Number(e.score_count) || 0);
  const quorum = Math.max(1, Number(e.quorum) || 3);
  // Before the lane labels: a below-floor or waiting row carrying a terminal
  // verdict is not queued for anything.
  if (isTerminalScoringFailure(e.status, e)) return count + " of " + quorum + " · cannot finish";
  if (e.status === "below_score_floor") return count + " of " + quorum + " · queued last";
  if (isQueueWithdrawn(e.status, e)) return count + " of " + quorum + " · withdrawn";
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
export function validationDetail(e: ActivityStatusEntry, now: Date = new Date()): string {
  const count = Math.max(0, Number(e.score_count) || 0);
  const quorum = Math.max(1, Number(e.quorum) || 3);
  const assignments = (e.validation_attempts || []).filter(
    (attempt) =>
      attempt.status === "issued" &&
      benchmarkVersionKey(attempt.bench_version) === benchmarkVersionKey(e.active_bench_version),
  ).length;
  if (isTerminalScoringFailure(e.status, e)) {
    const code = terminalFailureCode(e);
    const cause = (code && TERMINAL_FAILURE_CAUSES[code]) || "";
    return (
      "This submission cannot finish scoring. " +
      (cause ? cause + " " : "") +
      "That happened on every remaining validator run, so " +
      count +
      " of " +
      quorum +
      " scores is final for this benchmark version: no validator will be assigned again and re-running the same artifact would repeat the failure. Fix the artifact and submit a new version. This is not a rejection; the submission, its screening record and its validator history stay on file."
    );
  }
  const wait = seasonWait(e.status, e);
  if (wait) {
    return (
      count +
      " of " +
      quorum +
      " scores received. " +
      seasonWaitSentence(
        wait.bench_version,
        wait.next_season_opens_at,
        now,
        wait.season_scheduled !== false,
      )
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
  if (isQueueWithdrawn(e.status, e)) {
    const kept =
      count > 0
        ? "the submission and its " + count + " accepted score" + (count === 1 ? "" : "s")
        : "the submission and its screening record";
    return (
      "An operator withdrew this submission from the active benchmark queue, so validators will not score it while the withdrawal stands. This is not a rejection: " +
      kept +
      " stay on file."
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
  if (e.status === "under_review") {
    const held = isDeferredReview(e)
      ? "This submission is held for deferred source review. Existing scores do not clear the hold. "
      : "This submission is held for review. Holding is not a finding, and existing scores do not clear the hold. ";
    const why = e.deferred_review_triggers?.length
      ? "It entered review because " +
        e.deferred_review_triggers
          .map((trigger) =>
            trigger === "top_five"
              ? "its score placed it in the top five"
              : "a score anomaly check fired",
          )
          .join(" and ") +
        "; entering review is not a finding. "
      : "";
    if (e.review_conclusion === "budget_exhausted") {
      return (
        held +
        why +
        "The automated review ran out of budget before finishing, which is not a finding; an operator decision is pending."
      );
    }
    if (e.review_conclusion === "no_finding") {
      return (
        held +
        why +
        "The automated review finished without reaching a decision and made no finding; an operator decision is pending."
      );
    }
    if (e.review_conclusion === "not_completed") {
      return (
        held +
        why +
        "The automated review did not complete and recorded no finding; an operator decision is pending."
      );
    }
    if (e.review_conclusion === "pending") {
      return held + why + "The automated deep review has not reported yet.";
    }
    if (e.review_conclusion === "adverse_signal") {
      return (
        held +
        why +
        "The automated review raised a concern that an operator must adjudicate; an operator decision is pending."
      );
    }
    return (
      held +
      why +
      "The screening history below shows whether a deep review is running or an operator decision is pending."
    );
  }
  if (e.status === "rejected")
    return "Screening or source review rejected this submission. Existing scores remain as history; see the review result for the policy version and reason.";
  if (e.status === "screening_failed")
    return "Screening could not complete. This is not a submission rejection. Check the admission retry status below for whether a retry is authorized or operator action is needed.";
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
  return "Validation starts after the submission passes screening.";
}

// ── Admission retry honesty (#1215) ─────────────────────────────────────────

/** `admission_retry` from /public/agent/{id}/pipeline (all optional wire). */
export interface AdmissionRetryState {
  state?: string | null;
  attempt_count?: number | null;
  next_retry_at?: string | null;
  last_failure_infrastructure?: boolean | null;
  lane?: string | null;
}

/**
 * One honest sentence for a submission still in build & admission.
 */
export function admissionRetryLine(
  retry: AdmissionRetryState | null | undefined,
  now: Date = new Date(),
): string {
  if (!retry) return "";
  const attempts = Number(retry.attempt_count) || 0;
  if (retry.state === "running") {
    return "Admission attempt " + Math.max(attempts, 1) + " is running now.";
  }
  if (retry.state === "queued") {
    return "Waiting for a screener slot; no attempt has started yet.";
  }
  if (retry.state === "retry_queued" && retry.next_retry_at) {
    const infra =
      "A Ditto build infrastructure failure, not a miner failure. It retries automatically with backoff";
    const due = new Date(retry.next_retry_at);
    if (Number.isNaN(due.getTime())) return infra + ".";
    if (due.getTime() <= now.getTime()) {
      return infra + " and is due for another attempt.";
    }
    // Fixed UTC rendering: never the viewer's locale or timezone.
    return (
      infra + ", no earlier than " + due.toISOString().slice(0, 16).replace("T", " ") + " UTC."
    );
  }
  if (retry.state === "retry_queued") {
    return (
      "A guarded retry was authorized and is waiting for a screener slot (attempt " +
      (attempts + 1) +
      ")."
    );
  }
  if (retry.state === "parked") {
    return "Parked after one provider or source-review failure. It will not retry automatically; an operator must authorize the next attempt.";
  }
  if (retry.state === "stuck") {
    return "Stuck after a Ditto infrastructure failure, not a miner failure. It will not retry automatically; an operator must authorize the next attempt.";
  }
  return "";
}

const ADMISSION_LANE_LABELS: Record<string, string> = {
  build: "image build",
  runtime_smoke: "runtime smoke test",
  source_review: "source review",
};

/**
 * The admission lane the latest attempt is in, or stopped in. Platform sends
 * a lane only when it can evidence one; anything else renders nothing.
 */
export function admissionLaneLine(retry: AdmissionRetryState | null | undefined): string {
  const lane = retry?.lane ? ADMISSION_LANE_LABELS[retry.lane] : undefined;
  if (!lane || retry?.state === "queued") return "";
  return retry?.state === "running"
    ? "Current lane: " + lane + "."
    : "Last attempt stopped in: " + lane + ".";
}

export function admissionRetryChip(
  retry: AdmissionRetryState | null | undefined,
): readonly [string, string] | null {
  if (retry?.state === "parked") return ["Parked", "paused"];
  if (retry?.state === "stuck") return ["Stuck", "warn"];
  if (retry?.state === "retry_queued") return ["Retry queued", "progress"];
  return null;
}
