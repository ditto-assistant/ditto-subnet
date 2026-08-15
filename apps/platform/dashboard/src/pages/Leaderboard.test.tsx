// Parity tests for the dedicated leaderboard page port. Each block cites the
// inventory row (dashboard-refactor-notes/assert-inventory.md) it carries
// forward and keeps the old test's rationale as comments. The negative greps
// (board-rail/details, tie chips) live in src/build-invariants.test.ts.
import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  boardPageSize,
  resetBoardState,
  setLeaderboardVersionView,
} from "../components/board/board-state";
import { fx, fxScore, num } from "../lib/format";
import { dethroneFloor, displayComposite, rankEntries } from "../lib/scoring";
import { syncFromLocation } from "../stores/routeStore";
import { fixtureNameFor, loadFixture } from "../test-fixtures";
import type { LeaderboardEntry, LeaderboardPayload, RolloutState } from "../types/leaderboard";
import { LeaderboardPage } from "./LeaderboardPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const leaderboardCss = readFileSync(
  join(HERE, "..", "styles", "pages", "leaderboard.css"),
  "utf-8",
);
const cssNorm = leaderboardCss.replace(/\s+/g, " ");

const leaderboard = loadFixture<LeaderboardPayload>("leaderboard");
const ranked = rankEntries(leaderboard.entries ?? []);
const emissions = leaderboard.emissions ?? null;
const championEntry = ranked.find(
  (e) => String(e.agent_id) === String(emissions?.champion_agent_id),
) as LeaderboardEntry & { rank: number | null };
const rawLeaderEntry = ranked.find(
  (e) => String(e.agent_id) === String(emissions?.raw_leader_agent_id),
) as LeaderboardEntry & { rank: number | null };

interface FetchOptions {
  onRequest?: (path: string) => void;
  patch?: (name: string, body: unknown, path: string) => unknown;
}

function installFetch(options: FetchOptions = {}): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://fixtures.test");
    const path = url.pathname.replace(/^.*?(?=\/public\/)/, "") + url.search;
    options.onRequest?.(path);
    const name = path.startsWith("/public/") ? fixtureNameFor(path) : null;
    if (name === null) {
      return Promise.resolve(new Response(JSON.stringify({ detail: "missing" }), { status: 404 }));
    }
    let body: unknown = loadFixture(name);
    if (options.patch) body = options.patch(name, body, path) ?? body;
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

let restoreFetch: (() => void) | null = null;

beforeEach(() => {
  history.replaceState(null, "", "/#/leaderboard");
  syncFromLocation();
  resetBoardState();
  setLeaderboardVersionView("current");
});

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
});

function renderPage(options: FetchOptions = {}): void {
  restoreFetch = installFetch(options);
  render(() => <LeaderboardPage />);
}

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw new Error("missing #" + id);
  return node;
}

async function waitForBoard(): Promise<void> {
  await waitFor(() => {
    expect(document.querySelectorAll("#rows tr[data-i]").length).toBeGreaterThan(0);
  });
}

