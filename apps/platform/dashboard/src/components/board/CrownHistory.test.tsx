// The pinned-ledger history is the record that answers "was the crown
// stable?": each row is one immutable pin, so the table must state absence
// honestly and must never invent a champion from the live board.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { ResourceState } from "../../data/useEndpoint";
import { loadFixture } from "../../test-fixtures";
import type { LedgerEpochsPayload } from "../../types/leaderboard";
import { CrownHistory } from "./CrownHistory";

afterEach(() => cleanup());

function resource(
  data: LedgerEpochsPayload | null,
  error: unknown = null,
): ResourceState<LedgerEpochsPayload> {
  return {
    data: () => data ?? undefined,
    error: () => error,
    loading: () => false,
    refresh: () => undefined,
  } as unknown as ResourceState<LedgerEpochsPayload>;
}

const fixture = loadFixture<LedgerEpochsPayload>("ledger-epochs");
const text = (): string => document.getElementById("crown-history")?.textContent ?? "";

describe("CrownHistory", () => {
  it("lists every pin newest first and flags the epoch the crown moved", () => {
    render(() => <CrownHistory mode="page" resource={resource(fixture)} />);

    const rows = document.querySelectorAll("#crown-history tbody tr");
    expect(rows).toHaveLength(fixture.epochs?.length ?? 0);
    expect(rows[0]?.textContent).toContain("pin #24,281");
    expect(rows[0]?.textContent).toContain("held");
    // Row 24279 is where the incumbent handed the crown over.
    const moved = document.querySelectorAll("#crown-history tr.crown-changed");
    expect(moved).toHaveLength(1);
    expect(moved[0]?.textContent).toContain("pin #24,279");
    expect(moved[0]?.textContent).toContain("moved");
    expect(text()).toContain("crown moved 1 time in this window");
    // A pin taken without the incumbency marker says so instead of naming one.
    expect(rows[rows.length - 1]?.textContent).toContain("classic walk");
  });

  it("trims the overview mount and keeps the page mount complete", () => {
    const many: LedgerEpochsPayload = {
      ...fixture,
      count: 10,
      epochs: Array.from({ length: 10 }, (_, i) => ({
        ...(fixture.epochs as NonNullable<typeof fixture.epochs>)[0],
        epoch_index: 24_300 - i,
      })),
    };
    render(() => <CrownHistory mode="overview" resource={resource(many)} />);
    expect(document.querySelectorAll("#crown-history tbody tr")).toHaveLength(6);
  });

  it("states absence: no pins yet, live mode, and an unavailable history", () => {
    render(() => (
      <CrownHistory resource={resource({ mode: "epoch", count: 0, epochs: [] })} />
    ));
    expect(text()).toContain("No pin has been taken yet.");
    expect(document.querySelector("#crown-history table")).toBeNull();
    cleanup();

    render(() => (
      <CrownHistory resource={resource({ mode: "live", count: 0, epochs: [] })} />
    ));
    expect(text()).toContain("folding the live ledger");
    cleanup();

    render(() => <CrownHistory resource={resource(null, new Error("down"))} />);
    expect(text()).toContain("History unavailable");
  });
});
