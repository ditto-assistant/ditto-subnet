// Parity tests for the overview page port. Each block cites the inventory
// row (dashboard-refactor-notes/assert-inventory.md) it carries forward and
// keeps the old test's rationale as comments. Endpoint-path asserts moved to
// src/lib/api.test.ts; source-negative greps live in
// src/build-invariants.test.ts.
import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  THIRD_PARTY_HARNESSES,
  TIMELINE_MAX_ERAS,
  harnessMeasuredVersions,
  harnessUnmeasuredVersions,
  loadMemoryField,
  memoryTimelineHtml,
  resetMemoryFieldCache,
} from "../components/overview/memory-timeline";
import { resetBoardState, setLeaderboardVersionView } from "../components/board/board-state";
import { refreshAllEndpoints } from "../data/useEndpoint";
import { fx, fxScore, pct } from "../lib/format";
import { dethroneFloor, displayComposite, rankEntries } from "../lib/scoring";
import { syncFromLocation } from "../stores/routeStore";
import { fixtureNameFor, loadFixture } from "../test-fixtures";
import type { LeaderboardEntry, LeaderboardPayload } from "../types/leaderboard";
import type { TimelinePayload } from "../types/bench";
import { OverviewPage } from "./OverviewPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const overviewCss = readFileSync(join(HERE, "..", "styles", "pages", "overview.css"), "utf-8");
const cssNorm = overviewCss.replace(/\s+/g, " ");

const leaderboard = loadFixture<LeaderboardPayload>("leaderboard");
const ranked = rankEntries(leaderboard.entries ?? []);
const emissions = leaderboard.emissions ?? null;
const championEntry = ranked.find(
  (e) => String(e.agent_id) === String(emissions?.champion_agent_id),
) as LeaderboardEntry & { rank: number | null };

interface FetchOptions {
  fail?: boolean;
  onRequest?: (path: string) => void;
  patch?: (name: string, body: unknown, path: string) => unknown;
}

/** Fixture-backed fetch stub with per-test failure/patch/spy hooks. */
function installFetch(options: FetchOptions = {}): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://fixtures.test");
    const path = url.pathname.replace(/^.*?(?=\/public\/)/, "") + url.search;
    options.onRequest?.(path);
    if (options.fail) {
      return Promise.resolve(new Response("{}", { status: 503 }));
    }
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
  history.replaceState(null, "", "/#/overview");
  syncFromLocation();
  resetBoardState();
  setLeaderboardVersionView("current");
  resetMemoryFieldCache();
});

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
});