// ── Row 3 (leaderboard slice): the dedicated page restores every column ──
// "Compactness through a second surface, not through disclosure": the full
// column set lives here, un-hidden at every width by the page-scoped
// tri-state CSS. That exact rule was a pinned assert in the old suite.
describe("dedicated leaderboard page (row 3 slice)", () => {
  it("hosts the single leaderboard block in #leaderboard-page-host", async () => {
    renderPage();
    await waitForBoard();
    const host = el("leaderboard-page-host");
    expect(host.querySelector("#leaderboard-block")).toBeTruthy();
    // Nothing is wrapped in a disclosure; there is no rail.
    expect(document.querySelector("details #board")).toBeNull();
    expect(document.querySelector("aside.board-rail")).toBeNull();
  });

  it("shows an active efficiency award beside the final folded score", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const entries = (payload.entries ?? []).map((entry) => ({
          ...entry,
          official_composite: 0.756,
          effective_composite: 0.756,
          pre_efficiency_composite: 0.72,
          efficiency_bonus: 0.05,
          efficiency_fold_applied: true,
        }));
        return { ...payload, entries };
      },
    });
    await waitForBoard();
    await waitFor(() =>
      expect(document.querySelector(".cline2 .efficiency-bonus-chip")?.textContent).toBe(
        "efficiency +5.0%",
      ),
    );
    const chip = document.querySelector(".cline2 .efficiency-bonus-chip");
    expect(chip?.textContent).toBe("efficiency +5.0%");
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining(
        "Relative token efficiency adds 5.0% after continual retest aggregation",
      ),
    );
    expect(chip).toHaveAttribute("data-tooltip", expect.stringContaining("0.720 becomes 0.756"));
  });

  it("shows a bounded v9 efficiency penalty without calling it an upside award", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const entries = (payload.entries ?? []).map((entry) => ({
          ...entry,
          bench_version: 9,
          official_composite: 0.72,
          effective_composite: 0.612,
          pre_efficiency_composite: 0.72,
          efficiency_bonus: null,
          efficiency_factor: 0.85,
          efficiency_fold_applied: true,
        }));
        return { ...payload, entries };
      },
    });
    await waitForBoard();
    const chip = await waitFor(() => {
      const value = document.querySelector(".cline2 .efficiency-bonus-chip");
      expect(value?.textContent).toBe("efficiency tie-break ▼ 15.0% (floor)");
      return value;
    });
    expect(chip).toHaveClass("penalized");
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("frozen cohort P25 reference"),
    );
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("produces tie-break value 0.612"),
    );
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("quality score stays 0.720 and remains the primary ranking key"),
    );
  });

  it("makes the Bench v9 remaining-headroom uplift visible", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const entries = (payload.entries ?? []).map((entry) => ({
          ...entry,
          bench_version: 9,
          official_composite: 0.95,
          effective_composite: 0.955,
          pre_efficiency_composite: 0.95,
          efficiency_bonus: null,
          efficiency_factor: 1.1,
          efficiency_fold_applied: true,
        }));
        return { ...payload, entries };
      },
    });
    await waitForBoard();
    const chip = await waitFor(() => {
      const value = document.querySelector(".cline2 .efficiency-bonus-chip");
      expect(value?.textContent).toBe("efficiency tie-break ▲ 10.0% (cap)");
      return value;
    });
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("remaining quality headroom"),
    );
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("frozen cohort P25 reference is +10.0%"),
    );
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("0.950 + (1.100 − 1) × (1 − 0.950) = 0.955"),
    );
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("produces tie-break value 0.955"),
    );
  });

  it("marks an unapplied v9 factor as an audit-only projection", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const entries = (payload.entries ?? []).map((entry) => ({
          ...entry,
          bench_version: 9,
          official_composite: 0.72,
          effective_composite: 0.612,
          pre_efficiency_composite: 0.72,
          efficiency_bonus: null,
          efficiency_factor: 0.85,
        }));
        return { ...payload, entries };
      },
    });
    await waitForBoard();
    const chip = await waitFor(() => {
      const value = document.querySelector(".cline2 .efficiency-bonus-chip");
      expect(value?.textContent).toBe("efficiency tie-break preview ▼ 15.0% (floor)");
      return value;
    });
    expect(chip).toHaveClass("partial", "penalized");
    expect(chip).not.toHaveClass("settled");
    expect(chip).toHaveAttribute("data-tooltip", expect.stringContaining("projects to 0.612"));
    expect(chip).toHaveAttribute("data-tooltip", expect.stringContaining("audit-only projection"));
    expect(chip).toHaveAttribute(
      "data-tooltip",
      expect.stringContaining("current ranking and emissions remain based on 0.720"),
    );
  });

  it("re-asserts hide-md/hide-sm as table-cell for this board only (the pinned CSS)", () => {
    // The page's promise is every column at every viewport: undo the
    // window-width column hiding for this board only. The table keeps its
    // 1000px floor and scrolls inside .board, so all-columns stays honest on
    // a phone.
    expect(cssNorm).toContain(
      '.page[data-page="leaderboard"] #board .hide-md, ' +
        '.page[data-page="leaderboard"] #board .hide-sm { display: table-cell; }',
    );
  });

  it("combines identity and scores into six compact columns", async () => {
    renderPage();
    await waitForBoard();
    expect(document.querySelectorAll("#board thead th").length).toBe(6);
    for (const key of ["rank", "composite", "cost", "latency", "first_seen"]) {
      expect(document.querySelector('th.sortable[data-sort="' + key + '"]'), key).toBeTruthy();
    }
    expect(document.querySelector('th[data-sort="name"]')).toBeNull();
    expect(document.querySelector('th[data-sort="bench"]')).toBeNull();
    expect(document.querySelector(".modelcell")).toBeNull();
    expect(document.querySelectorAll("#rows tr[data-i]:first-child .score-stack-row")).toHaveLength(
      3,
    );
    // Emissions is present but deliberately not sortable; its tip explains
    // the KOTH role.
    expect(el("emissions-col-tip").closest("th")?.hasAttribute("data-sort")).toBe(false);
  });

  it("states why an empty cohort adjusted nothing, instead of rendering blank", async () => {
    // The 2026-08-13 production shape: the preview ran and qualified nobody.
    // Per-row chips need a factor to render, so without this strip the board
    // is silent and a switched-off adjustment is indistinguishable from an
    // active one that assigned nothing.
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          efficiency: {
            active: false,
            preview: true,
            bench_version: 9,
            curve_version: 3,
            cohort_size: 0,
            n_min: 8,
            reference_p25_tokens: null,
            factor_alpha: 0.25,
            minimum_factor: 0.85,
            maximum_factor: 1.1,
          },
        };
      },
    });
    await waitForBoard();
    // Wait on the tone, not on the strip: the strip renders for every payload,
    // so waiting for it to appear would assert against the previous fixture.
    await waitFor(() =>
      expect(el("efficiency-strip").querySelector(".efficiency-summary")).toHaveAttribute(
        "data-tone",
        "dormant",
      ),
    );
    const strip = el("efficiency-strip");
    expect(strip).toHaveClass("show");
    expect(strip.textContent).toContain("not adjusting any score");
    expect(strip.textContent).toContain("No agent has qualified");
    expect(strip.textContent).toContain("qualified cohort 0 / 8 required");
    // No cohort means no reference cost; inventing one would be a fiction.
    expect(strip.textContent).not.toContain("neutral reference");
  });

  it("marks a qualified cohort as a projection while the fold is off", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          efficiency: {
            active: false,
            preview: true,
            bench_version: 9,
            curve_version: 3,
            cohort_size: 12,
            n_min: 8,
            reference_p25_tokens: 1_800_000,
            factor_alpha: 0.25,
            minimum_factor: 0.85,
            maximum_factor: 1.1,
          },
        };
      },
    });
    await waitForBoard();
    await waitFor(() =>
      expect(el("efficiency-strip").querySelector(".efficiency-summary")).toHaveAttribute(
        "data-tone",
        "projected",
      ),
    );
    const strip = el("efficiency-strip");
    expect(strip).toHaveClass("show");
    expect(strip.textContent).toContain("switched off");
    expect(strip.textContent).toContain("Nothing here moves a rank");
    expect(strip.textContent).toContain("neutral reference " + num(1_800_000) + " tokens");
    expect(strip.textContent).toContain("bounded ×" + fx(0.85) + " to ×" + fx(1.1));
  });

  it("keeps the strips as closing context after the board", async () => {
    renderPage();
    await waitForBoard();
    const board = document.querySelector('.board[tabindex="0"]') as HTMLElement;
    for (const id of [
      "emissions-strip",
      "efficiency-strip",
      "rollout-strip",
      "leaderboard-notice",
    ]) {
      expect(
        board.compareDocumentPosition(el(id)) & Node.DOCUMENT_POSITION_FOLLOWING,
        id,
      ).toBeTruthy();
    }
  });

  it("resets an archive view back to the current rollout on entry", async () => {
    // The router forces the page to the current rollout: an archive can never
    // render here (archive browsing lives on the overview).
    setLeaderboardVersionView("6");
    renderPage();
    await waitForBoard();
    const current = document.querySelector('[data-leaderboard-version="current"]') as HTMLElement;
    expect(current).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(el("leaderboard-title").textContent).not.toContain("history"));
  });
});

