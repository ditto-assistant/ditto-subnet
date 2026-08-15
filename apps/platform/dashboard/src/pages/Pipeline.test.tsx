// Parity tests for the submission-pipeline page (the pipeline half of the
// old operations page: monolith markup 2742–2862, the #623/#635 board
// reshape, and the last-reconciled-snapshot rule). Rendered against the
// recorded fixtures (frozen clock 2026-07-31T14:00Z, the golden renderer's
// instant); the fleet half lives in Operations.test.tsx.
import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { refreshAllEndpoints } from "../data/useEndpoint";
import { syncFromLocation } from "../stores/routeStore";
import { installFixtureFetch } from "../test-fixtures";
import { PipelinePage } from "./PipelinePage";

let restoreFetch: (() => void) | null = null;
let fetchSpy: ReturnType<typeof vi.spyOn> | null = null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/pipeline");
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

function text(id: string): string {
  return document.getElementById(id)?.textContent ?? "";
}

async function renderPage(): Promise<void> {
  render(() => <PipelinePage />);
  await waitFor(() =>
    expect(text("operations-snapshot")).toContain("Pipeline and fleet reconciled"),
  );
}

describe("pipeline board from the shared snapshot", () => {
  it("consumes exactly one /public/operations fetch for every panel", async () => {
    await renderPage();
    const opsCalls = fetchedPaths().filter((u) => u.includes("/public/operations"));
    expect(opsCalls.length).toBe(1);
    // Banned per-panel endpoints from the pre-snapshot era.
    expect(fetchedPaths().some((u) => u.includes("/public/validators"))).toBe(false);
    expect(fetchedPaths().some((u) => u.includes("/public/activity?page=1&limit=200"))).toBe(false);
  });

  it("stamps the shared snapshot note with reconciliation + age", async () => {
    await renderPage();
    const note = document.getElementById("operations-snapshot");
    expect(note).toHaveAttribute("aria-live", "polite");
    expect(note?.textContent).toBe(
      "Pipeline and fleet reconciled · recent history shown; full history in Activity · 2h ago",
    );
  });

  it("renders the four stage columns with authoritative counts", async () => {
    await renderPage();
    const stages = Array.from(
      document.querySelectorAll("#pipeline-overview .pipeline-column"),
      (col) => col.getAttribute("data-pipeline-stage"),
    );
    expect(stages).toEqual(["admission", "waiting_validator", "evaluating", "scored"]);
    expect(text("pipeline-scored-count")).toContain("628");
    expect(document.querySelectorAll("#pipeline-scored .pipeline-item").length).toBe(50);
    expect(document.querySelector("#pipeline-scored .pipeline-more")?.textContent).toBe(
      "578 older submissions in Activity",
    );
    // Newest score first.
    expect(
      document.querySelector("#pipeline-scored .pipeline-item .pipeline-item-name")?.textContent,
    ).toBe("blackhole_v8");
  });

  it("separates the stuck backlog behind its quick-filter", async () => {
    await renderPage();
    // The three waiting entries all exhausted their retry budget: the count
    // stays authoritative while the actionable lane reads empty.
    const count = document.getElementById("pipeline-wait-validator-count") as HTMLElement;
    expect(count.textContent).toContain("3");
    const stuck = count.querySelector("[data-pipeline-stuck-filter]") as HTMLButtonElement;
    expect(stuck.textContent).toBe("3 stuck");
    expect(stuck).toHaveAttribute("aria-pressed", "false");
    expect(document.querySelector("#pipeline-wait-validator .pipeline-empty")?.textContent).toBe(
      "No submissions waiting.",
    );

    fireEvent.click(stuck);
    await waitFor(() =>
      expect(document.querySelectorAll("#pipeline-wait-validator .pipeline-item").length).toBe(3),
    );
    const chips = document.querySelectorAll("#pipeline-wait-validator .retry-chip.exhausted");
    expect(chips.length).toBe(3);
    expect(chips[0]?.textContent).toBe("Stuck · needs operator");
  });

  it("hides the rescreen notice when no policy rescreen is queued", async () => {
    await renderPage();
    expect((document.getElementById("rescreen-notice") as HTMLElement).hidden).toBe(true);
  });
});

// ── Weekend drift: renamed lanes, the integrity-review branch, and the
// last-reconciled-snapshot rule (Python guards
// test_operations_refresh_keeps_last_successful_snapshot and the #623/#635
// board reshape) ─────────────────────────────────────────────────────────────
describe("weekend drift: board reshape and refresh resilience", () => {
  it("renders the mechanical-admission lane names and the atlas explainer", async () => {
    const { container } = render(() => <PipelinePage />);
    await waitFor(() => {
      expect(container.querySelector("#pipeline-admission-title")?.textContent).toBe(
        "Build & admission",
      );
    });
    expect(container.querySelectorAll("#pipeline-overview .pipeline-column")).toHaveLength(4);
    expect(container.querySelector("#pipeline-wait-validator-title")?.textContent).toBe(
      "Waiting for validators",
    );
    expect(container.querySelector("#pipeline-evaluating-title")?.textContent).toBe("Scoring");
    expect(container.querySelector("#pipeline-scored-title")?.textContent).toBe("Scored & live");
    expect(container.textContent).toContain(
      "Mechanical admission builds a verified image before validators.",
    );
  });

  it("shows the conditional integrity-review branch with the authoritative count", async () => {
    const { container } = render(() => <PipelinePage />);
    await waitFor(() => {
      expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("53");
    });
    const aside = container.querySelector(".pipeline-review-branch");
    expect(aside?.querySelector(".pipeline-review-eyebrow")?.textContent).toBe(
      "Conditional after scoring",
    );
    expect(aside?.querySelector("#pipeline-review-title")?.textContent).toBe(
      "Source integrity review",
    );
    // Only qualifiers and anomaly holds enter — the copy says so, and the
    // fixture window carries none, so the branch states that rather than
    // implying review of everything.
    expect(aside?.textContent).toContain("Only leaderboard qualifiers and robust anomaly holds");
    expect(container.querySelector("#pipeline-review-items")?.textContent).toContain(
      "No submissions are held for integrity review.",
    );
  });

  it("keeps the last reconciled snapshot when a refresh fails", async () => {
    const { container } = render(() => <PipelinePage />);
    await waitFor(() => {
      expect(text("operations-snapshot")).toContain("Pipeline and fleet reconciled");
    });
    expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("53");

    // Every subsequent operations fetch fails; the board must NOT blank.
    restoreFetch?.();
    restoreFetch = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    refreshAllEndpoints();
    await waitFor(() => {
      expect(text("operations-snapshot")).toContain(
        "Refresh delayed · showing last reconciled snapshot",
      );
    });
    // A refresh failure does not invalidate the last reconciled snapshot:
    // the board keeps rendering it while polls retry.
    expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("53");
    expect(text("operations-snapshot")).toContain("2h ago");
  });

  it("cold-start failure still renders the unavailable placeholders", async () => {
    restoreFetch?.();
    restoreFetch = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    const { container } = render(() => <PipelinePage />);
    await waitFor(() => {
      expect(text("operations-snapshot")).toBe("Shared operations snapshot unavailable");
    });
    expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("–");
    expect(container.querySelector("#pipeline-review-items")?.textContent).toContain(
      "Review state unavailable.",
    );
  });
});
