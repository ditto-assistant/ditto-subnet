import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The pages paint from async reads, so every assertion here is about the gap
// between "the history entry was restored" and "the rows finally exist".

let docHeight = 0;
let scrolls: number[] = [];
let store: Record<string, string> = {};

/** jsdom implements neither scrollTo nor a settable scrollHeight, and this
 * runner has no Web Storage at all — stand all three up explicitly. */
function installEnvironment(): void {
  docHeight = 800;
  scrolls = [];
  store = {};
  Object.defineProperty(document.documentElement, "scrollHeight", {
    configurable: true,
    get: () => docHeight,
  });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 800, writable: true });
  Object.defineProperty(window, "scrollY", { configurable: true, value: 0, writable: true });
  Object.defineProperty(window, "sessionStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => {
        store[key] = value;
      },
      removeItem: (key: string) => {
        delete store[key];
      },
    },
  });
  window.scrollTo = ((x: number, y: number) => {
    scrolls.push(y);
    Object.defineProperty(window, "scrollY", { configurable: true, value: y, writable: true });
  }) as typeof window.scrollTo;
}

function seed(positions: Record<string, number>): void {
  store["ditto.scroll.v1"] = JSON.stringify(positions);
}

function saved(): Record<string, number> {
  return JSON.parse(store["ditto.scroll.v1"] ?? "{}") as Record<string, number>;
}

function at(y: number): void {
  Object.defineProperty(window, "scrollY", { configurable: true, value: y, writable: true });
}

/** Module state is a per-document singleton; every case gets a fresh copy. */
async function loadScroll(): Promise<typeof import("./scroll")> {
  vi.resetModules();
  return import("./scroll");
}

beforeEach(() => {
  vi.useFakeTimers();
  installEnvironment();
  history.replaceState(null, "", "/board");
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("installScrollMemory", () => {
  it("takes scroll restoration off the browser", async () => {
    const { installScrollMemory } = await loadScroll();
    history.scrollRestoration = "auto";
    const teardown = installScrollMemory();
    expect(history.scrollRestoration).toBe("manual");
    teardown();
  });

  it("waits for the document to grow before applying a saved offset", async () => {
    seed({ "/board": 1200 });
    const { installScrollMemory } = await loadScroll();
    const teardown = installScrollMemory();

    // The rows have not arrived: the offset is unreachable and nothing moves.
    expect(scrolls).toEqual([]);
    vi.advanceTimersByTime(500);
    expect(scrolls).toEqual([]);

    docHeight = 5000;
    vi.advanceTimersByTime(100);
    expect(scrolls).toEqual([1200]);
    teardown();
  });

  it("gives up at the deadline without landing past the saved offset", async () => {
    seed({ "/board": 4000 });
    const { installScrollMemory } = await loadScroll();
    const teardown = installScrollMemory();

    docHeight = 2000; // reachable 1200 — still short of the 4000 asked for
    vi.advanceTimersByTime(5000);
    expect(scrolls).toEqual([1200]);
    teardown();
  });

  it("abandons a pending restore once the reader scrolls for themselves", async () => {
    seed({ "/board": 1200 });
    const { installScrollMemory } = await loadScroll();
    const teardown = installScrollMemory();

    window.dispatchEvent(new Event("wheel"));
    docHeight = 5000;
    vi.advanceTimersByTime(5000);
    expect(scrolls).toEqual([]);
    teardown();
  });

  it("lands at the top for an entry with nothing remembered", async () => {
    const { installScrollMemory } = await loadScroll();
    const teardown = installScrollMemory();
    expect(scrolls).toEqual([0]);
    teardown();
  });

  it("restores the entry a popstate lands on", async () => {
    seed({ "/board": 1500, "/fleet": 0 });
    const { installScrollMemory } = await loadScroll();
    history.replaceState(null, "", "/fleet");
    const teardown = installScrollMemory();
    expect(scrolls).toEqual([0]);

    docHeight = 5000;
    history.replaceState(null, "", "/board");
    window.dispatchEvent(new PopStateEvent("popstate"));
    vi.advanceTimersByTime(100);
    expect(scrolls).toEqual([0, 1500]);
    teardown();
  });

  it("does not let the offsets seen on the way to a restore overwrite it", async () => {
    seed({ "/board": 1500 });
    const { installScrollMemory } = await loadScroll();
    const teardown = installScrollMemory();

    // A short document scrolls itself while it settles; that is not a reader
    // choosing position 0, and it must not clobber the target.
    at(0);
    window.dispatchEvent(new Event("scroll"));
    vi.advanceTimersByTime(200);
    expect(saved()["/board"]).toBe(1500);

    docHeight = 5000;
    vi.advanceTimersByTime(100);
    expect(scrolls).toEqual([1500]);
    teardown();
  });
});

describe("rememberScroll", () => {
  it("files the live offset under the current entry", async () => {
    const { installScrollMemory, rememberScroll } = await loadScroll();
    const teardown = installScrollMemory();
    at(940);
    rememberScroll();
    expect(saved()["/board"]).toBe(940);
    teardown();
  });
});

describe("scrollToTop", () => {
  it("lands at the top and forgets the entry's earlier offset", async () => {
    seed({ "/board": 1500 });
    const { installScrollMemory, scrollToTop, scrollRestoreOwnsEntry } = await loadScroll();
    docHeight = 5000;
    const teardown = installScrollMemory();
    vi.advanceTimersByTime(100);
    expect(scrollRestoreOwnsEntry()).toBe(true);

    scrollToTop();
    expect(scrolls.at(-1)).toBe(0);
    expect(saved()["/board"]).toBeUndefined();
    // A fresh navigation is the reader's position now, so nothing that resets
    // scroll on open has to defer to this module any more.
    expect(scrollRestoreOwnsEntry()).toBe(false);
    teardown();
  });
});

describe("scrollRestoreOwnsEntry", () => {
  it("is false for an entry with no remembered offset", async () => {
    const { installScrollMemory, scrollRestoreOwnsEntry } = await loadScroll();
    const teardown = installScrollMemory();
    expect(scrollRestoreOwnsEntry()).toBe(false);
    teardown();
  });
});