// ── Row 4: test_leaderboard_carries_a_filter_over_name_uid_and_hotkey ──
// "The one capability worth keeping from #383's rail" — an in-place table
// filter (vs. the shell's global search which jumps to a single record);
// it must reset paging and live on the board toolbar, not a sidebar.
describe("board filter (row 4)", () => {
  it("lives on the board toolbar with its clear control", async () => {
    renderPage();
    await waitForBoard();
    const toolbar = document.querySelector(".board-toolbar") as HTMLElement;
    expect(toolbar).toBeTruthy();
    expect(toolbar.querySelector("#board-filter")).toBeTruthy();
    expect(toolbar.querySelector("#board-filter-clear")).toBeTruthy();
  });

  it("filters by agent name, UID, and hotkey, and the counts follow", async () => {
    renderPage();
    await waitForBoard();
    const input = el("board-filter") as HTMLInputElement;
    const target = ranked[0] as LeaderboardEntry;

    fireEvent.input(input, { target: { value: target.agent_name as string } });
    await waitFor(() => expect(document.querySelectorAll("#rows tr[data-i]").length).toBe(1));

    fireEvent.input(input, { target: { value: "uid " + target.miner_uid } });
    await waitFor(() => {
      const rows = document.querySelectorAll("#rows tr[data-i]");
      expect(rows.length).toBe(1);
      expect((rows[0] as HTMLElement).textContent).toContain(target.agent_name as string);
    });

    fireEvent.input(input, { target: { value: (target.miner_hotkey || "").slice(0, 12) } });
    await waitFor(() => expect(document.querySelectorAll("#rows tr[data-i]").length).toBe(1));
  });

  it("states when no miner matches and Escape clears back to the full board", async () => {
    renderPage();
    await waitForBoard();
    const input = el("board-filter") as HTMLInputElement;
    fireEvent.input(input, { target: { value: "no-such-miner-xyz" } });
    await waitFor(() => expect(el("rows").textContent).toContain("No miner matches that filter."));
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() =>
      expect(document.querySelectorAll("#rows tr[data-i]").length).toBe(ranked.length),
    );
    expect(input.value).toBe("");
  });

  it("resets paging when the filter narrows the list", async () => {
    // "A narrowed list rarely contains the page you were on."
    const bigBoard = (body: unknown): unknown => {
      const payload = body as LeaderboardPayload;
      const template = (payload.entries ?? [])[0] as LeaderboardEntry;
      const entries: LeaderboardEntry[] = [];
      for (let i = 0; i < boardPageSize + 5; i += 1) {
        entries.push({
          ...template,
          agent_id: "gen-" + i,
          agent_name: "gen-miner-" + i,
          miner_hotkey: "5Gen" + String(i).padStart(44, "0"),
          miner_uid: 500 + i,
          submission_family: undefined,
          official_composite: 0.9 - i * 0.001,
        } as LeaderboardEntry);
      }
      return { ...payload, entries };
    };
    renderPage({ patch: (name, body) => (name === "leaderboard" ? bigBoard(body) : body) });
    await waitForBoard();
    // The shared store keeps last-good rows across mounts; wait for the
    // patched 30-row board before paging into it.
    await waitFor(() => expect(el("board-pinfo").textContent).toContain("30 scored"));
    fireEvent.click(el("board-next"));
    await waitFor(() => expect(el("board-pinfo").textContent).toContain("Page 2 of 2"));
    const input = el("board-filter") as HTMLInputElement;
    fireEvent.input(input, { target: { value: "gen-miner-1" } });
    await waitFor(() => expect(el("board-pinfo").textContent).toContain("Page 1 of 1"));
  });
});

