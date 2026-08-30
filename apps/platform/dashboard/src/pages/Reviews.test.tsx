import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { athReviewSnapshot, athSnapshotLabel } from "../components/reviews/ath";
import { REFRESH_MS } from "../lib/config";
import { syncFromLocation } from "../stores/routeStore";
import { installFixtureFetch, loadFixture } from "../test-fixtures";
import type { AthSnapshot } from "../types/pipeline";
import { AthPage } from "./AthPage";

const athFixture = loadFixture<AthSnapshot>("activity-ath");
const firstEntry = (athFixture.entries ?? [])[0] as NonNullable<
  NonNullable<AthSnapshot["entries"]>[number]
>;

let restoreFetch: (() => void) | null = null;
let fetchSpy: ReturnType<typeof vi.spyOn> | null = null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/ath");
  syncFromLocation();
  restoreFetch = installFixtureFetch();
  fetchSpy = vi.spyOn(globalThis, "fetch");
});

afterEach(() => {
  cleanup();
  fetchSpy?.mockRestore();
  fetchSpy = null;
  restoreFetch?.();
  restoreFetch = null;
  vi.useRealTimers();
});

function fetchedPaths(): string[] {
  return (fetchSpy?.mock.calls ?? []).map((call: unknown[]) => String(call[0]));
}

/** Serve custom ATH snapshots per page number (other paths 404). */
function stubAthFetch(responder: (page: number) => AthSnapshot): void {
  // Unwind the spy first: mockRestore reinstates the fetch it wrapped.
  fetchSpy?.mockRestore();
  restoreFetch?.();
  restoreFetch = null;
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://fixtures.test");
    if (!url.pathname.endsWith("/public/activity")) {
      return Promise.resolve(new Response("{}", { status: 404 }));
    }
    return Promise.resolve(
      new Response(JSON.stringify(responder(Number(url.searchParams.get("page")) || 1)), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  }) as typeof fetch;
  fetchSpy = vi.spyOn(globalThis, "fetch");
}

async function renderPage(): Promise<void> {
  render(() => <AthPage />);
  await waitFor(() => expect(document.getElementById("ath-count")?.textContent).toBe("50"));
}

