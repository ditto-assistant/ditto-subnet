import type { V9BaseEvidence } from "../../types/leaderboard";
import type { AcceptedScore, InferenceRun } from "../../types/pipeline";

/** Finalized public score rows. Provisional rows omit validator identity. */
export interface PublishedRunScore {
  validator_hotkey: string;
  bench_version: number | null;
  ticket_deadline: string | null;
  transcript_sha256: string | null;
  tool_mean: number | null;
  memory_mean: number | null;
  composite: number;
  v9_base?: V9BaseEvidence | null;
}

export interface PublishedScoresPayload {
  scores: PublishedRunScore[];
}

function deadline(value: string | null | undefined): string | null {
  // Keep subsecond precision; Date.parse loses the microseconds in lease IDs.
  return value ? value.replace(/\+00:00$/, "Z") : null;
}

/** A validator can have multiple retries, so a hotkey alone is not a run key. */
export function scoreForInferenceRun(
  run: InferenceRun,
  scores: PublishedRunScore[],
): PublishedRunScore | null {
  const runDeadline = deadline(run.ticket_deadline);
  if (!run.validator_hotkey || run.bench_version == null || !runDeadline) return null;
  const matches = scores.filter(
    (score) =>
      score.validator_hotkey === run.validator_hotkey &&
      score.bench_version === run.bench_version &&
      deadline(score.ticket_deadline) === runDeadline,
  );
  return matches.length === 1 ? (matches[0] ?? null) : null;
}

/** Pipeline accepted rows expose a digest, but not the validator hotkey. */
export function scoreForAcceptedResult(
  accepted: AcceptedScore,
  scores: PublishedRunScore[],
): PublishedRunScore | null {
  if (!accepted.transcript_sha256 || accepted.bench_version == null) return null;
  const matches = scores.filter(
    (score) =>
      score.transcript_sha256 === accepted.transcript_sha256 &&
      score.bench_version === accepted.bench_version &&
      score.composite === accepted.composite,
  );
  return matches.length === 1 ? (matches[0] ?? null) : null;
}
