import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { createSignal } from "solid-js";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { spaQuery } from "../../lib/router";
import { rankEntries } from "../../lib/scoring";
import { syncFromLocation } from "../../stores/routeStore";
import { loadFixture } from "../../test-fixtures";
import type { LeaderboardEntry, LeaderboardPayload } from "../../types/leaderboard";
import { BoardTable } from "./BoardTable";
import { boardPage, boardPageSize, resetBoardState, restoreBoardPage } from "./board-state";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

const fixture = loadFixture<LeaderboardPayload>("leaderboard");

function boardOf(count: number): LeaderboardPayload {
  const template = (fixture.entries ?? [])[0] as LeaderboardEntry;
  const entries: LeaderboardEntry[] = [];
  for (let i = 0; i < count; i += 1) {
    entries.push({
      ...template,
      agent_id: "pager-" + i,
      agent_name: "pager-miner-" + i,
      miner_hotkey: "5Pgr" + String(i).padStart(44, "0"),
      miner_uid: 700 + i,
      submission_family: undefined,
      official_composite: 0.9 - i * 0.001,
    } as LeaderboardEntry);
  }
  return { ...fixture, entries };
}

/** A store that starts loading (no payload), like a cold deep link. */
function loadingStore() {
  const [payload, setPayload] = createSignal<LeaderboardPayload | null>(null);
  const [unavailable, setUnavailable] = createSignal(false);
  const entries = (): BoardEntry[] =>
    rankEntries(payload()?.entries ?? []) as unknown as BoardEntry[];
  const store = {
    payload,
    unavailable,
    entries,
    settledView: () => false,
    emissions: () => payload()?.emissions ?? null,
    efficiency: () => null,
    champion: () => null,
    emissionFor: () => null,
    chainWeights: () => null,
    chainFold: () => null,
    rollout: () => null,
    bench: () => ({ active: null, desired: null, current: null }),
    refreshAll: () => {},
    ensureFresh: () => {},
  } as unknown as LeaderboardStore;
  return { store, setPayload, setUnavailable };
}

beforeEach(() => {
  resetBoardState();
});

afterEach(() => {
  cleanup();
  history.replaceState(null, "", "/leaderboard");
  syncFromLocation();
});

function deepLink(page: number): void {
  history.replaceState(null, "", "/leaderboard?page=" + page);
  syncFromLocation();
  restoreBoardPage();
}

describe("board pager deep link", () => {
  it("keeps ?page=N while the leaderboard is still loading or a poll failed", async () => {
    deepLink(2);
    const { store, setPayload, setUnavailable } = loadingStore();
    render(() => <BoardTable store={store} />);

    // No rows yet: the page must not be clamped to 1 or dropped from the URL.
    expect(boardPage()).toBe(2);
    expect(spaQuery().get("page")).toBe("2");

    setPayload(boardOf(boardPageSize + 5));
    await waitFor(() => expect(document.querySelectorAll("tr[data-i]").length).toBe(5));
    expect(boardPage()).toBe(2);
    expect(spaQuery().get("page")).toBe("2");

    // A failed poll must not throw the reader back to page 1 either.
    setUnavailable(true);
    setPayload(null);
    expect(boardPage()).toBe(2);
  });

  it("still clamps a page past the end once rows have loaded", async () => {
    deepLink(3);
    const { store, setPayload } = loadingStore();
    render(() => <BoardTable store={store} />);
    expect(boardPage()).toBe(3);

    setPayload(boardOf(boardPageSize + 5));
    await waitFor(() => expect(boardPage()).toBe(2));
  });
});
