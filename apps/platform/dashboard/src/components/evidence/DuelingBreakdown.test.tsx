import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { DuelingBreakdown, DuelingColumn } from "../../types/leaderboard";
import { DuelingBreakdownPanel, columnSource } from "./DuelingBreakdown";

afterEach(cleanup);

const column = (id: string, extra: Partial<DuelingColumn> = {}): DuelingColumn => ({
  column_id: id,
  grading_bench_version: 16,
  n: 35,
  sum: 28,
  mean: 0.8,
  substituted: false,
  public_default: false,
  self_masked: false,
  contribution: 0.08,
  ...extra,
});

// The projection the Platform tests pin
// (apps/platform/ditto/tests/api_server/test_dueling_breakdown.py).
const breakdown: DuelingBreakdown = {
  formula: "gate x ((1 - w x |U|) x datagen + w x sum col)",
  season_id: "8f2b6a52-3b7f-4a55-9d0c-0d6c7d2a9e11",
  season_index: 3,
  datagen_raw: 0.9,
  datagen_gate: 0.79832,
  column_weight: 0.1,
  counted_columns: 4,
  datagen_share: 0.6,
  generator_sum: 0.23,
  pre_gate_composite: 0.77,
  composite: 0.6147064,
  composite_stderr: 0.0125,
  priced_cost_applied_factor_bps: 9392,
  priced_cost_factor_bps: 9392,
  priced_cost_factor: 0.9392,
  priced_cost_posture: "enforce",
  priced_cost_result: "priced",
  priced_cost_window: "whole_run",
  other_gate_factor: 0.85,
  columns: [
    column("c0", { public_default: true }),
    column("c1", { self_masked: true, contribution: 0, sum: 35, mean: 1 }),
    column("c2", { grading_bench_version: 15, sum: 21, mean: 0.6, contribution: 0.06 }),
    column("c3", { substituted: true, sum: 17.5, mean: 0.5, contribution: 0.05 }),
    column("c4", { sum: 14, mean: 0.4, contribution: 0.04 }),
  ],
};

describe("DuelingBreakdownPanel", () => {
  it("lays out the formula, the priced-cost factor and every column", () => {
    const { container } = render(() => <DuelingBreakdownPanel breakdown={breakdown} />);
    const panel = container.querySelector("[data-dueling-breakdown]");
    expect(panel?.textContent).toContain("gate x ((1 - w x |U|) x datagen + w x sum col)");
    expect(container.querySelector('[data-dueling-metric="priced_cost_factor"]')?.textContent).toBe(
      "0.9392",
    );
    expect(panel?.textContent).toContain("(whole_run)");
    expect(container.querySelector('[data-dueling-metric="composite"]')?.textContent).toBe(
      "0.6147",
    );
    const rows = container.querySelectorAll("[data-dueling-column]");
    expect(rows).toHaveLength(5);
    const masked = container.querySelector('[data-dueling-column="c1"]');
    expect(masked?.getAttribute("data-self-masked")).toBe("true");
    expect(masked?.textContent).toContain("masked (own)");
    expect(container.querySelector('[data-dueling-column="c2"]')?.textContent).toContain("v15");
  });

  it("renders nothing below v16", () => {
    const { container } = render(() => <DuelingBreakdownPanel breakdown={undefined} />);
    expect(container.querySelector("[data-dueling-breakdown]")).toBeNull();
  });

  it("names a shadow priced-cost curve that was not applied", () => {
    const { container } = render(() => (
      <DuelingBreakdownPanel breakdown={{ ...breakdown, priced_cost_applied_factor_bps: 10_000 }} />
    ));
    expect(container.textContent).toContain("curve 0.9392 not applied");
  });

  it("labels column sources", () => {
    expect(columnSource(column("c0", { public_default: true }))).toBe("public default");
    expect(columnSource(column("c3", { substituted: true }))).toBe("public default (substituted)");
    expect(columnSource(column("c2"))).toBe("miner generator");
  });
});
