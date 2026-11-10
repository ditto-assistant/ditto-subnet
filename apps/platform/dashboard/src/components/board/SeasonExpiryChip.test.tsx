// SN-466: a dueling season rotation expires a quorum by design, and the board
// keeps the row instead of deleting it. This chip is what tells a reader why a
// row is pinned-but-expiring or unranked, so it must stay silent on ordinary
// rows and never imply the row is being re-scored.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { SeasonExpiry } from "../../types/leaderboard";
import { SeasonExpiryChip } from "./chips";
import type { BoardEntry } from "./leaderboard-data";

afterEach(() => cleanup());

function entry(expiry: SeasonExpiry | null): BoardEntry {
  return {
    agent_id: "a",
    miner_hotkey: "5Miner",
    composite: 0.960714,
    season_expiry: expiry,
  } as unknown as BoardEntry;
}

function expiry(overrides: Partial<SeasonExpiry> = {}): SeasonExpiry {
  return {
    state: "pinned_carry",
    score_dueling_season_ids: ["87a46084-1664-4dbe-bc56-6fb8d3f57200"],
    window_dueling_season_ids: ["428a8bc5-b002-4af4-91e4-964cb83128f4"],
    in_window_score_count: 0,
    last_valid_composite: 0.960714,
    pinned_epoch_index: 26259,
    ...overrides,
  };
}

const chip = (): Element | null => document.querySelector(".season-expiry-chip");

describe("SeasonExpiryChip", () => {
  it("renders nothing for a row whose quorum is in the live window", () => {
    render(() => <SeasonExpiryChip entry={entry(null)} />);
    expect(chip()).toBeNull();
  });

  it("names a pinned carry and the pin that still holds it", () => {
    render(() => <SeasonExpiryChip entry={entry(expiry())} />);
    expect(chip()?.textContent).toBe("Season expired \u00b7 pinned");
    expect(chip()?.getAttribute("data-tooltip")).toContain("Epoch pin 26259");
  });

  it("labels an expired reference row without promising a re-score", () => {
    render(() => (
      <SeasonExpiryChip entry={entry(expiry({ state: "expired", pinned_epoch_index: null }))} />
    ));
    expect(chip()?.textContent).toBe("Season expired");
    const tip = chip()?.getAttribute("data-tooltip") ?? "";
    expect(tip).toContain("0.9607");
    expect(tip).not.toMatch(/re-?scor/i);
  });
});