// ── Row 1 (page slice): sort, tabs, pager, and rank vocabulary ──
describe("board view controls (row 1 slice)", () => {
  it("keeps an accepted retest seed visible on the exact hidden family submission", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          entries: (payload.entries ?? []).map((entry, index) =>
            index === 0
              ? {
                  ...entry,
                  submission_family: {
                    members: [
                      {
                        agent_id: "11111111-1111-4111-8111-111111111111",
                        agent_name: "prior-version",
                        agent_version: 4,
                        canonical_composite: 0.91,
                        confirmation_seed_depth: 1,
                      },
                    ],
                  },
                }
              : entry,
          ),
        } satisfies LeaderboardPayload;
      },
    });
    await waitForBoard();

    const toggle = await waitFor(() => {
      const value = document.querySelector<HTMLButtonElement>("[data-family-toggle]");
      expect(value).toBeTruthy();
      return value as HTMLButtonElement;
    });
    fireEvent.click(toggle);

    const chip = await waitFor(() => {
      const value = document.querySelector(".family-child:not([hidden]) .retest-seed-chip");
      expect(value?.textContent).toBe("1 retest seed");
      return value;
    });
    expect(chip).toHaveClass("settled");
    expect(chip).toHaveAttribute("data-tooltip", expect.stringContaining("this exact submission"));
  });

  it("labels all v9 confirmation states and suppresses pending rows in enforce mode", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          v9_confirmation_mode: "enforce",
          entries: (payload.entries ?? []).map((entry, index) =>
            index < 3
              ? {
                  ...entry,
                  bench_version: 9,
                  eligible: true,
                  finalized: true,
                  v9_confirmation_status: ["base_only", "provisional", "full_confirmed"][
                    index
                  ] as LeaderboardEntry["v9_confirmation_status"],
                }
              : entry,
          ),
        };
      },
    });
    await waitForBoard();
    await waitFor(() => expect(document.querySelectorAll(".v9-confirmation-chip")).toHaveLength(3));
    const rows = Array.from(document.querySelectorAll<HTMLElement>("#rows tr[data-i]"));
    for (const label of ["Bench 9 base only", "Bench 9 confirmation pending"]) {
      const row = rows.find((candidate) => candidate.textContent?.includes(label));
      expect(row).toBeTruthy();
      expect(row?.querySelector(".rank")?.textContent).toBe("–");
      expect(row?.querySelector(".emission-badge")).toBeNull();
    }
    const full = rows.find((candidate) =>
      candidate.textContent?.includes("Bench 9 full confirmed"),
    );
    expect(full).toBeTruthy();
    expect(full?.querySelector(".rank")?.textContent).not.toBe("–");
  });

  it("shows shadow collection while preserving authoritative base rank and emissions", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          v9_confirmation_mode: "shadow",
          entries: (payload.entries ?? []).map((entry, index) =>
            index < 2
              ? {
                  ...entry,
                  bench_version: 9,
                  eligible: true,
                  finalized: true,
                  v9_confirmation_status: index === 0 ? "base_only" : "provisional",
                }
              : entry,
          ),
        } satisfies LeaderboardPayload;
      },
    });
    await waitForBoard();
    await waitFor(() => expect(document.querySelectorAll(".v9-confirmation-chip")).toHaveLength(2));
    expect(el("leaderboard-notice")).toHaveTextContent("LongMemEval shadow is active");
    const rows = Array.from(document.querySelectorAll<HTMLElement>("#rows tr[data-i]"));
    for (const label of ["Bench 9 LongMem shadow queued", "Bench 9 LongMem shadow running"]) {
      const row = rows.find((candidate) => candidate.textContent?.includes(label));
      expect(row).toBeTruthy();
      expect(row?.querySelector(".rank")?.textContent).not.toBe("–");
      expect(row?.querySelector(".emission-badge")).toBeTruthy();
    }
  });

  it("defaults to the Scored tab with live counts (provisional is pre-quorum feedback)", async () => {
    renderPage();
    await waitForBoard();
    expect(document.querySelector('[data-board-tab="scored"]')).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // waitFor: the shared store may still hold the previous test's board.
    await waitFor(() =>
      expect(document.querySelector('[data-board-count="all"]')?.textContent).toBe("12"),
    );
    expect(document.querySelector('[data-board-count="scored"]')?.textContent).toBe("12");
    expect(document.querySelector('[data-board-count="provisional"]')?.textContent).toBe("0");
    expect(el("board-pinfo").textContent).toBe("Page 1 of 1 · 12 scored");
  });

  it("sorts rank/name ascending by default and score columns descending", async () => {
    renderPage();
    await waitForBoard();
    const rankTh = document.querySelector('th[data-sort="rank"]') as HTMLElement;
    expect(rankTh).toHaveAttribute("aria-sort", "ascending");
    const compositeTh = document.querySelector('th[data-sort="composite"]') as HTMLElement;
    fireEvent.click(compositeTh);
    await waitFor(() => expect(compositeTh).toHaveAttribute("aria-sort", "descending"));
    expect(rankTh.hasAttribute("aria-sort")).toBe(false);
    // Toggling the same key flips direction.
    fireEvent.click(compositeTh);
    await waitFor(() => expect(compositeTh).toHaveAttribute("aria-sort", "ascending"));
    const first = document.querySelector("#rows tr[data-i]") as HTMLElement;
    // Ascending composite puts the lowest displayed composite first.
    const comps = ranked.map((e) => displayComposite(e)).sort((a, b) => a - b);
    expect(first.textContent).toContain(fx(comps[0] as number));
  });

  it("keeps ranked standings above zero-score rows under alternate sorts", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          entries: (payload.entries ?? []).map((entry, index) =>
            index === 0
              ? {
                  ...entry,
                  agent_name: "ranked-costly",
                  eligible: true,
                  finalized: true,
                  average_run_cost_microusd: 10_000,
                }
              : index === 1
                ? {
                    ...entry,
                    agent_name: "unranked-free",
                    eligible: false,
                    finalized: true,
                    average_run_cost_microusd: 0,
                  }
                : entry,
          ),
        } satisfies LeaderboardPayload;
      },
    });
    await waitForBoard();
    await waitFor(() => expect(document.body.textContent).toContain("ranked-costly"));
    const costTh = document.querySelector('th[data-sort="cost"]') as HTMLElement;
    fireEvent.click(costTh);
    fireEvent.click(costTh);
    await waitFor(() => expect(costTh).toHaveAttribute("aria-sort", "ascending"));

    const rows = Array.from(document.querySelectorAll<HTMLElement>("#rows tr[data-i]"));
    const rankedRow = rows.findIndex((row) => row.textContent?.includes("ranked-costly"));
    const unrankedRow = rows.findIndex((row) => row.textContent?.includes("unranked-free"));
    expect(rankedRow).toBeGreaterThanOrEqual(0);
    expect(unrankedRow).toBeGreaterThan(rankedRow);
  });

  it("explains alternate ordering and restores canonical rank order", async () => {
    renderPage();
    await waitForBoard();
    const costTh = document.querySelector('th[data-sort="cost"]') as HTMLElement;
    const rankTh = document.querySelector('th[data-sort="rank"]') as HTMLElement;

    fireEvent.click(costTh);
    await waitFor(() => expect(costTh).toHaveAttribute("aria-sort", "descending"));
    expect(document.querySelector(".board-sort-state")?.textContent).toContain(
      "Sorted by Average run cost · high to low. Canonical ranks stay fixed.",
    );
    expect(rankTh.querySelector(".sarrow")?.textContent).toBe("↕");

    fireEvent.click(document.querySelector(".board-sort-reset") as HTMLElement);
    await waitFor(() => expect(rankTh).toHaveAttribute("aria-sort", "ascending"));
    expect(document.querySelector(".board-sort-state")).toBeNull();
  });

  it("keeps the header's tooltip term as the keyboard sort control", async () => {
    renderPage();
    await waitForBoard();
    const costTh = document.querySelector('th[data-sort="cost"]') as HTMLElement;
    const tip = costTh.querySelector(".tip") as HTMLElement;
    expect(tip).toHaveAttribute("role", "button");
    fireEvent.keyDown(costTh, { key: "Enter" });
    await waitFor(() => expect(costTh).toHaveAttribute("aria-sort", "descending"));
  });

  it("shows API-backed average run cost and its completed-run sample count", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          entries: (payload.entries ?? []).map((entry, index) =>
            index === 0
              ? { ...entry, average_run_cost_microusd: 123_400, inference_run_count: 7 }
              : entry,
          ),
        };
      },
    });
    await waitForBoard();
    await waitFor(() =>
      expect(document.querySelector(".run-cost-cell")?.textContent).toContain("$0.123"),
    );
    expect(document.querySelector(".run-cost-cell")?.textContent).toContain("7 completed");
  });

  it("opens a row's miner drill-down route on click (plain tabbable tr, no role=button)", async () => {
    renderPage();
    await waitForBoard();
    const row = document.querySelector("#rows tr[data-i]") as HTMLElement;
    expect(row).toHaveAttribute("tabindex", "0");
    expect(row.hasAttribute("role")).toBe(false);
    fireEvent.click(row);
    await waitFor(() =>
      expect(location.hash).toContain("miner=" + (ranked[0] as LeaderboardEntry).miner_hotkey),
    );
  });
});