// ── Row 9: the public queue, its explainer, and the honest states ───────────
describe("public miner-facing ATH review queue (row 9)", () => {
  it("routes as its own page and explains holds without discarding work", async () => {
    await renderPage();
    expect(document.querySelector('section.page[data-page="ath"]')).toBeTruthy();
    const text = document.body.textContent ?? "";
    expect(text).toContain("High scores get a second look.");
    expect(text).toContain("Recorded scores stay preserved");
    expect(text).toContain("emission eligibility pauses");
    // The three outcomes, each with its consequence.
    expect(text).toContain("A clear restores eligibility");
    expect(text).toContain("The submission closes with a public status update.");
    expect(text).toContain("A fresh evaluation replaces the held result");
  });

  it("fans out over the public activity endpoint only", async () => {
    await renderPage();
    const paths = fetchedPaths();
    expect(
      paths.some((path) =>
        path.includes("/public/activity?review=ath&status=under_review&limit=200&page=1"),
      ),
    ).toBe(true);
    // Public data only: no admin endpoint, no credential on any request.
    expect(paths.every((path) => path.includes("/public/"))).toBe(true);
    expect(paths.some((path) => path.includes("/admin/"))).toBe(false);
    for (const call of fetchSpy?.mock.calls ?? []) {
      const init = call[1] as RequestInit | undefined;
      const headers = (init?.headers ?? {}) as Record<string, string>;
      expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain("authorization");
    }
    expect(document.body.textContent).not.toContain("Authorization");
  });

  it("summarizes the queue and labels the snapshot's age honestly", async () => {
    await renderPage();
    expect(document.getElementById("ath-count")?.textContent).toBe("50");
    expect(document.getElementById("ath-oldest")?.textContent).toBe("14d ago");
    expect(document.getElementById("ath-scores")?.textContent).toBe("93");
    // Fixture generated_at is ~2h before the frozen clock: older than two
    // refresh ticks, so the label says cached — never fresh-as-live.
    const snapshot = document.getElementById("ath-snapshot") as HTMLElement;
    expect(snapshot.textContent).toBe("Cached snapshot · 2h ago");
    expect(snapshot.classList.contains("stale")).toBe(true);
    expect(snapshot).toHaveAttribute("role", "status");
    // The combined loading/empty state is hidden while entries render.
    const state = document.getElementById("ath-review-state") as HTMLElement;
    expect(state.hidden).toBe(true);
    expect(document.getElementById("ath-review-list")).toHaveAttribute("aria-live", "polite");
  });

  it("renders each held card with anchors, copy controls, and preserved scores", async () => {
    await renderPage();
    const card = document.querySelector(".ath-review-card") as HTMLElement;
    expect(card).toHaveAttribute("aria-label", "ATH review for silica, Submission v1");
    // Agent + miner identities are entity anchors with copy controls.
    const agentAnchor = card.querySelector('[data-entity-link="agent"]');
    expect(agentAnchor).toHaveTextContent("silica, Submission v1");
    expect(card.querySelector(".ath-id-line code")?.textContent).toBe(firstEntry.agent_id);
    expect(card.querySelector('.ath-id-line .copy[data-copy-label="agent ID"]')).toHaveAttribute(
      "data-key",
      firstEntry.agent_id,
    );
    const minerCopy = card.querySelector('.ath-hotkey .copy[data-copy-label="miner hotkey"]');
    expect(minerCopy).toHaveAttribute("data-key", firstEntry.miner_hotkey);
    expect(card.querySelector('[data-entity-link="miner"]')).toBeTruthy();
    // Submitted / Held (review_opened_at) grid, preserved composite, count.
    expect(card.textContent).toContain("Submitted");
    expect(card.textContent).toContain("Held");
    expect(card.textContent).toContain("ATH review pending");
    const scores = card.querySelector(".ath-score") as HTMLElement;
    expect(scores.textContent).toContain("Scores recorded");
    expect(scores.querySelectorAll("strong")[0]?.textContent).toBe(String(firstEntry.score_count));
    expect(scores.querySelectorAll("strong")[1]?.textContent).toBe(
      Number(firstEntry.preserved_composite).toFixed(3),
    );
    // #622: the hold reason is the CURRENT operator reason.
    const hold = card.querySelector(".ath-hold-reason") as HTMLElement;
    expect(hold.querySelector("b")?.textContent).toBe("Current operator reason");
    expect(hold.textContent).toContain(String(firstEntry.review_reason));
  });

  it("stitches a deep queue through bounded fan-out pagination", async () => {
    const entry = (overrides: Record<string, unknown>): Record<string, unknown> => ({
      ...firstEntry,
      ...overrides,
    });
    stubAthFetch((page) =>
      page === 1
        ? {
            entries: [entry({ agent_id: "p1-a" }), entry({ agent_id: "p1-b" })] as never,
            total_pages: 3,
            generated_at: "2026-07-31T13:59:30Z",
          }
        : {
            entries: [entry({ agent_id: "p" + page })] as never,
            total_pages: 3,
            generated_at: "2026-07-31T13:59:30Z",
          },
    );
    render(() => <AthPage />);
    await waitFor(() => expect(document.getElementById("ath-count")?.textContent).toBe("4"));
    const pages = fetchedPaths()
      .filter((path) => path.includes("review=ath"))
      .map((path) => new URLSearchParams(path.split("?")[1] ?? "").get("page"));
    expect(pages).toEqual(["1", "2", "3"]);
    expect(document.querySelectorAll(".ath-review-card").length).toBe(4);
    // A fresh stitch (generated seconds ago) is labeled as the public
    // snapshot, not cached.
    expect(document.getElementById("ath-snapshot")?.textContent).toBe("Public snapshot · 30s ago");
  });

  it("shows empty and failed states in honest words — no example data", async () => {
    stubAthFetch(() => ({ entries: [], total_pages: 1, generated_at: "2026-07-31T13:59:59Z" }));
    render(() => <AthPage />);
    await waitFor(() =>
      expect(document.getElementById("ath-review-state")?.textContent).toContain(
        "No active ATH reviews.",
      ),
    );
    expect((document.getElementById("ath-review-state") as HTMLElement).hidden).toBe(false);
    cleanup();

    // Cold failure: no cached snapshot to fall back on.
    fetchSpy?.mockRestore();
    globalThis.fetch = (() => Promise.reject(new Error("down"))) as typeof fetch;
    fetchSpy = vi.spyOn(globalThis, "fetch");
    render(() => <AthPage />);
    await waitFor(() =>
      expect(document.getElementById("ath-review-state")?.textContent).toContain(
        "Could not load active reviews.",
      ),
    );
    const state = document.getElementById("ath-review-state") as HTMLElement;
    expect(state.classList.contains("error")).toBe(true);
    expect(state.textContent).toContain("No example or private data is shown.");
    expect(document.getElementById("ath-count")?.textContent).toBe("–");
    expect(document.getElementById("ath-snapshot")?.textContent).toBe(
      "Public review snapshot unavailable",
    );
  });

  it("labels a failed refresh as the last public snapshot (unit)", () => {
    const snapshot: AthSnapshot = { entries: [], generated_at: "2026-07-31T13:59:30Z" };
    const now = Date.now();
    expect(athSnapshotLabel(snapshot, false, now)).toEqual({
      text: "Public snapshot · 30s ago",
      stale: false,
    });
    // Older than two refresh ticks: cached, stale.
    const old = { ...snapshot, generated_at: new Date(now - REFRESH_MS * 2 - 1000).toISOString() };
    expect(athSnapshotLabel(old, false, now).text).toContain("Cached snapshot · ");
    expect(athSnapshotLabel(old, false, now).stale).toBe(true);
    // A failed refresh keeps the cache visible but says so.
    expect(athSnapshotLabel(snapshot, true, now)).toEqual({
      text: "Refresh failed · showing last public snapshot",
      stale: true,
    });
  });

  it("builds the fan-out from page 1's total_pages (unit)", async () => {
    stubAthFetch((page) => ({
      entries: [{ agent_id: "unit-p" + page }] as never,
      total_pages: 2,
      generated_at: "2026-07-31T13:59:59Z",
    }));
    const snapshot = await athReviewSnapshot();
    expect(snapshot.entries?.map((e) => e.agent_id)).toEqual(["unit-p1", "unit-p2"]);
    expect(snapshot.count).toBe(2);
    expect(snapshot.total).toBe(2);
  });
});

