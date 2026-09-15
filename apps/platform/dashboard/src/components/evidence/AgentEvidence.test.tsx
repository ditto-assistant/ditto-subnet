// The continual-retest card, from a live miner report.
//
// Bench v12 rank 1 (`6d0aa2f5`, composite 0.818274) rendered "Current score:
// NaN" next to "Retest samples: 0" and "Retest mean: 0.439", and the miner read
// the 0.439 as their score having been dragged down by eight bad samples. Two
// separate defects produced that card:
//
//   1. `official_composite` is nullable on the board entry and the card ran it
//      through a bare `fx(Number(...))`, so an absent value printed "NaN"
//      instead of falling back to `composite` the way `displayComposite` and
//      every other composite reader already do.
//   2. The mean is taken over `visibleSamples()`, which falls back to PENDING
//      seeds when no wave has completed — while the count beside it reports
//      completed waves only. "0 samples" next to "Retest mean 0.439" reads as a
//      score, so the label has to track which set it actually summarised.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import { ConfirmationScores } from "./AgentEvidence";
import type { PipelineDetailPayload } from "./AgentEvidence";

afterEach(cleanup);

const AGENT_ID = "6d0aa2f5-492b-4510-9bcb-129ff4e1e353";

/** The eight degraded samples the miner reported, plus the seven sound ones. */
const REPORTED_SAMPLES = [
  0.143, 0.137, 0.141, 0.143, 0.294, 0.007, 0.014, 0.0, 0.793, 0.849, 0.815, 0.812, 0.81,
  0.813, 0.811,
];

function pipeline(samples: number[]): PipelineDetailPayload {
  return {
    agent_id: AGENT_ID,
    active_bench_version: 12,
    confirmation_scores: samples.map((composite, index) => ({
      bench_version: 12,
      seed: String(index + 1),
      composite,
    })),
    validation_attempts: [],
  } as unknown as PipelineDetailPayload;
}

function card(entry: Record<string, unknown>, samples = REPORTED_SAMPLES) {
  const entries = () => [{ agent_id: AGENT_ID, ...entry }] as never;
  return render(() => <ConfirmationScores pipeline={pipeline(samples)} entries={entries} />);
}

function statValue(container: HTMLElement, label: string): string | null {
  for (const stat of container.querySelectorAll(".retest-summary > div")) {
    if (stat.querySelector("span")?.textContent === label) {
      return stat.querySelector("strong")?.textContent ?? null;
    }
  }
  return null;
}

describe("ConfirmationScores current score", () => {
  it("falls back to composite when official_composite never landed", () => {
    // Exactly the reported row: finalized three-score quorum, no fold yet, so
    // the board carries `composite` and no `official_composite` at all.
    const { container } = card({ composite: 0.818274, completed_wave_count: 0 });

    expect(statValue(container, "Current score")).toBe("0.818");
  });

  it("prefers official_composite once continual aggregation sets one", () => {
    const { container } = card({
      composite: 0.818274,
      official_composite: 0.7391252,
      completed_wave_count: 0,
    });

    expect(statValue(container, "Current score")).toBe("0.739");
  });

  it("never prints NaN when the board carries no composite at all", () => {
    const { container } = card({ completed_wave_count: 0 });

    expect(statValue(container, "Current score")).not.toContain("NaN");
  });
});

describe("ConfirmationScores sample mean", () => {
  it("does not call a pending-seed mean a retest mean", () => {
    // completed_wave_count 0 and no `continual_mean` method: the card reports
    // "Retest samples: 0", so the mean beside it is over pending seeds and must
    // say so. 0.439 is the mean of all fifteen; the seven sound ones mean 0.815.
    const { container } = card({ composite: 0.818274, completed_wave_count: 0 });

    expect(statValue(container, "Retest samples")).toBe("0");
    expect(statValue(container, "Retest mean")).toBeNull();
    expect(statValue(container, "Pending mean")).toBe("0.439");
  });

  it("calls it a retest mean once waves have actually completed", () => {
    const { container } = card({
      composite: 0.818274,
      official_composite: 0.815,
      aggregate_method: "continual_mean",
      completed_wave_count: 15,
    });

    expect(statValue(container, "Retest samples")).toBe("15");
    expect(statValue(container, "Pending mean")).toBeNull();
    expect(statValue(container, "Retest mean")).toBe("0.439");
  });
});