// ── Row 36: test_renders_the_dethrone_floor_and_rollout_state ──
// Consensus parameters are API-served: the "score to beat" is its own
// element computed from the API-served margin (floor = champ + margin×scale,
// never champ×(1+margin)), published as a floor not a guarantee, and the
// rollout strip reads its threshold from /public/bench/rollout.
describe("dethrone floor + rollout strip (row 36)", () => {
  it("publishes the additive floor as its own element, as a floor not a guarantee", async () => {
    renderPage();
    await waitForBoard();
    await waitFor(() => expect(el("emissions-threshold").classList.contains("show")).toBe(true));
    const threshold = el("emissions-threshold");
    expect(threshold.textContent).toContain("Beat this to contend:");
    const floor = dethroneFloor(emissions, championEntry);
    expect(floor).not.toBeNull();
    const f = floor as NonNullable<ReturnType<typeof dethroneFloor>>;
    expect(threshold.querySelector(".beat")?.textContent).toBe(fx(f.floor));
    expect(threshold.textContent).toContain("The champion holds " + fx(f.champComposite));
    expect(threshold.textContent).toContain(num(f.effectiveMargin) + " composite points");
    // dethrone_z > 0 in the fold, so the statistical-band caveat renders.
    expect(threshold.textContent).toContain("this is a floor, not a guarantee");
    expect(threshold.textContent).toContain("A lead inside the band is not rejected");
  });

  it("renders the rollout strip from /public/bench/rollout (activated state)", async () => {
    renderPage();
    await waitForBoard();
    await waitFor(() => expect(el("rollout-strip").classList.contains("show")).toBe(true));
    expect(el("rollout-strip")).toHaveAttribute("role", "status");
    expect(el("rollout-strip")).toHaveAttribute("aria-live", "polite");
    expect(el("rollout-head").textContent).toContain("Benchmark rollout");
    expect(el("rollout-head").textContent).toContain("activated");
    expect(el("rollout-progress").textContent).toBe(
      "v7 is activated and drives validator weights.",
    );
    expect(el("rollout-note").textContent).toContain("The whole ledger ranks on v7.");
  });

  it("reads the quorum threshold and bounded inherited cohort from the rollout payload", async () => {
    const collecting = (body: unknown): RolloutState => ({
      ...(body as RolloutState),
      active_version: 7,
      desired_version: 8,
      status: "collecting",
      priority_complete: true,
      cohort_size: 10,
      cohort_ready_count: 4,
    });
    renderPage({
      patch: (name, body) => (name === "bench-rollout" ? collecting(body) : body),
    });
    await waitForBoard();
    await waitFor(() =>
      expect(el("rollout-progress").textContent).toContain(
        "inherited top-cohort agents have complete v8 quorums.",
      ),
    );
    expect(el("rollout-progress").textContent).toContain("4 of 10");
    // The cohort size is the payload's number, never a literal.
    expect(el("rollout-note").textContent).toContain(
      "This rollout's bounded inherited cohort has 10 miners",
    );
  });

  it("keeps a superseded rollout from reading as in progress", async () => {
    const superseded = (body: unknown): RolloutState => ({
      ...(body as RolloutState),
      active_version: 7,
      desired_version: 8,
      status: "superseded",
    });
    renderPage({
      patch: (name, body) => (name === "bench-rollout" ? superseded(body) : body),
    });
    await waitForBoard();
    await waitFor(() =>
      expect(el("rollout-progress").textContent).toContain(
        "The v8 rollout was superseded before activation",
      ),
    );
    expect(el("rollout-note").textContent).toBe(
      "Weights remain on v7 throughout. A superseded rollout never moves emissions.",
    );
  });
});