// ── Weekend drift #622: current review reason, initial hold as history ──────
// An operator can revise the hold reason or reopen a review. The card's
// status names the event, the lead reason is the CURRENT one, and the
// initial hold reason stays visible as labeled history when it differs.
describe("current ATH review reason (#622)", () => {
  it("names a reopened review and keeps the initial hold reason as history", async () => {
    stubAthFetch(() => ({
      entries: [
        {
          ...firstEntry,
          review_event: "reopened",
          review_reason: "manual re-check of tool-call provenance",
          review_original_reason: "content near-duplicate of agent abc",
        },
      ] as never,
      total_pages: 1,
      generated_at: "2026-07-31T13:59:59Z",
    }));
    render(() => <AthPage />);
    await waitFor(() => expect(document.querySelector(".ath-review-card")).toBeTruthy());
    const card = document.querySelector(".ath-review-card") as HTMLElement;
    expect(card.querySelector(".ath-review-status")?.textContent).toBe("ATH review reopened");
    const holds = Array.from(card.querySelectorAll(".ath-hold-reason"), (hold) => [
      hold.querySelector("b")?.textContent,
      hold.querySelector("span")?.textContent,
    ]);
    expect(holds).toEqual([
      ["Current operator reason", "manual re-check of tool-call provenance"],
      ["Initial hold reason", "content near-duplicate of agent abc"],
    ]);
  });

  it("shows no history block when the reason never changed", async () => {
    stubAthFetch(() => ({
      entries: [
        {
          ...firstEntry,
          review_event: "opened",
          review_reason: "held for review",
          review_original_reason: "held for review",
        },
      ] as never,
      total_pages: 1,
      generated_at: "2026-07-31T13:59:59Z",
    }));
    render(() => <AthPage />);
    await waitFor(() => expect(document.querySelector(".ath-review-card")).toBeTruthy());
    const card = document.querySelector(".ath-review-card") as HTMLElement;
    expect(card.querySelector(".ath-review-status")?.textContent).toBe("ATH review pending");
    expect(card.querySelectorAll(".ath-hold-reason").length).toBe(1);
    expect(card.textContent).not.toContain("Initial hold reason");
  });
});