function renderOverview(options: FetchOptions = {}): void {
  restoreFetch = installFetch(options);
  render(() => <OverviewPage />);
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

// ── Masthead: the homepage's first reading line ──
// Who reigns, what state the subnet is in, when the next payout lands — one
// ruled band above the split, so none of it sits below the fold or behind
// the chart, and the clock appears once per screen.
describe("overview masthead", () => {
  it("leads the page with one band — champion, vitals ledger, payout clock — above the split", async () => {
    renderOverview();
    await waitForBoard();
    const section = document.querySelector('section.page[data-page="overview"]') as HTMLElement;
    const masthead = section.querySelector(".overview-masthead") as HTMLElement;
    expect(masthead).toBeTruthy();
    // The band is the page's first reading line; the two-pane split follows.
    expect(section.firstElementChild).toBe(masthead);
    expect(masthead.nextElementSibling?.classList.contains("overview-split")).toBe(true);
    // Three instruments, in reading order, inside the one frame.
    const cells = Array.from(masthead.children).map((el) => el.className.split(" ")[0]);
    expect(cells).toEqual(["champion-box", "snapshot", "overview-clock"]);
    // The clock is a second mount of the rail's instrument under its own id,
    // so the page never carries two #epoch-clock.
    expect(masthead.querySelector("#overview-epoch-clock.epoch-clock")).toBeTruthy();
    expect(document.querySelectorAll("#epoch-clock")).toHaveLength(0);
    expect(masthead.querySelector("#overview-epoch-clock")?.textContent).toContain(
      "Next payout in",
    );
    // The rail's copy folds away while this page is on at rail widths, so
    // the reading appears once per screen; the phone top bar keeps its own.
    const shellCss = readFileSync(join(HERE, "..", "styles", "shell.css"), "utf-8").replace(
      /\s+/g,
      " ",
    );
    expect(shellCss).toContain(
      '@media (min-width: 961px) { .layout:has(.page.active[data-page="overview"]) .sidebar > .epoch-clock { display: none; } }',
    );
    expect(cssNorm).toContain(
      "@media (max-width: 960px) { .overview-masthead > .overview-clock { display: none; } }",
    );
  });

  it("lays the vitals ledger out as three ruled rows: population, scores, machine", async () => {
    renderOverview();
    await waitForBoard();
    const lines = Array.from(document.querySelectorAll(".stat-ledger .ledger-line"));
    expect(lines.map((line) => line.getAttribute("data-ledger"))).toEqual([
      "population",
      "population",
      "population",
      "scores",
      "scores",
      "scores",
      "machine",
      "machine",
      "machine",
    ]);
    expect(lines.map((line) => line.querySelector("dd")?.id)).toEqual([
      "h-miners",
      "c-miners",
      "h-agents",
      "c-top",
      "c-median",
      "c-spread",
      "h-validators",
      "h-scores",
      "h-last",
    ]);
    expect(cssNorm).toContain(
      ".stat-ledger { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));",
    );
  });
});

// ── Row 3: test_overview_shows_the_full_board_without_a_disclosure ──
// "Standings are never hidden behind a click" — ditto-platform#383 collapsed
// the leaderboard table behind a <details>; that stays banned. The overview
// is a two-pane layout with a compact board; the full column set lives on the
// dedicated Leaderboard page ("compactness through a second surface, not
// through disclosure").
describe("overview board disclosure ban (row 3)", () => {
  it("pairs a context rail with a compact board and never wraps standings in a disclosure", async () => {
    renderOverview();
    await waitForBoard();
    const section = document.querySelector('section.page[data-page="overview"]') as HTMLElement;
    expect(section.querySelector(".overview-split")).toBeTruthy();
    expect(section.querySelector("aside.board-rail")).toBeNull();
    expect(section.querySelector("details.board-full")).toBeNull();
    // The board table is not inside any <details>.
    expect(document.querySelector("details #board")).toBeNull();
  });

  it("keeps the emissions/rollout strips and standing notices after the board", async () => {
    renderOverview();
    await waitForBoard();
    const board = document.querySelector('.board[tabindex="0"]') as HTMLElement;
    for (const id of ["emissions-strip", "rollout-strip", "leaderboard-notice"]) {
      const strip = el(id);
      expect(
        board.compareDocumentPosition(strip) & Node.DOCUMENT_POSITION_FOLLOWING,
        id + " follows the board",
      ).toBeTruthy();
    }
    expect(el("leaderboard-version-pills")).toBeTruthy();
  });

  it("keeps the compact leaderboard metrics sortable, with the emissions column tip", async () => {
    renderOverview();
    await waitForBoard();
    for (const key of ["rank", "composite", "cost", "latency", "first_seen"]) {
      expect(document.querySelector('th[data-sort="' + key + '"]'), key).toBeTruthy();
    }
    expect(el("emissions-col-tip")).toBeTruthy();
  });
});

// ── Row 1 (overview/leaderboard slice): test_includes_submission_pipeline ──
// The mega-spec's board half: family-ranked leaderboard (one lightweight rank
// per payment-owner family), registration strictness, emission-eligibility rank
// medals, provisional "P" ranks + quorum badges, the emissions strip as a
// polite live region, chain observation, and the version pills with the
// historical bench_version fetch. Pipeline/drawer literals live with the
// submissions/operations/reviews ports.
describe("overview leaderboard block (row 1 slice)", () => {
  it("ranks one representative and expands its minimal family children", async () => {
    renderOverview();
    await waitForBoard();
    // The family rule is stated on the table itself.
    expect(el("board").getAttribute("aria-label")).toBe(
      "Subnet 118 leaderboard: one ranked representative per payment-owner submission family",
    );
    const toggle = document.querySelector(".family-toggle") as HTMLButtonElement;
    const familyParentId = toggle.dataset.familyToggle as string;
    const child = document.querySelector(
      `tr[data-family-parent="${familyParentId}"]`,
    ) as HTMLTableRowElement;
    expect(child.hidden).toBe(true);
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(child.hidden).toBe(false);
    expect(child.querySelector(".family-member-name")).toBeTruthy();
    expect(child.querySelector(".family-member-score")).toBeTruthy();
    expect(document.querySelectorAll("#rows tr[data-i]")).toHaveLength(ranked.length);
  });

  it("medals the top three ranks only while emission-eligible (e.emission_eligible === true)", async () => {
    renderOverview();
    await waitForBoard();
    expect(document.querySelector(".rank.r1")).toBeTruthy();
    cleanup();
    restoreFetch?.();
    // Same board with the leader's eligibility revoked: no r1 medal.
    renderOverview({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        (payload.entries ?? []).forEach((entry) => {
          entry.emission_eligible = false;
        });
        return payload;
      },
    });
    await waitForBoard();
    // The shared store keeps last-good rows across mounts; wait for the
    // patched refetch to land before asserting on it.
    await waitFor(() => expect(document.querySelector(".rank.r1")).toBeNull());
  });

  it("treats registration as strict (=== true): a null read renders the unconfirmed badge", async () => {
    renderOverview({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const second = (payload.entries ?? [])[1] as LeaderboardEntry;
        second.registered = null;
        return payload;
      },
    });
    await waitForBoard();
    await waitFor(() => {
      const badges = Array.from(document.querySelectorAll("#rows .prov"));
      expect(badges.some((b) => b.textContent === "unconfirmed")).toBe(true);
    });
  });

  it("prefixes provisional ranks with P and shows the quorum badge", async () => {
    renderOverview({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = body as LeaderboardPayload;
        const first = (payload.entries ?? [])[0] as LeaderboardEntry;
        first.finalized = false;
        first.score_count = 2;
        return payload;
      },
    });
    await waitForBoard();
    // Scored is the default tab (a provisional run is pre-quorum feedback,
    // not a standing); the pre-quorum row is visible on the All view.
    fireEvent.click(document.querySelector('[data-board-tab="all"]') as HTMLElement);
    // The provisional tier keeps its own rank counter ("P" + e.rank).
    await waitFor(() => {
      const provRank = Array.from(document.querySelectorAll("#rows .rank")).find((node) =>
        /^P\d+$/.test(node.textContent || ""),
      );
      expect(provRank).toBeTruthy();
    });
    const quorum = document.querySelector(".quorum-badge") as HTMLElement;
    expect(quorum.textContent).toBe("2 of 3 · provisional");
  });

  it("renders the emissions strip as a polite live region with the fold's own numbers", async () => {
    renderOverview();
    await waitForBoard();
    const strip = el("emissions-strip");
    expect(strip).toHaveAttribute("role", "status");
    expect(strip).toHaveAttribute("aria-live", "polite");
    await waitFor(() => expect(el("emissions-title").textContent).toContain("KOTH champion"));
    // Champion share comes from the fold, never from copy.
    expect(el("emissions-title").textContent).toContain(
      "receives " + pct(emissions?.champion_share as number) + " of the miner pool.",
    );
    // The margin is read from emissions.margin ('" protection margin and the
    // " + method'), and the paired decision names the paired-seed band.
    expect(el("emissions-reason").textContent).toContain("must lead by more than");
    expect(el("emissions-reason").textContent).toContain(
      "protection margin and the paired-seed uncertainty band",
    );
  });

  it("explains active tie pooling from the fold instead of implying fixed slots", async () => {
    renderOverview({
      patch: (name, body) => {
        if (name !== "leaderboard") return body;
        const payload = structuredClone(body) as LeaderboardPayload;
        if (payload.emissions) payload.emissions.tie_weighting_active = true;
        return payload;
      },
    });
    await waitForBoard();

    await waitFor(() =>
      expect(el("emissions-reason").textContent).toContain(
        "pool only the ranked shares of the slots they occupy",
      ),
    );
    expect(el("emissions-reason").textContent).toContain(
      "Missing paired evidence cannot widen a group",
    );
  });

  it("summarizes the revealed chain weights (validator top choice · c/v)", async () => {
    renderOverview();
    await waitForBoard();
    await waitFor(() =>
      expect(el("chain-observation-copy").textContent).toContain("revealed miner-bearing vectors"),
    );
    const copy = el("chain-observation-copy").textContent || "";
    expect(copy).toContain("is the validator top choice in");
    expect(copy).toContain("Commit-reveal can make this lag active commitments");
    // The per-row chip vocabulary.
    const notes = Array.from(document.querySelectorAll(".chain-weight-note"));
    expect(notes.length).toBeGreaterThan(0);
    expect(
      notes.some(
        (n) =>
          (n.textContent || "").startsWith("Validator top choice · ") ||
          (n.textContent || "").startsWith("Validator support · "),
      ),
    ).toBe(true);
  });

  it("switches to a historical bench view via ?bench_version and says it does not drive weights", async () => {
    const paths: string[] = [];
    renderOverview({ onRequest: (path) => paths.push(path) });
    await waitForBoard();
    const pill = document.querySelector('[data-leaderboard-version="6"]') as HTMLElement;
    expect(pill).toBeTruthy();
    fireEvent.click(pill);
    await waitFor(() =>
      expect(paths.some((p) => p === "/public/leaderboard?bench_version=6")).toBe(true),
    );
    await waitFor(() => expect(el("leaderboard-title").textContent).toContain("history"));
    expect(el("leaderboard-hint").textContent).toBe(
      "historical scores only · does not drive current validator weights",
    );
    expect(el("leaderboard-version-context").textContent).toContain(
      "Current emissions remain on the rollout view.",
    );
    // Historical views carry no emissions projection; the champion box says so
    // instead of misstating the live subnet.
    expect(el("champion-body").textContent).toContain("archive");
  });

  it("shows the champion box for the reigning KOTH champion with fold-fed numbers", async () => {
    renderOverview();
    await waitForBoard();
    await waitFor(() =>
      expect(el("champion-body").textContent).toContain(championEntry.agent_name as string),
    );
    expect(el("champion-box-kicker").textContent).toContain("KOTH · reigning champion");
    const body = el("champion-body");
    expect(body.textContent).toContain("Raw rank #" + championEntry.rank);
    expect(body.textContent).toContain("Miner pool " + pct(0.65));
    expect(body.textContent).toContain(fx(displayComposite(championEntry)));
  });

  it("explains a held crown in the champion box and keeps the callout on the compact board", async () => {
    // The fixture's crown sits at raw #2 while raw #1 leads inside the band —
    // the standing that reliably reads as a bug ("why is #2 the champion?").
    // The overview must explain it in place: the strips are hidden here, so
    // the champion-box note and the callout above the board carry the rule.
    renderOverview();
    await waitForBoard();
    await waitFor(() => expect(el("champion-note").textContent).toBeTruthy());
    const note = el("champion-note");
    expect(note.textContent).toContain("Holds the crown from raw #" + championEntry.rank);
    expect(note.textContent).toContain("1 agent scores higher");
    expect(note.textContent).toContain("head-to-head");
    // The shared block's held-crown callout renders on this mount too, and
    // the overview's strip-hiding rules must never swallow it.
    await waitFor(() => expect(el("koth-standing").classList.contains("show")).toBe(true));
    expect(cssNorm).not.toContain("koth-standing");
    // The higher-scoring leaders dim on the compact board as well, with the
    // note allowed to wrap in the narrow emissions column.
    expect(document.querySelectorAll("tr.above-champion")).toHaveLength(
      (championEntry.rank as number) - 1,
    );
    expect(el("koth-standing").textContent).toContain(
      "Needed to take crown+" + fxScore(emissions?.raw_leader_decision?.required_lead as number),
    );
    expect(cssNorm).toContain(
      ".overview-main #board .above-champion-note { white-space: normal; }",
    );
  });

  it("fills the snapshot ledger from health + operations and drops the retired stats", async () => {
    renderOverview();
    await waitForBoard();
    await waitFor(() => expect(el("h-miners").textContent).toBe("294"));
    expect(el("h-scores").textContent).toBe("2556");
    expect(el("h-agents").textContent).toBe("628");
    await waitFor(() => expect(el("h-validators").textContent).toBe("6"));
    expect(el("c-miners").textContent).toBe("12");
    const comps = ranked.map((e) => displayComposite(e));
    expect(el("c-top").textContent).toContain(fx(comps[0] as number));
    expect(el("c-spread").textContent).toContain(fx((comps[0] as number) - (comps[1] as number)));
    const section = document.querySelector('[data-page="overview"]') as HTMLElement;
    expect(section.textContent).toContain("Total scores");
    expect(section.textContent).toContain("Validators");
    // Retired stat cards stay retired.
    expect(section.textContent).not.toContain("Scoring Spend");
    expect(section.textContent).not.toContain("Avg latency");
    expect(section.textContent).not.toContain("Scores · 24h");
  });
});

