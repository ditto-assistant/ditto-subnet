// Per-benchmark-version cohort grouping for the agent drawer (monolith
// benchmarkCohorts 7094–7131, pipelineCurrentCohort 7133–7135,
// pipelineDisplayState 7137–7147, cohortProgressSummary 7172–7195). Scores
// compare only within one benchmark version, so every drawer section groups
// by cohort with the working / incomplete-upgrade cohort focused first.
import { benchmarkVersionKey, retestAttemptCounts } from "../pipeline/status";
import type { ActivityStatusEntry } from "../pipeline/status";
import type { AcceptedScore, PipelinePayload, ValidationAttempt } from "../../types/pipeline";

export interface BenchmarkCohort {
  key: string;
  scores: AcceptedScore[];
  attempts: ValidationAttempt[];
}

export function benchmarkCohorts(pipeline: PipelinePayload): BenchmarkCohort[] {
  const cohorts: Record<string, BenchmarkCohort> = {};
  function cohort(version: unknown): BenchmarkCohort {
    const key = benchmarkVersionKey(version);
    let existing = cohorts[key];
    if (!existing) {
      existing = { key, scores: [], attempts: [] };
      cohorts[key] = existing;
    }
    return existing;
  }
  (pipeline.provisional_scores || []).forEach((score) => {
    cohort(score.bench_version).scores.push(score);
  });
  (pipeline.validation_attempts || []).forEach((attempt) => {
    cohort(attempt.bench_version).attempts.push(attempt);
  });
  const activeKey = benchmarkVersionKey(pipeline.active_bench_version);
  let focusKey = activeKey;
  const keys = Object.keys(cohorts);
  const working = keys.filter((key) =>
    (cohorts[key] as BenchmarkCohort).attempts.some(
      (attempt) => attempt.actively_running || attempt.status === "issued",
    ),
  );
  const incompleteUpgrade = keys.filter((key) => {
    const entry = cohorts[key] as BenchmarkCohort;
    return (
      key !== "unknown" &&
      key !== activeKey &&
      entry.scores.length > 0 &&
      entry.scores.length < Math.max(1, Number(pipeline.quorum) || 3)
    );
  });
  if (working.length) focusKey = working.sort((a, b) => Number(b) - Number(a))[0] as string;
  else if (incompleteUpgrade.length)
    focusKey = incompleteUpgrade.sort((a, b) => Number(b) - Number(a))[0] as string;
  return Object.keys(cohorts)
    .map((key) => cohorts[key] as BenchmarkCohort)
    .sort((a, b) => {
      if (a.key === focusKey) return -1;
      if (b.key === focusKey) return 1;
      if (a.key === activeKey) return -1;
      if (b.key === activeKey) return 1;
      if (a.key === "unknown") return 1;
      if (b.key === "unknown") return -1;
      return Number(a.key) - Number(b.key);
    });
}

export function pipelineCurrentCohort(pipeline: PipelinePayload): BenchmarkCohort | null {
  return benchmarkCohorts(pipeline)[0] || null;
}

/**
 * The drawer's headline state (pipelineDisplayState 7137–7147): when the
 * focused cohort is NOT the active version (an upgrade in flight or an old
 * generation still draining), the current-progress copy describes THAT
 * cohort's version, count, and stage instead of the wire status.
 */
export function pipelineDisplayState(
  entry: ActivityStatusEntry,
  pipeline: PipelinePayload | null,
): ActivityStatusEntry {
  const current = { ...entry, ...pipeline } as ActivityStatusEntry;
  const cohort = pipelineCurrentCohort(current as PipelinePayload);
  if (!cohort || cohort.key === benchmarkVersionKey(current.active_bench_version)) return current;
  const running = cohort.attempts.some((attempt) => attempt.actively_running);
  const issued = cohort.attempts.some((attempt) => attempt.status === "issued");
  current.active_bench_version = Number(cohort.key);
  current.score_count = cohort.scores.length;
  current.status = running ? "evaluating" : issued ? "waiting_validator" : current.status;
  return current;
}

/** "N of Q quorum inputs · …" (cohortProgressSummary 7172–7195). */
export function cohortProgressSummary(cohort: BenchmarkCohort, quorum: unknown): string {
  const q = Math.max(1, Number(quorum) || 3);
  const retests = retestAttemptCounts(cohort.attempts);
  let summary = Math.min(cohort.scores.length, q) + " of " + q + " quorum inputs";
  const canonicalRunning = cohort.attempts.filter(
    (attempt) => attempt.purpose === "canonical_quorum" && attempt.actively_running,
  ).length;
  const canonicalAssigned = cohort.attempts.filter(
    (attempt) =>
      attempt.purpose === "canonical_quorum" &&
      attempt.status === "issued" &&
      !attempt.actively_running,
  ).length;
  const transitioning = cohort.attempts.filter(
    (attempt) => attempt.purpose === "legacy_unclassified" && attempt.status === "issued",
  ).length;
  function retestState(count: number, state: string): string {
    return count + " " + (count === 1 ? "retest" : "retests") + " " + state;
  }
  if (canonicalRunning) summary += " · " + canonicalRunning + " scoring";
  if (canonicalAssigned) summary += " · " + canonicalAssigned + " assigned";
  if (transitioning) summary += " · " + transitioning + " legacy lease draining";
  if (retests.running) summary += " · " + retestState(retests.running, "running");
  if (retests.assigned) summary += " · " + retestState(retests.assigned, "assigned");
  if (retests.expired) summary += " · " + retestState(retests.expired, "expired");
  return summary;
}