// ── Held-crown standing clarity ──
// The fixture's fold holds the crown at raw #2 while raw #1 leads inside the
// dethrone band — the exact standing that reads as "why isn't #1 the
// champion?" and floods the operator channels. The board must say so above
// the table, dim the not-yet-dethroning leaders, and crown the incumbent's
// row, all from fold-fed numbers.
describe("held-crown standing clarity", () => {
  it("calls out the held crown above the board with the exact fold decision", async () => {
    renderPage();
    await waitForBoard();
    await waitFor(() => expect(el("koth-standing").classList.contains("show")).toBe(true));
    const callout = el("koth-standing");
    expect(callout).toHaveAttribute("role", "note");
    expect(callout.textContent).toContain((rawLeaderEntry.agent_name as string) + " is rank #1");
    expect(callout.textContent).toContain(championEntry.agent_name as string);
    expect(callout.textContent).toContain("remains champion");
    expect(callout.textContent).toContain(
      "Rank / KOTH score" + fxScore(displayComposite(rawLeaderEntry)),
    );
    expect(callout.textContent).toContain(
      "Paired-seed lead+" + fxScore(emissions?.raw_leader_decision?.challenger_lead as number),
    );
    expect(callout.textContent).toContain(
      "Lead required>+" + fxScore(emissions?.raw_leader_decision?.required_lead as number),
    );
    expect(callout.textContent).toContain(
      fxScore(rawLeaderEntry.composite) + " raw quorum median is not the crown score",
    );
    expect(callout.textContent).toContain("Paired shared-seed comparison");
  });

  it("dims every higher-scoring row and notes it outscores without dethroning", async () => {
    renderPage();
    await waitForBoard();
    await waitFor(() =>
      expect(document.querySelectorAll("tr.above-champion")).toHaveLength(
        (championEntry.rank as number) - 1,
      ),
    );
    const dimmed = document.querySelector("tr.above-champion") as HTMLElement;
    // The dimmed leader keeps its tail badge (it still earns tail emissions)
    // and gains the not-dethroned note with the exact decision in its tooltip.
    const note = dimmed.querySelector(".above-champion-note") as HTMLElement;
    expect(note.textContent).toBe("#1 challenger · crown held");
    expect(note.getAttribute("data-tooltip")).toContain(
      "KOTH lead is +" + fxScore(emissions?.raw_leader_decision?.challenger_lead as number),
    );
    expect(note.getAttribute("data-tooltip")).toContain(
      "requires more than +" + fxScore(emissions?.raw_leader_decision?.required_lead as number),
    );
    expect(dimmed.getAttribute("aria-label")).toContain(
      "outscores the champion but has not cleared the dethrone band",
    );
    // The champion row is never dimmed and wears the crown in its rank chip.
    const championRow = document.querySelector("tr.champion") as HTMLElement;
    expect(championRow.classList.contains("above-champion")).toBe(false);
    expect(championRow.querySelector(".rank .rank-crown")?.textContent).toBe("♛");
    // Rows ranked below the champion are untouched: the treatment marks the
    // held-out leaders, not everything that isn't the champion.
    expect(document.querySelectorAll("#rows tr[data-i]").length).toBeGreaterThan(
      document.querySelectorAll("tr.above-champion").length + 1,
    );
  });

  it("names an unpaired threshold and exact dethrone score without presenting the floor as enough", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          emissions: {
            ...payload.emissions,
            raw_leader_decision: {
              method: "unpaired",
              challenger_lead: 0.0185715,
              required_lead: 0.046305194,
              required_score: 0.750175444,
            },
          },
        };
      },
    });
    await waitForBoard();
    await waitFor(() =>
      expect(el("koth-standing-copy").textContent).toContain("Unpaired uncertainty band"),
    );
    const copy = el("koth-standing-copy").textContent;
    expect(copy).toContain("Current lead+0.018572");
    expect(copy).toContain("Lead required>+0.046305");
    expect(copy).toContain("Dethrone score>0.750175");
    expect(copy).not.toContain("beat 0.710");
  });

  it("pins the dimming and hover-restore rules in the stylesheet", () => {
    expect(cssNorm).toContain(
      "tbody tr.above-champion > td { opacity: 0.55; transition: opacity 0.15s ease; }",
    );
    expect(cssNorm).toContain(
      "tbody tr.above-champion:hover > td, tbody tr.above-champion:focus-visible > td { opacity: 1; }",
    );
  });

  it("stands down entirely when the raw leader holds the crown", async () => {
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        // Same fold with the crown on raw #1: no held-crown standing exists.
        return {
          ...payload,
          emissions: {
            ...payload.emissions,
            champion_agent_id: payload.emissions?.raw_leader_agent_id,
          },
        };
      },
    });
    await waitForBoard();
    // The shared store may briefly hold the previous test's board; wait for
    // the patched fold to land (the callout drops its .show).
    await waitFor(() => expect(el("koth-standing").classList.contains("show")).toBe(false));
    expect(document.querySelectorAll("tr.above-champion")).toHaveLength(0);
    expect(document.querySelector(".above-champion-note")).toBeNull();
  });

  it("shows a joint crown instead of an impossible single-winner threshold", async () => {
    const joint = ranked.slice(0, 2);
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        return {
          ...payload,
          emissions: {
            ...payload.emissions,
            allocation_mode: "score_ceiling_pool",
            score_ceiling_pool_size: joint.length,
            recipients: joint.map((entry) => ({
              role: "joint_champion",
              agent_id: entry.agent_id,
              miner_hotkey: entry.miner_hotkey,
              share_of_miner_pool: 1 / joint.length,
            })),
          },
        };
      },
    });
    await waitForBoard();
    await waitFor(() =>
      expect(el("koth-standing-copy").textContent).toContain("Score-ceiling joint crown"),
    );
    expect(el("koth-standing-copy").textContent).toContain(
      "2 highest evidence-tied agents split the full miner pool equally",
    );
    expect(el("emissions-title").textContent).toContain(
      "2 evidence-tied agents each receive 50.0%",
    );
    expect(el("emissions-threshold").classList.contains("show")).toBe(false);
    expect(document.querySelectorAll("tr.joint-champion")).toHaveLength(2);
    expect(document.querySelectorAll("tr.above-champion")).toHaveLength(0);
  });
});