// ── Row 2: test_includes_off_network_harness_memory_comparison ──
// The memory timeline leads the overview (no longer at the bottom of the
// benchmark page); the harness filter and per-harness hardcoded evidence
// links were dropped; the kicker above the title was removed. Third-party
// evidence (run IDs, means, models, seed) is pinned verbatim.
describe("off-network harness comparison (row 2)", () => {
  it("leads the overview rail with the memory-timeline section", async () => {
    renderOverview();
    await waitForBoard();
    const rail = document.querySelector(".overview-rail") as HTMLElement;
    expect(rail.firstElementChild?.classList.contains("harness-comparison")).toBe(true);
    expect(el("harness-comparison-title").textContent).toBe("How far miners have taken memory");
    expect(document.querySelector("details.harness-comparison-method")).toBeTruthy();
    const section = document.querySelector(".harness-comparison") as HTMLElement;
    expect(section.textContent).toContain("Method and comparability caveats");
    expect(section.textContent).toContain(
      "Hermes Agent and OpenClaw measured retrospectively where reference runs are available",
    );
    // Dropped affordances stay dropped: no harness filter, no chart-row
    // layout, no "Reference only" kicker above the title.
    expect(document.getElementById("third-party-harness-filter")).toBeNull();
    expect(document.querySelector(".memory-chart-row")).toBeNull();
    expect(section.textContent).not.toContain("Reference only · no emissions");
  });

  it("pins the third-party evidence records verbatim", () => {
    const [hermes, openclaw] = THIRD_PARTY_HARNESSES;
    expect(hermes?.profile).toBe("Native SessionDB session_search");
    expect(hermes?.model).toBe("qwen/qwen3-32b");
    expect(hermes?.route).toBe("OpenRouter · Nebius pinned");
    expect(hermes?.seed).toBe("3058240546919425205");
    expect(openclaw?.subject).toBe("OpenClaw 2026.7.1");
    expect(openclaw?.profile).toBe("Native memory-core FTS · 20-result recall");
    const hermesV7 = hermes?.points.find((p) => p.benchVersion === 7);
    expect(hermesV7?.runId).toBe("34178537-0529-48d8-8421-8b7c566db2d4");
    expect(hermesV7?.memoryMean).toBe(0.13636363636363635);
    expect(hermesV7?.model).toBe("openai/gpt-oss-20b");
    expect(hermesV7?.route).toBe("OpenRouter · aggregate throughput");
    const hermesV8 = hermes?.points.find((p) => p.benchVersion === 8);
    expect(hermesV8?.runId).toBe("0bce82c0-e1da-42b8-8b25-d3f47b13f117");
    expect(hermesV8?.memoryMean).toBe(0.029880478087649404);
    expect(hermesV8?.memoryCorrect).toBe(5);
    expect(hermesV8?.memoryCases).toBe(251);
    expect(hermesV8?.seed).toBe("123456789");
    expect(hermesV8?.datasetSha256).toBe(
      "6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98",
    );
    const openclawV7 = openclaw?.points.find((p) => p.benchVersion === 7);
    expect(openclawV7?.runId).toBe("dd651606-bcfd-4ed8-83ae-926a0a19ee6b");
    expect(openclawV7?.memoryMean).toBe(0.22601010101010102);
    const openclawV8 = openclaw?.points.find((p) => p.benchVersion === 8);
    expect(openclawV8?.runId).toBe("d3ddbb28-1240-46a5-b851-560582657f08");
    expect(openclawV8?.memoryMean).toBe(0.40039840637450197);
    expect(openclawV8?.memoryCorrect).toBe(98);
    expect(openclawV8?.memoryCases).toBe(251);
    expect(openclawV8?.seed).toBe("123456789");
    expect(openclawV8?.datasetSha256).toBe(
      "6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98",
    );
  });

  it("derives the evidence links from the records instead of hardcoding them", async () => {
    renderOverview();
    await waitFor(() => {
      expect(el("harness-comparison-evidence").querySelectorAll("a").length).toBe(
        THIRD_PARTY_HARNESSES.length,
      );
    });
    const links = Array.from(el("harness-comparison-evidence").querySelectorAll("a"));
    links.forEach((link, index) => {
      const evidence = THIRD_PARTY_HARNESSES[index];
      expect(link.getAttribute("href")).toBe(evidence?.evidenceUrl);
      expect(link.textContent).toBe(evidence?.label + " evidence ↗");
    });
    const method = el("harness-comparison-method").textContent || "";
    expect(method).toContain("Their points are positioned in each immutable contract's band");
    expect(method).toContain("v4 corrects v3 false positives");
    expect(method).toContain(
      "Third-party harnesses never enter score rank, KOTH, validator weights, or payouts.",
    );
  });
});

