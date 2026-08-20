// Attempt-level vocabulary for the agent evidence drawer (monolith
// attemptLabel 6998–7006, validatorFailureLabel 7008–7015,
// screeningAttemptLabel 7017–7028, screeningReviewCategoryLabel 7030–7034,
// VALIDATOR_RETRY_EXPLANATION 7070, validatorRetryInfo 7080–7083) plus the
// pure half of renderValidationAttempt (7628–7691) as a testable view.
import { shortKey } from "../../lib/format";
import type { ChipState } from "../ui/StatusChip";
import type { ScreeningAttempt, ValidationAttempt } from "../../types/pipeline";

export const VALIDATOR_RETRY_EXPLANATION =
  "Validator took too long to post a score. Another validator will score you soon.";

export function attemptLabel(status: string | null | undefined, role: string): ChipState {
  const labels: Record<string, ChipState> = {
    running: ["Running", "progress"],
    passed: ["Passed", "good"],
    rejected: ["Rejected", "bad"],
    failed: ["Could not complete", "warn"],
    expired: [role === "validator" ? "Assignment expired" : "Expired", "warn"],
    issued: ["Score pending", "progress"],
    scored: ["Score submitted", "good"],
  };
  return (status != null && labels[status]) || [String(status || "Unknown").replace(/_/g, " "), ""];
}

export function validatorFailureLabel(
  reason: string | null | undefined,
  code?: string | null,
): string {
  if (code === "inference_allowance_exhausted") return "Inference allowance exhausted";
  if (code === "inference_request_rejected") return "Inference request rejected";
  const labels: Record<string, string> = {
    sandbox_oom: "Sandbox out of memory",
    infrastructure: "Validator infrastructure failure",
    scoring_error: "Scoring run failed",
  };
  return (reason != null && labels[reason]) || "";
}

function isAgentTerminalInferenceCode(code?: string | null): boolean {
  return code === "inference_allowance_exhausted" || code === "inference_request_rejected";
}

/** Screening attempt chip; a resolved quarantine reads by its resolution
 * (7017–7028). Unresolved quarantine is ["Quarantined", "warn"]. */
export function screeningAttemptLabel(attempt: ScreeningAttempt): ChipState {
  const resolutions: Record<string, ChipState> = {
    release: ["Released from quarantine", "good"],
    rescreen: ["Sent for rescreening", "progress"],
    reject: ["Rejected after quarantine", "bad"],
  };
  if (attempt.status === "quarantined" && attempt.quarantine_resolution) {
    const resolved = resolutions[attempt.quarantine_resolution];
    if (resolved) return resolved;
  }
  if (attempt.status === "quarantined") return ["Quarantined", "warn"];
  return attemptLabel(attempt.status, "screener");
}