describe("registration-aware crown clarity", () => {
  it("crowns the registered successor and keeps the former champion visibly inactive", async () => {
    const formerChampion = championEntry;
    const successor = ranked[0] as LeaderboardEntry & { rank: number | null };
    renderPage({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const remainingTail = (payload.emissions?.recipients ?? []).filter(
          (recipient) =>
            String(recipient.agent_id) !== String(formerChampion.agent_id) &&
            String(recipient.agent_id) !== String(successor.agent_id),
        );
        return {
          ...payload,
          entries: (payload.entries ?? []).map((entry) =>
            String(entry.agent_id) === String(formerChampion.agent_id)
              ? { ...entry, registered: false, emission_eligible: false, miner_uid: null }
              : entry,
          ),
          emissions: {
            ...payload.emissions,
            champion_agent_id: successor.agent_id,
            champion_miner_hotkey: successor.miner_hotkey,
            raw_leader_agent_id: successor.agent_id,
            raw_leader_miner_hotkey: successor.miner_hotkey,
            raw_leader_decision: null,
            recipients: [
              {
                role: "champion",
                agent_id: successor.agent_id,
                miner_hotkey: successor.miner_hotkey,
                raw_rank: 1,
                share_of_miner_pool: 0.65,
                shared_seed_confirmations: 0,
              },
              ...remainingTail,
            ],
          },
        };
      },
    });
    await waitForBoard();
    await waitFor(() =>
      expect(el("emissions-title").textContent).toContain(successor.agent_name as string),
    );

    const rowFor = (name: string): HTMLElement | undefined =>
      Array.from(document.querySelectorAll("#rows tr[data-i]")).find((row) =>
        row.textContent?.includes(name),
      ) as HTMLElement | undefined;

    await waitFor(() =>
      expect(rowFor(formerChampion.agent_name as string)?.textContent).toContain("not registered"),
    );
    const formerRow = rowFor(formerChampion.agent_name as string) as HTMLElement;
    const successorRow = rowFor(successor.agent_name as string) as HTMLElement;
    expect(formerRow.textContent).toContain("not registered");
    expect(formerRow).not.toHaveClass("champion");
    expect(formerRow.querySelector(".rank-crown")).toBeNull();
    expect(successorRow).toHaveClass("champion");
    expect(successorRow.querySelector(".rank-crown")?.textContent).toBe("♛");
    expect(el("leaderboard-notice").textContent).toContain("cannot hold the KOTH crown");
  });
});