// ── Row 5: test_memory_timeline_plots_the_field_and_crowns_the_champion ──
// The field comes from the existing per-version leaderboard; settled
// contracts are immutable so only the newest board refetches; the champion
// plate is the point of the chart and its label lives in a reserved gutter;
// the reveal animation must enhance an already-visible default.
describe("memory timeline field + champion (row 5)", () => {
  it("plots every finalized run as a field dot and crowns the reigning champion", async () => {
    renderOverview();
    await waitFor(
      () => {
        expect(document.querySelectorAll(".timeline-field").length).toBeGreaterThan(0);
        expect(document.querySelector(".timeline-champion-plate")).toBeTruthy();
      },
      { timeout: 4000 },
    );
    expect(document.querySelector(".timeline-champion-halo")).toBeTruthy();
    const plate = document.querySelector(".timeline-champion-plate") as SVGGElement;
    // Champion identity comes from the live emissions fold's hotkey.
    expect(plate.getAttribute("aria-label")).toContain(championEntry.agent_name as string);
    // The plate rect sits in the annotation gutter above the plot (top=34,
    // plateY = top - plateH - 5 = 8 on the landscape branch).
    const rect = plate.querySelector("rect") as SVGRectElement;
    expect(Number(rect.getAttribute("y"))).toBeLessThan(34);
  });

  it("fetches each contract's board once and refetches only the newest", async () => {
    const paths: string[] = [];
    restoreFetch = installFetch({ onRequest: (p) => paths.push(p) });
    await loadMemoryField([6, 7], 7);
    await loadMemoryField([6, 7], 7);
    const v6 = paths.filter((p) => p === "/public/leaderboard?bench_version=6").length;
    const v7 = paths.filter((p) => p === "/public/leaderboard?bench_version=7").length;
    // Settled contracts are immutable: fetched once, kept forever. Only the
    // newest board is allowed to refetch.
    expect(v6).toBe(1);
    expect(v7).toBe(2);
  });

  it("keeps the rendered graph mounted when unchanged data is refetched", async () => {
    const paths: string[] = [];
    renderOverview({ onRequest: (path) => paths.push(path) });
    await waitFor(
      () => {
        expect(document.querySelectorAll(".timeline-field").length).toBeGreaterThan(0);
      },
      { timeout: 4000 },
    );
    const svg = document.querySelector(".memory-timeline-svg");
    const latestBoard = "/public/leaderboard?bench_version=7";
    const before = paths.filter((path) => path === latestBoard).length;

    refreshAllEndpoints();

    await waitFor(() => {
      expect(paths.filter((path) => path === latestBoard).length).toBeGreaterThan(before);
    });
    expect(document.querySelector(".memory-timeline-svg")).toBe(svg);
  });

  it("gives every contract an equal band and says so in the reading notes", async () => {
    renderOverview();
    await waitFor(() => expect(document.querySelector(".memory-timeline-svg")).toBeTruthy(), {
      timeout: 4000,
    });
    const section = document.querySelector(".harness-comparison") as HTMLElement;
    expect(section.textContent).toContain("Each contract gets an equal band, not equal clock time");
    // Era labels are the equal-band x anchors: consecutive centers are evenly
    // spaced.
    const labels = Array.from(document.querySelectorAll(".timeline-era-label")).map((n) =>
      Number(n.getAttribute("x")),
    );
    expect(labels.length).toBeGreaterThan(2);
    const step = (labels[1] as number) - (labels[0] as number);
    for (let i = 2; i < labels.length; i += 1) {
      expect(Math.abs((labels[i] as number) - (labels[i - 1] as number) - step)).toBeLessThan(0.1);
    }
  });

  it("keeps the reveal animation as an enhancement over a visible resting state", () => {
    // Entrances use `both` fill (finished chart = resting state) and the only
    // looping motion is removed under reduced motion.
    expect(cssNorm).toContain("@keyframes timeline-dot-in { from { opacity: 0;");
    expect(cssNorm).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{ \.timeline-champion-pulse \{ display: none; \} \}/,
    );
  });
});