/** "policy_finding-x" → "Policy Finding X" (7030–7034). */
export function screeningReviewCategoryLabel(value: unknown): string {
  return String(value || "policy finding")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export interface ValidationAttemptView {
  /** Headline chip: [label, tone]; "Scoring now" while actively running. */
  headline: string;
  tone: string;
  /** Tooltip copy on the expired-canonical retry marker, or null. */
  retryTip: string | null;
  /** "Continual retest · " / "Canonical quorum · " / legacy labels. */
  purposeLabel: string;
  /** "Validator {shortKey}" — rendered as the entity anchor. */
  validatorLabel: string;
  /** Everything after the validator anchor (verb, attempt count, failure
   * notes, deadlines). */
  metaRest: string;
  /** "Bench v{n}" / "Bench unknown". */
  benchLabel: string;
  /** The timestamp the row is dated by (the lease it describes). */
  when: string | null;
}

/**
 * Port of renderValidationAttempt's derivations (7628–7691). A ticket row is
 * a lease SLOT, rewritten in place on every reissue: issued_at resets and
 * attempt_count bumps, while failure_reason / failed_at are deliberately
 * preserved as an audit trail. So a recorded failure does not necessarily
 * describe the lease shown here. It is history whenever the row has since
 * gone terminal-scored (the platform only accepts a fail_job against an
 * *issued* lease, so a scored row's failure must predate its last reissue)
 * or whenever failed_at is older than issued_at. Treating that as the
 * current state painted every successful quorum input "Scoring run failed ·
 * deferred", stamped with the superseded failure's timestamp — so a fully
 * scored submission looked like three failures and its accepted scores
 * looked unexplained (#459).
 */
export function validationAttemptView(a: ValidationAttempt): ValidationAttemptView {
  let label = attemptLabel(a.actively_running ? "running" : a.status, "validator");
  if (a.purpose === "continual_retest" && a.status === "expired") {
    label = ["Retest expired", "warn"];
  }
  const failureLabel = validatorFailureLabel(a.failure_reason, a.failure_code);
  const priorFailure =
    !!failureLabel &&
    (a.status === "scored" ||
      Boolean(a.failed_at && a.issued_at && new Date(a.failed_at) < new Date(a.issued_at)));
  const currentFailure = !!failureLabel && !priorFailure;
  if (!a.actively_running && currentFailure) {
    label = isAgentTerminalInferenceCode(a.failure_code)
      ? [failureLabel, "bad"]
      : [failureLabel + " · deferred", "warn"];
  }
  const continual = a.purpose === "continual_retest";
  const canonical = a.purpose === "canonical_quorum";
  const purposeLabel = continual
    ? "Continual retest · "
    : canonical
      ? "Canonical quorum · "
      : a.status === "issued"
        ? "Legacy lease draining · "
        : "Legacy lease unclassified · ";
  const retryTip =
    a.failure_code === "inference_allowance_exhausted" && currentFailure
      ? "The agent exhausted its request or token allowance, or sent one request larger than the run token allowance. It is not validator infrastructure and does not receive an automatic infrastructure retry."
      : a.failure_code === "inference_request_rejected" && currentFailure
        ? "The platform refused the harness's inference request before reserving capacity (schema, size, or an unsupported field). It is not a spent grant and does not receive an automatic infrastructure retry."
        : canonical && (a.actively_running ? "running" : a.status) === "expired"
          ? VALIDATOR_RETRY_EXPLANATION
          : null;
  let meta = "";
  if (a.actively_running) meta += " is running the benchmark";
  else if (a.status === "issued") meta += " has this assignment";
  else if (a.status === "expired") meta += " did not submit a score";
  else meta += " submitted a score";
  const attemptCount = Number((a as { attempt_count?: number | null }).attempt_count) || 1;
  if (attemptCount > 1) meta += " on attempt " + attemptCount;
  if (currentFailure && a.failure_code === "inference_allowance_exhausted") {
    meta += " · exceeded the run inference allowance";
  } else if (currentFailure && a.failure_code === "inference_request_rejected") {
    meta += " · request refused before reservation";
  } else if (currentFailure) meta += " · reported " + failureLabel.toLowerCase();
  else if (priorFailure) meta += " · an earlier attempt reported " + failureLabel.toLowerCase();
  if (a.deadline && (a.actively_running || a.status === "issued")) {
    meta += " · score due " + new Date(a.deadline).toLocaleString();
  } else if (a.deadline && a.status === "expired") {
    meta += " before " + new Date(a.deadline).toLocaleString();
  }
  // Date the row by the lease it describes. Falling back to failed_at for a
  // superseded failure stamped a freshly reissued (or already scored) lease
  // with an hours-old time, which read as the row being stale.
  const when = currentFailure && a.failed_at ? a.failed_at : (a.issued_at ?? null);
  return {
    headline: a.actively_running ? "Scoring now" : (label[0] as string),
    tone: label[1] as string,
    retryTip,
    purposeLabel,
    validatorLabel: "Validator " + shortKey(a.validator_hotkey),
    metaRest: meta,
    benchLabel: a.bench_version == null ? "Bench unknown" : "Bench v" + a.bench_version,
    when,
  };
}
