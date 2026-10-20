// The board's job under #2041 is to publish three separate things: the score's
// rank, the provisional crown, and whether the row is earning. This chip is the
// third one, so what it must NOT do matters as much as what it renders: it must
// stay silent on an earning row, and it must not claim a submission has stopped
// earning while the operator gate is only rehearsing.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { RewardEligibility } from "../../types/leaderboard";
import { RewardEligibilityChip } from "./chips";
import type { BoardEntry } from "./leaderboard-data";

afterEach(() => cleanup());

function entry(eligibility: RewardEligibility | null): BoardEntry {
  return {
    agent_id: "a",
    miner_hotkey: "5Miner",
    composite: 0.95,
    reward_eligibility: eligibility,
  } as unknown as BoardEntry;
}

function eligibility(overrides: Partial<RewardEligibility> = {}): RewardEligibility {
  return {
    state: "unresolved_review",
    reason: "Source review for this exact artifact is still open.",
    reward_eligible: false,
    posture_satisfied: false,
    enforcement: "enforce",
    policy_revision: 3,
    window_start: "2026-09-23T14:00:00Z",
    activates_at: null,
    ...overrides,
  };
}

const text = (): string => document.body.textContent ?? "";
const chip = (): Element | null => document.querySelector(".reward-eligibility-chip");

describe("RewardEligibilityChip", () => {
  it("renders nothing for a row with no eligibility annotation", () => {
    render(() => <RewardEligibilityChip entry={entry(null)} />);
    expect(chip()).toBeNull();
  });

  it("renders nothing for a row that is earning", () => {
    render(() => (
      <RewardEligibilityChip
        entry={entry(
          eligibility({ state: "eligible", reward_eligible: true, posture_satisfied: true }),
        )}
      />
    ));
    // A chip on every row would say nothing, and would read as an accusation
    // on the rows it did appear on.
    expect(chip()).toBeNull();
  });

  it("states that an enforced withheld row is not earning, without accusing", () => {
    render(() => <RewardEligibilityChip entry={entry(eligibility())} />);
    expect(text()).toContain("Not earning");
    expect(text()).toContain("review open");
    expect(chip()?.className).toContain("settled");
    expect(chip()?.className).not.toContain("warn");
  });

  it("says a shadow finding WOULD not earn, because the row is still paid", () => {
    render(() => <RewardEligibilityChip entry={entry(eligibility({ enforcement: "shadow" }))} />);
    expect(text()).toContain("Would not earn");
    expect(text()).not.toContain("Not earning");
  });

  it("names a Ditto-side infrastructure failure as ours", () => {
    render(() => (
      <RewardEligibilityChip
        entry={entry(
          eligibility({
            state: "review_infrastructure_failed",
            reason: "Ditto's own build or review infrastructure failed.",
          }),
        )}
      />
    ));
    expect(text()).toContain("Ditto fault");
  });

  it("tells a cleared submission it starts earning next window", () => {
    render(() => (
      <RewardEligibilityChip
        entry={entry(
          eligibility({
            state: "awaiting_next_window",
            reason: "Emissions start at the next emission window.",
            activates_at: "2026-09-23T15:00:00Z",
          }),
        )}
      />
    ));
    expect(text()).toContain("earns next window");
  });

  it("uses the warn tone only for an adjudicated rejection", () => {
    render(() => (
      <RewardEligibilityChip
        entry={entry(
          eligibility({
            state: "review_rejected",
            reason: "Source review for this exact artifact ended in a rejection.",
          }),
        )}
      />
    ));
    expect(text()).toContain("rejected");
    expect(chip()?.className).toContain("warn");
  });
});