// ── Row 6: test_memory_timeline_names_the_gaps_instead_of_implying_data ──
// "A contract can outrun both the reference runs and its own rollout.
// Neither may be papered over" — a reference line that just stops reads as a
// baseline collapsing to zero, and an open-rollout band drawn like a settled
// one implies an immovable rank. Both states are derived from data (harness
// records + /public/bench/rollout) so later contracts inherit the treatment.
describe("memory timeline gaps (row 6)", () => {
  const releases = [2, 3, 4, 5, 6, 7, 8, 9].map((v) => ({
    bench_version: v,
    released_at: "2026-07-0" + Math.min(9, v) + "T00:00:00Z",
    activated_at: null,
    title: "Contract v" + v,
  }));
  const timeline: TimelinePayload = {
    releases,
    points: [
      {
        recorded_at: "2026-07-10T00:00:00Z",
        bench_version: 7,
        agent_id: "a",
        agent_name: "alpha",
        memory_mean: 0.5,
      },
    ],
  };

  function renderGapChart(): string {
    const out = memoryTimelineHtml(timeline, {
      width: 960,
      phoneViewport: false,
      rollout: { active_version: 8, desired_version: 9, status: "collecting" },
      championHotkey: null,
      fieldByVersion: {},
      pendingByVersion: { 9: 2 },
    });
    if (out.kind !== "chart") throw new Error("expected a chart");
    return out.html;
  }

  it("derives v8 coverage and the v9 gap from harness records, never version literals", () => {
    const shown: Record<number, boolean> = { 7: true, 8: true, 9: true };
    for (const evidence of THIRD_PARTY_HARNESSES) {
      expect(harnessMeasuredVersions(evidence)).toContain(8);
      expect(harnessUnmeasuredVersions(evidence, shown)).toEqual([9]);
    }
  });

  it("extends both reference lines through v8 and names the omitted v9 measurements", () => {
    const html = renderGapChart();
    expect(html).toContain('class="timeline-unmeasured"');
    expect(html).not.toContain("· not on v8");
    expect(html).toContain("· not on v9");
    expect(html).toContain("not yet measured");
    expect(html).toContain("No reference harness has been run on v9");
  });

  it("labels a run of adjacent unmeasured contracts once, centred on the run", () => {
    // Two neighbouring gap bands each narrower than the label used to draw
    // the same words on top of themselves; one label per run instead.
    const out = memoryTimelineHtml(
      {
        releases: releases.concat([
          {
            bench_version: 10,
            released_at: "2026-07-10T00:00:00Z",
            activated_at: null,
            title: "Contract v10",
          },
        ]),
        points: timeline.points,
      },
      {
        width: 620,
        phoneViewport: false,
        rollout: { active_version: 9, desired_version: 10, status: "collecting" },
        championHotkey: null,
        fieldByVersion: {},
        pendingByVersion: {},
      },
    );
    if (out.kind !== "chart") throw new Error("expected a chart");
    expect(out.html.match(/class="timeline-unmeasured"/g)).toHaveLength(1);
    expect(out.html).toContain("no run on v9, v10");
  });

  it("marks the collecting rollout's band open with the rollout strip's vocabulary", () => {
    const html = renderGapChart();
    expect(html).toContain('class="timeline-band open"');
    expect(html).toContain("v9 collecting");
    expect(html).toContain("rollout still collecting");
    expect(html).toContain("The v9 rollout is still collecting");
    // Scored-but-pre-quorum runs are counted, never plotted.
    expect(html).toContain("awaiting quorum");
    expect(html).toContain("a rank here can never move retroactively");
  });

  it("does not mark a settled band open once the rollout activates", () => {
    const out = memoryTimelineHtml(timeline, {
      width: 960,
      phoneViewport: false,
      rollout: { active_version: 8, desired_version: 8, status: "activated" },
      championHotkey: null,
      fieldByVersion: {},
      pendingByVersion: {},
    });
    if (out.kind !== "chart") throw new Error("expected a chart");
    expect(out.html).not.toContain('class="timeline-band open"');
  });

  it("tints the open band with the rollout strip's own accent", () => {
    expect(cssNorm).toContain(".timeline-band.open { fill: color-mix(in oklch, var(--accent-2)");
  });
});

