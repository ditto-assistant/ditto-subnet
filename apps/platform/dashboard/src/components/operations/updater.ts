import type { FleetEntry, ValidatorUpdaterStatus } from "../../types/fleet";

export interface UpdaterView {
  label: string;
  tone: "progress" | "warn" | "bad" | "unknown";
  summary: string;
  target?: string;
  targetTitle?: string;
  title: string;
}

function plural(count: number, singular: string): string {
  return count === 1 ? singular : singular + "s";
}

function activeRunCount(entry: FleetEntry): number {
  return (entry.active_benchmarks || []).length + (entry.confirmation_benchmarks || []).length;
}

function candidateTarget(updater: ValidatorUpdaterStatus): {
  label?: string;
  title?: string;
} {
  if (updater.candidate_version) {
    return {
      label: "Target v" + updater.candidate_version,
      title: updater.candidate_descriptor || undefined,
    };
  }
  const digest = updater.candidate_descriptor?.match(/@sha256:([0-9a-f]{64})$/)?.[1];
  if (digest) {
    return {
      label: "Target " + digest.slice(0, 8) + "…",
      title: updater.candidate_descriptor || undefined,
    };
  }
  return {};
}

function failureCount(updater: ValidatorUpdaterStatus): string {
  const count = updater.failed_candidate_count || 0;
  return count + " failed " + plural(count, "attempt");
}

/** Compact release ownership rendered below the software version. */
export function updaterModeLine(entry: FleetEntry): string | null {
  const updater = entry.updater_status;
  if (!updater) {
    return entry.stack?.mode === "managed" ? "Updater not reported" : null;
  }
  if (updater.state === "not_managed") return "Source-managed";
  if (updater.state === "disabled") return "Managed · updates off";
  if (updater.self_refresh_installed === false) {
    return "Managed · updater refresh missing";
  }
  return "Managed · " + (updater.channel || "channel unknown");
}

/** Translate the signed updater state into operator copy. Idle and source
 * builds stay compact in the version cell; active, blocked, and unreadable
 * states earn an inline notice beside the work they affect. */
export function updaterView(entry: FleetEntry): UpdaterView | null {
  const updater = entry.updater_status;
  if (!updater) {
    if (entry.stack?.mode !== "managed") return null;
    return {
      label: "Updater not reported",
      tone: "unknown",
      summary: "This managed validator has not reported updater telemetry.",
      title: "Managed updater telemetry requires heartbeat protocol 23 or newer.",
    };
  }
  if (updater.state === "not_managed" || updater.state === "idle") return null;

  const target = candidateTarget(updater);
  const base = { target: target.label, targetTitle: target.title };
  switch (updater.state) {
    case "disabled":
      return {
        ...base,
        label: "Updates disabled",
        tone: "warn",
        summary: "This managed validator is not polling the release channel.",
        title: "The complete-stack updater is installed but disabled on this host.",
      };
    case "unavailable":
      return {
        ...base,
        label: "Updater unavailable",
        tone: "warn",
        summary: "The validator could not read a trustworthy updater state.",
        title: "Updater state was missing, malformed, or unreadable on the validator host.",
      };
    case "prefetched":
      return {
        ...base,
        label: "Update staged",
        tone: "progress",
        summary: "The signed release is authenticated and ready for a safe drain.",
        title: "Component images are warm; the current stack has not been interrupted.",
      };
    case "draining": {
      const runs = activeRunCount(entry);
      const drained = updater.transaction_phase === "drained";
      return {
        ...base,
        label: "Safe drain",
        tone: "progress",
        summary: drained
          ? "All work is quiescent; the staged release can now replace the stack."
          : runs
            ? `Finishing ${runs} active ${plural(runs, "run")} before restart · no new work.`
            : "Waiting for all validator work to become quiescent · no new work.",
        title:
          "The updater does not stop the current stack until benchmark, confirmation, and weight work is quiescent. If draining times out, it resumes the old stack.",
      };
    }
    case "replacing":
      return {
        ...base,
        label: "Replacing stack",
        tone: "progress",
        summary: "The old stack is stopped and the staged release is starting.",
        title: "The managed replacement transaction is in progress.",
      };
    case "verifying":
      return {
        ...base,
        label: "Verifying update",
        tone: "progress",
        summary: "The candidate is running while readiness and Platform acceptance are checked.",
        title: "The updater commits only after the complete candidate stack is healthy.",
      };
    case "rollback":
      return {
        ...base,
        label: "Rolling back",
        tone: "bad",
        summary: "The candidate did not pass; the previous complete stack is being restored.",
        title: "The updater failed closed and is restoring the previously authenticated stack.",
      };
    case "backoff":
      return {
        ...base,
        label: "Update retry delayed",
        tone: "warn",
        summary: failureCount(updater) + "; the candidate will retry after its backoff window.",
        title: updater.last_failure_reason
          ? "Last failure: " + updater.last_failure_reason
          : "The candidate is waiting for its bounded retry window.",
      };
    case "retry_ready":
      return {
        ...base,
        label: "Update retry ready",
        tone: "warn",
        summary: failureCount(updater) + "; the next updater poll may retry this candidate.",
        title: updater.last_failure_reason
          ? "Last failure: " + updater.last_failure_reason
          : "The bounded retry window is open.",
      };
    case "suppressed":
      return {
        ...base,
        label: "Update blocked",
        tone: "bad",
        summary: failureCount(updater) + "; automatic retries are suppressed.",
        title: updater.last_failure_reason
          ? "Last failure: " + updater.last_failure_reason
          : "This candidate requires operator attention.",
      };
  }
}
