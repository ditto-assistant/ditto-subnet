// Parity tests for the global search (assert-inventory row 32,
// test_includes_accessible_global_search): a combobox/listbox pairing over
// the miners + submissions corpus with keyboard shortcuts ("/", Cmd/Ctrl+K,
// arrows, Escape) that navigates to the owning page via pushState.
import { cleanup, fireEvent, render } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { rankEntries } from "../../lib/scoring";
import { syncFromLocation } from "../../stores/routeStore";
import { loadFixture } from "../../test-fixtures";
import type { LeaderboardEntry, LeaderboardPayload } from "../../types/leaderboard";
import type { OperationsPayload } from "../../types/fleet";
import type { PipelineEntry } from "../../types/pipeline";
import { GlobalSearch } from "./GlobalSearch";

const leaderboard = loadFixture<LeaderboardPayload>("leaderboard");
const operations = loadFixture<OperationsPayload>("operations");
const miners = rankEntries(leaderboard.entries ?? []);
const submissions: PipelineEntry[] = operations.activity?.entries ?? [];
const topMiner = miners[0] as LeaderboardEntry & { rank: number | null };

beforeEach(() => {
  history.replaceState(null, "", "/#/overview");
  syncFromLocation();
});

afterEach(cleanup);

function renderSearch(): void {
  render(() => <GlobalSearch miners={() => miners} submissions={() => submissions} />);
}

function input(): HTMLInputElement {
  const el = document.getElementById("search-input") as HTMLInputElement | null;
  if (!el) throw new Error("missing search input");
  return el;
}

function type(query: string): void {
  const el = input();
  el.focus();
  fireEvent.focus(el);
  el.value = query;
  fireEvent.input(el);
}

function options(): HTMLElement[] {
  return Array.from(document.querySelectorAll('#search-results [role="option"]'));
}

describe("GlobalSearch accessibility (row 32)", () => {
  it("is a combobox controlling a listbox", () => {
    renderSearch();
    const el = input();
    expect(el).toHaveAttribute("type", "search");
    expect(el).toHaveAttribute("role", "combobox");
    expect(el).toHaveAttribute("aria-autocomplete", "list");
    expect(el).toHaveAttribute("aria-controls", "search-results");
    expect(el).toHaveAttribute("aria-expanded", "false");
    const results = document.getElementById("search-results");
    expect(results).toHaveAttribute("role", "listbox");
    expect(results).toHaveAttribute("aria-label", "Search results");
    expect(document.getElementById("search-meta")).toHaveAttribute("aria-live", "polite");
  });

  it("renders unselected options with a kind badge for each match", () => {
    renderSearch();
    type(topMiner.miner_hotkey.slice(0, 6).toLowerCase());
    const opts = options();
    expect(opts.length).toBeGreaterThan(0);
    opts.forEach((opt) => expect(opt).toHaveAttribute("aria-selected", "false"));
    expect(opts[0]?.querySelector(".search-result-kind")?.textContent).toBe("Miner");
    expect(input()).toHaveAttribute("aria-expanded", "true");
  });

  it("caps results at 8 and counts them in the live meta line", () => {
    renderSearch();
    // Every fixture entry matches the shared hotkey prefix "5".
    type("5");
    expect(options().length).toBeLessThanOrEqual(8);
    expect(document.getElementById("search-meta")?.textContent).toMatch(/results · Enter to open/);
  });

  it("moves the active option with the arrow keys and wraps", () => {
    renderSearch();
    type(topMiner.miner_hotkey.slice(0, 10));
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(options()[0]).toHaveAttribute("aria-selected", "true");
    expect(input()).toHaveAttribute("aria-activedescendant", "search-result-0");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    const last = options().length - 1;
    expect(options()[last]).toHaveAttribute("aria-selected", "true");
    expect(input()).toHaveAttribute("aria-activedescendant", "search-result-" + last);
  });

  it("opens a miner match on the overview with its entity overlay", () => {
    renderSearch();
    type(topMiner.miner_hotkey);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(location.hash).toBe("#/overview?miner=" + topMiner.miner_hotkey);
  });

  it("opens a submission match on the submissions page", () => {
    renderSearch();
    const submission = submissions.find((entry) => entry.name) as PipelineEntry;
    type(String(submission.name));
    const first = options()[0];
    expect(first?.querySelector(".search-result-kind")?.textContent).toBe("Submission");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(location.hash).toMatch(/^#\/submissions\?agent=/);
  });

  it("closes with Escape", () => {
    renderSearch();
    type("5");
    expect(document.getElementById("search-popover")).not.toHaveAttribute("hidden");
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(document.getElementById("search-popover")).toHaveAttribute("hidden");
    expect(input()).toHaveAttribute("aria-expanded", "false");
  });

  it('focuses the field from anywhere with "/" and Cmd/Ctrl+K', () => {
    renderSearch();
    input().blur();
    fireEvent.keyDown(document.body, { key: "/" });
    expect(document.activeElement).toBe(input());
    input().blur();
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(input());
  });

  it('does not hijack "/" while typing in another field', () => {
    render(() => (
      <>
        <input id="other-field" />
        <GlobalSearch miners={() => miners} submissions={() => submissions} />
      </>
    ));
    const other = document.getElementById("other-field") as HTMLInputElement;
    other.focus();
    fireEvent.keyDown(other, { key: "/" });
    expect(document.activeElement).toBe(other);
  });

  it("explains itself before a query and names a miss honestly", () => {
    renderSearch();
    const el = input();
    el.focus();
    fireEvent.focus(el);
    expect(document.getElementById("search-meta")?.textContent).toBe(
      "Search by hotkey, agent name, or ID",
    );
    type("zzzznotathing");
    expect(document.getElementById("search-meta")?.textContent).toBe("No results");
    expect(document.querySelector(".search-empty")?.textContent).toContain(
      "No miner or submission matches",
    );
  });

  it("dedupes submissions by agent id in the corpus", () => {
    const duplicated = [...submissions, ...submissions];
    render(() => <GlobalSearch miners={() => []} submissions={() => duplicated} />);
    const submission = submissions.find((entry) => entry.name) as PipelineEntry;
    type(String(submission.name));
    const titles = options().map((opt) => opt.querySelector(".search-result-title")?.textContent);
    expect(new Set(titles).size).toBe(titles.length);
  });

  it("does not recover a stricken handle from the operations corpus", () => {
    const stolen: PipelineEntry = {
      agent_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      name: "Jupiter-ditto-v10",
      name_handle: {
        stem: "jupiter",
        status: "disputed",
        claim_id: "11111111-1111-1111-1111-111111111111",
      },
      version: 1,
      status: "scored",
      miner_hotkey: "5StolenHandleHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    };
    render(() => <GlobalSearch miners={() => []} submissions={() => [stolen]} />);
    type("jupiter");
    expect(options()).toHaveLength(0);
    type("Unnamed submission");
    expect(options()[0]?.querySelector(".search-result-title")?.textContent).toContain(
      "Unnamed submission",
    );
  });
});