// ── Row 7: test_memory_timeline_window_is_not_pinned_to_a_bench_version ──
// The bands are whatever the timeline endpoint returns, windowed by count —
// "No version literal decides what is drawn." (Version-literal negative
// greps live in src/build-invariants.test.ts.)
describe("memory timeline window (row 7)", () => {
  it("windows to the most recent TIMELINE_MAX_ERAS (6) releases at or above v2", () => {
    expect(TIMELINE_MAX_ERAS).toBe(6);
    const releases = [1, 2, 3, 4, 5, 6, 7, 8].map((v) => ({
      bench_version: v,
      released_at: "2026-07-0" + Math.min(9, v) + "T00:00:00Z",
      activated_at: null,
      title: "Contract v" + v,
    }));
    const out = memoryTimelineHtml(
      { releases, points: [] },
      {
        width: 960,
        phoneViewport: false,
        rollout: null,
        championHotkey: null,
        fieldByVersion: {},
        pendingByVersion: {},
      },
    );
    if (out.kind !== "chart") throw new Error("expected a chart");
    // v1 is below the >= 2 floor; v2 falls out of the six-band window.
    const drawn = Array.from(out.html.matchAll(/timeline-era-label[^>]*>v(\d+)</g)).map((m) =>
      Number(m[1]),
    );
    expect(drawn).toEqual([3, 4, 5, 6, 7, 8]);
  });
});

