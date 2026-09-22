import { describe, expect, it } from "vitest";

import type { AcceptedScore, InferenceRun } from "../../types/pipeline";
import { scoreForAcceptedResult, scoreForInferenceRun, type PublishedRunScore } from "./run-score";

const score: PublishedRunScore = {
  validator_hotkey: "validator-a",
  bench_version: 12,
  ticket_deadline: "2026-09-21T03:37:35.227552Z",
  transcript_sha256: "a".repeat(64),
  composite: 0.817,
  tool_mean: 0.9,
  memory_mean: 0.8,
};

describe("published score joins", () => {
  it("matches only the exact validator lease, not an earlier retry", () => {
    const run: InferenceRun = {
      validator_hotkey: score.validator_hotkey,
      bench_version: score.bench_version,
      ticket_deadline: score.ticket_deadline,
    };
    expect(scoreForInferenceRun(run, [score])).toBe(score);
    expect(
      scoreForInferenceRun({ ...run, ticket_deadline: "2026-09-21T02:37:35.227552Z" }, [score]),
    ).toBeNull();
    expect(scoreForInferenceRun({ ...run, bench_version: 13 }, [score])).toBeNull();
    expect(scoreForInferenceRun(run, [score, score])).toBeNull();
  });

  it("matches an accepted result by transcript digest, version, and composite", () => {
    const accepted: AcceptedScore = {
      transcript_sha256: score.transcript_sha256,
      bench_version: 12,
      composite: 0.817,
    };
    expect(scoreForAcceptedResult(accepted, [score])).toBe(score);
    expect(scoreForAcceptedResult({ ...accepted, transcript_sha256: null }, [score])).toBeNull();
    expect(scoreForAcceptedResult({ ...accepted, composite: 0.818 }, [score])).toBeNull();
    expect(scoreForAcceptedResult(accepted, [score, score])).toBeNull();
  });
});
