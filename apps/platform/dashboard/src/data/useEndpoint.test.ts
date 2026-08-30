// An agent deep link is an entity-first surface. Pause the global board,
// fleet, search-corpus, and timeline reads while its card is open; they are
// unrelated to the answer the reader is waiting for. Closing the card hydrates
// the dashboard once.
import { createRoot } from "solid-js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { syncFromLocation } from "../stores/routeStore";
import { FIXTURE_TOP_AGENT_ID } from "../test-fixtures";
import { agentCardOpen, hydrateOnAgentCardClose, useEndpoint } from "./useEndpoint";

const POLL_MS = 1000;

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.useFakeTimers();
  history.replaceState(null, "", "/#/overview");
  syncFromLocation();
  fetchSpy = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(
      () =>
        Promise.resolve(
          new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
        ) as ReturnType<typeof fetch>,
    );
});

afterEach(() => {
  fetchSpy.mockRestore();
  vi.useRealTimers();
  history.replaceState(null, "", "/#/overview");
  syncFromLocation();
});

function visit(url: string): void {
  history.replaceState(null, "", url);
  syncFromLocation();
}

function reads(): number {
  return fetchSpy.mock.calls.filter((call: unknown[]) =>
    String(call[0]).includes("/public/weights"),
  ).length;
}

describe("agentCardOpen", () => {
  it("is true for an agent route in either form, and for nothing else", () => {
    expect(agentCardOpen()).toBe(false);
    visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
    expect(agentCardOpen()).toBe(true);
    visit("/agent/" + FIXTURE_TOP_AGENT_ID);
    expect(agentCardOpen()).toBe(true);
    // A miner or validator drilldown is not an entity-first surface: those
    // bodies render FROM the global payloads, so their reads must keep going.
    visit("/#/overview?miner=5Ehotkey");
    expect(agentCardOpen()).toBe(false);
    visit("/#/operations?validator=5Vhotkey");
    expect(agentCardOpen()).toBe(false);
  });
});

describe("polled endpoints while an agent card is open", () => {
  it("stops polling while the card is open and resumes when it closes", async () => {
    await createRoot(async (dispose) => {
      useEndpoint("/public/weights", { pollMs: POLL_MS });
      await vi.advanceTimersByTimeAsync(0);
      const initial = reads();
      expect(initial).toBe(1);

      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(reads()).toBe(initial + 1);

      visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
      await vi.advanceTimersByTimeAsync(POLL_MS * 4);
      // Four ticks passed and not one read fired.
      expect(reads()).toBe(initial + 1);

      visit("/#/submissions");
      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(reads()).toBe(initial + 2);
      dispose();
    });
  });

  it("still serves a manual refresh while the card is open", async () => {
    await createRoot(async (dispose) => {
      const endpoint = useEndpoint("/public/weights", { pollMs: POLL_MS });
      await vi.advanceTimersByTimeAsync(0);
      visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
      const before = reads();
      endpoint.refresh();
      await vi.advanceTimersByTimeAsync(0);
      // The pause is about unattended polling; a reader who asks gets an answer.
      expect(reads()).toBe(before + 1);
      dispose();
    });
  });
});

describe("hydrateOnAgentCardClose", () => {
  /** Register the watcher and let its first effect settle, as mounting does. */
  function watch(hydrate: () => void): () => void {
    return createRoot((dispose) => {
      hydrateOnAgentCardClose(hydrate);
      return dispose;
    });
  }

  it("hydrates exactly once when the card closes", () => {
    const hydrate = vi.fn();
    const dispose = watch(hydrate);
    expect(hydrate).not.toHaveBeenCalled();

    visit("/#/submissions?agent=" + FIXTURE_TOP_AGENT_ID);
    expect(hydrate).not.toHaveBeenCalled();

    visit("/#/submissions");
    expect(hydrate).toHaveBeenCalledTimes(1);

    // A later unrelated route change is not a second hydrate.
    visit("/#/overview");
    expect(hydrate).toHaveBeenCalledTimes(1);
    dispose();
  });

  it("does not hydrate when no card was ever open", () => {
    const hydrate = vi.fn();
    const dispose = watch(hydrate);
    visit("/#/operations?validator=5Vhotkey");
    visit("/#/leaderboard");
    expect(hydrate).not.toHaveBeenCalled();
    dispose();
  });
});