// ── Row 8: test_api_failures_do_not_render_sample_data ──
// API failure renders explicit unavailable states, never demo/sample data.
// (The `var SAMPLE` negative greps live in src/build-invariants.test.ts.)
describe("API failure states (row 8)", () => {
  it("states the leaderboard absence outright and dashes the snapshot", async () => {
    renderOverview({ fail: true });
    await waitFor(() => {
      expect(el("rows").textContent).toContain(
        "Could not load live leaderboard data. Try refreshing in a moment.",
      );
    });
    await waitFor(() => {
      expect(el("leaderboard-hint").textContent).toBe("Live standings are temporarily unavailable");
    });
    for (const id of ["c-miners", "c-top", "c-median", "c-spread"]) {
      expect(el(id).textContent).toBe("–");
    }
    await waitFor(() => expect(el("h-miners").textContent).toBe("–"));
    for (const id of ["h-agents", "h-scores", "h-last"]) {
      expect(el(id).textContent).toBe("–");
    }
    expect(el("emissions-strip").classList.contains("show")).toBe(false);
    expect(el("chain-observation").classList.contains("show")).toBe(false);
  });

  it("names the timeline absence instead of substituting a chart", async () => {
    renderOverview({ fail: true });
    await waitFor(() => {
      expect(el("harness-comparison-chart").textContent).toContain(
        "Benchmark release history is temporarily unavailable. No substitute timeline is shown.",
      );
    });
    expect(el("harness-comparison-chart").classList.contains("harness-comparison-state")).toBe(
      true,
    );
  });
});

// ── Row 36 helper coverage: the dethrone floor renders from lib/scoring ──
// (Full strip markup asserts live in Leaderboard.test.tsx; this guards the
// overview mount uses the same additive fold math via lib/scoring.)
describe("dethrone floor source of truth", () => {
  it("publishes the additive floor computed by lib/scoring", async () => {
    renderOverview();
    await waitForBoard();
    const floor = dethroneFloor(emissions, championEntry);
    expect(floor).not.toBeNull();
    await waitFor(() =>
      expect(el("emissions-threshold").textContent).toContain(
        fx((floor as { floor: number }).floor),
      ),
    );
  });
});
