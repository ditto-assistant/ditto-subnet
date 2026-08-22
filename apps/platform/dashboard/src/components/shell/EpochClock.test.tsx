// The clock answers the subnet's most-asked question — "when do I get
// emissions?" — so the cases that matter are the ones where it could answer it
// wrongly: a stale snapshot, a chain read that failed, and the last minute.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChainEpoch } from "../../types/leaderboard";
import { EpochClock } from "./EpochClock";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** SN118's real cadence, re-anchored so the tick sits `secondsAway` out. */
function epochAt(secondsAway: number): ChainEpoch {
  return {
    tempo_blocks: 360,
    block_seconds: 12,
    epoch_seconds: 4320,
    last_epoch_block: 8_895_229,
    next_epoch_block: 8_895_589,
    blocks_since_last_epoch: 360 - Math.round(secondsAway / 12),
    blocks_until_next_epoch: Math.round(secondsAway / 12),
    next_epoch_at: new Date(Date.now() + secondsAway * 1000).toISOString(),
    commit_reveal_enabled: true,
    reveal_period_epochs: 1,
    weights_rate_limit_blocks: 100,
  };
}

const clockText = (): string =>
  document.querySelector("#epoch-clock .epoch-clock-time")?.textContent ?? "";

describe("EpochClock", () => {
  it("reads the countdown, the tick's block, and the tempo", () => {
    render(() => <EpochClock epoch={() => epochAt(1815)} />);

    expect(clockText()).toMatch(/^30:1[3-5]$/);
    const text = document.getElementById("epoch-clock")?.textContent ?? "";
    expect(text).toContain("block 8,895,589");
    expect(text).toContain("360-block epoch");
  });

  it("fills the gauge to the subnet's position in the epoch", () => {
    // Half an epoch gone, so half the gauge is lit. The gauge is the mechanic:
    // with every word stripped it still says a fixed cycle is this far along.
    render(() => <EpochClock epoch={() => epochAt(2160)} />);

    const ticks = document.querySelectorAll("#epoch-clock .epoch-clock-gauge i");
    expect(ticks.length).toBe(24);
    expect(document.querySelectorAll("#epoch-clock .epoch-clock-gauge i.on").length).toBe(12);
    // Exactly one head, so the live edge never reads as two.
    expect(document.querySelectorAll("#epoch-clock .epoch-clock-gauge i.head").length).toBe(1);
  });

  it("marks the last minute rather than counting it down quietly", () => {
    render(() => <EpochClock epoch={() => epochAt(45)} />);

    expect(document.getElementById("epoch-clock")).toHaveClass("imminent");
    expect(document.getElementById("epoch-clock")).toHaveAttribute(
      "aria-label",
      "Next payout in under a minute.",
    );
  });

  it("does not announce per second, and speaks the reading coarsely", () => {
    // A region that announced 38px digits every second would make the whole
    // dashboard unusable with a screen reader on.
    render(() => <EpochClock epoch={() => epochAt(1815)} />);

    const region = document.getElementById("epoch-clock");
    expect(region).toHaveAttribute("aria-live", "off");
    expect(region).toHaveAttribute("aria-label", "Next payout in about 30 minutes.");
    expect(document.querySelector("#epoch-clock .epoch-clock-gauge")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("rolls a spent target forward and flags the reading as projected", () => {
    // A cached snapshot can outlive the tick it named. Parking at 00:00 would
    // claim a payout is perpetually imminent.
    render(() => <EpochClock epoch={() => epochAt(-120)} />);

    expect(clockText()).not.toBe("0:00");
    expect(document.getElementById("epoch-clock")?.textContent).toContain("projected");
    expect(document.getElementById("epoch-clock")).toHaveClass("projected");
  });

  it("states an absent chain read in place instead of vanishing", () => {
    // The rail is chrome on every page: a widget that disappeared would shift
    // the nav under the pointer and read as "no payout", not "no reading".
    render(() => <EpochClock epoch={() => null} />);

    expect(document.getElementById("epoch-clock")).toBeTruthy();
    expect(clockText()).toBe("--:--");
    expect(document.getElementById("epoch-clock")?.textContent).toContain("Chain read unavailable");
    expect(document.querySelector("#epoch-clock .epoch-clock-gauge")).toBeNull();
  });

  it("ticks", () => {
    vi.useFakeTimers();
    const epoch = epochAt(1815);
    render(() => <EpochClock epoch={() => epoch} />);
    const first = clockText();

    vi.advanceTimersByTime(3000);

    expect(clockText()).not.toBe(first);
  });
});