// ── Row 1 (chip vocabulary slice): the composite cell chips ──
describe("composite cell chips (row 1 slice)", () => {
  it("carries the continual seed-round chip (the dot plot stays retired)", async () => {
    // The per-sample dot plot was a debugging view; the row now carries the
    // seed-round count and keeps the mean's composition in the tooltip.
    renderPage();
    await waitForBoard();
    const chip = document.querySelector(".rollout-chip.settled.seed-rounds-chip.tip");
    expect(chip).toBeTruthy();
    expect(chip?.textContent).toMatch(/^\d+ seeds?$/);
    expect(chip?.getAttribute("data-tooltip")).toContain("arithmetic mean of");
  });

  it("shows the quality-gate chip only when the gates meaningfully bite", async () => {
    renderPage();
    await waitForBoard();
    const gateChips = Array.from(document.querySelectorAll(".gate-chip"));
    // Fixture: Forever's multiplier 0.8026 → "gates −20%"; the ~0.9999 rows
    // show nothing (reduction ≤ 0.005 is noise, not a gate).
    expect(gateChips.some((chip) => chip.textContent === "gates −20%")).toBe(true);
    const rows = document.querySelectorAll("#rows tr[data-i]");
    expect(gateChips.length).toBeLessThan(rows.length);
  });
});
