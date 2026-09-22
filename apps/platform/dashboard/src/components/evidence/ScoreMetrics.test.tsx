import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { V9BaseEvidence } from "../../types/leaderboard";
import { ScoreMetrics } from "./ScoreMetrics";

const gates: V9BaseEvidence = {
  bench_version: 12,
  score_gates: {
    rollout_mode: "enforce",
    model_use: {
      administered_cases: 351,
      eligible_cases: 349,
      successful_inference_cases: 349,
      missing_inference_cases: 0,
      observed_requests: 1011,
      successful_requests: 846,
      request_coverage_bps: 10000,
      coverage_bps: 10000,
      threshold_bps: 1,
      result: "passed",
      factor_bps: 10000,
    },
    authoritative_tool: {
      expected_executions: 136,
      matched_executions: 125,
      missing_executions: 11,
      unexpected_executions: 2,
      observed_executions: 127,
      coverage_bps: 9191,
      threshold_bps: 1,
      result: "passed",
      factor_bps: 10000,
    },
  },
};

afterEach(cleanup);

describe("score metric summary", () => {
  it("shows trusted gate results beside the exact composite", () => {
    const view = render(() => <ScoreMetrics composite={0.817} v9Base={gates} />);
    expect(view.getByText("2/2 pass")).toBeTruthy();
    expect(view.getByText("0.817")).toBeTruthy();
  });

  it("leaves missing tool, memory, and gate data unknown", () => {
    const view = render(() => <ScoreMetrics composite={0.817} />);
    expect(Array.from(view.container.querySelectorAll("dd"), (dd) => dd.textContent)).toEqual([
      "—",
      "—",
      "—",
      "0.817",
    ]);
  });
});
