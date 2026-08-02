import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mockBootParams } = vi.hoisted(() => ({ mockBootParams: new URLSearchParams() }));
vi.mock("../lib/config", () => ({ bootParams: mockBootParams }));

import type { PageName } from "../lib/router";
import {
  closeEntityRoute,
  currentPage,
  entityReturnUrl,
  entityRoute,
  initRouteListeners,
  navigateToPage,
  pushEntityRoute,
  syncFromLocation,
} from "./routeStore";

function setLocation(url: string): void {
  history.replaceState(null, "", url);
}

function fullUrl(): string {
  return location.pathname + location.search + location.hash;
}

beforeEach(() => {
  setLocation("/");
  entityReturnUrl.value = null;
  syncFromLocation();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("currentPage", () => {
  it("defaults to overview", () => {
    expect(currentPage()).toBe("overview");
  });

  it("follows a valid hash route", () => {
    setLocation("/#/operations");
    syncFromLocation();
    expect(currentPage()).toBe("operations");
  });

  it("resolves unknown '#/…' routes to overview and normalizes the URL", () => {
    setLocation("/#/bogus");
    syncFromLocation();
    expect(currentPage()).toBe("overview");
    expect(location.hash).toBe("#/overview");
  });

  it("normalizes '#/' the same way", () => {
    setLocation("/#/");
    syncFromLocation();
    expect(currentPage()).toBe("overview");
    expect(location.hash).toBe("#/overview");
  });

  it("keeps the config query while normalizing", () => {
    setLocation("/?api=x#/nope");
    syncFromLocation();
    expect(fullUrl()).toBe("/?api=x#/overview");
  });

  it("leaves non-route fragments (skip link) alone and keeps the page", () => {
    setLocation("/#/operations");
    syncFromLocation();
    setLocation("/#main-content");
    syncFromLocation();
    expect(currentPage()).toBe("operations");
    expect(location.hash).toBe("#main-content");
  });

  it("the hash page wins over an entity overlay param", () => {
    setLocation("/#/operations?agent=a1");
    syncFromLocation();
    expect(currentPage()).toBe("operations");
    // Entity routes own their hash and are left untouched.
    expect(location.hash).toBe("#/operations?agent=a1");
  });

  it("cold entity links land on their ENTITY_PAGES fallback", () => {
    setLocation("/#/agents/a1");
    syncFromLocation();
    expect(currentPage()).toBe("submissions");
    expect(location.hash).toBe("#/agents/a1");

    setLocation("/?miner=hk");
    syncFromLocation();
    expect(currentPage()).toBe("overview");

    setLocation("/agent/a1");
    syncFromLocation();
    expect(currentPage()).toBe("submissions");
  });
});

describe("entityRoute", () => {
  it("mirrors readEntityRoute", () => {
    expect(entityRoute()).toBeNull();
    setLocation("/#/overview?miner=hk");
    syncFromLocation();
    expect(entityRoute()).toMatchObject({ kind: "miner", id: "hk", key: "miner:hk" });
  });

  it("is referentially stable across syncs of the same URL", () => {
    setLocation("/#/overview?miner=hk");
    syncFromLocation();
    const first = entityRoute();
    syncFromLocation();
    expect(entityRoute()).toBe(first);
  });
});

describe("navigateToPage", () => {
  it("drops page-scoped view state on a cross-page move", () => {
    setLocation("/#/submissions?status=rejected&q=needle&page=2");
    syncFromLocation();
    navigateToPage("overview");
    expect(fullUrl()).toBe("/#/overview");
    expect(currentPage()).toBe("overview");
  });

  it("keeps page-scoped view state on a same-page move (overlay close)", () => {
    setLocation("/#/submissions?status=rejected&agent=a1");
    syncFromLocation();
    navigateToPage("submissions");
    expect(fullUrl()).toBe("/#/submissions?status=rejected");
    expect(entityRoute()).toBeNull();
  });

  it("no-ops (no history entry) when the URL is already the target", () => {
    setLocation("/#/submissions?status=rejected");
    syncFromLocation();
    const push = vi.spyOn(history, "pushState");
    navigateToPage("submissions");
    expect(push).not.toHaveBeenCalled();
    expect(fullUrl()).toBe("/#/submissions?status=rejected");
  });

  it("pushes (not replaces) a history entry and clears entityReturnUrl", () => {
    setLocation("/#/overview");
    syncFromLocation();
    entityReturnUrl.value = "/#/operations";
    const push = vi.spyOn(history, "pushState");
    navigateToPage("benchmark");
    expect(push).toHaveBeenCalledTimes(1);
    expect(push).toHaveBeenCalledWith({}, "", "/#/benchmark");
    expect(entityReturnUrl.value).toBeNull();
    expect(currentPage()).toBe("benchmark");
  });
});

describe("pushEntityRoute", () => {
  it("pushes the overlay URL, records the return URL, and tags history state", () => {
    setLocation("/#/operations");
    syncFromLocation();
    pushEntityRoute("validator", "v1");
    expect(fullUrl()).toBe("/#/operations?validator=v1");
    expect(entityReturnUrl.value).toBe("/#/operations");
    expect(history.state).toEqual({ entity: true });
    expect(entityRoute()).toMatchObject({ kind: "validator", id: "v1" });
  });

  it("keeps the page's activity filters under the overlay", () => {
    setLocation("/#/submissions?status=rejected");
    syncFromLocation();
    pushEntityRoute("agent", "a1");
    expect(fullUrl()).toBe("/#/submissions?status=rejected&agent=a1");
  });

  it("no-ops when the URL already addresses the entity", () => {
    setLocation("/#/operations");
    syncFromLocation();
    pushEntityRoute("validator", "v1");
    const push = vi.spyOn(history, "pushState");
    pushEntityRoute("validator", "v1");
    expect(push).not.toHaveBeenCalled();
    // The recorded return URL is not overwritten by the no-op.
    expect(entityReturnUrl.value).toBe("/#/operations");
  });
});

describe("closeEntityRoute", () => {
  it("replaces the URL with the page under the overlay, keeping its view state", () => {
    setLocation("/#/submissions?status=rejected&agent=a1");
    syncFromLocation();
    closeEntityRoute();
    expect(fullUrl()).toBe("/#/submissions?status=rejected");
    expect(entityRoute()).toBeNull();
    expect(currentPage()).toBe("submissions");
    expect(entityReturnUrl.value).toBeNull();
  });

  it("keeps the hash page for miner overlays too", () => {
    setLocation("/#/overview?miner=hk");
    syncFromLocation();
    closeEntityRoute();
    expect(fullUrl()).toBe("/#/overview");
  });

  it("goes back when the overlay minted a history entry", () => {
    setLocation("/#/operations");
    syncFromLocation();
    pushEntityRoute("agent", "a1");
    const back = vi.spyOn(history, "back").mockImplementation(() => {});
    const replace = vi.spyOn(history, "replaceState");
    closeEntityRoute();
    expect(back).toHaveBeenCalledTimes(1);
    expect(replace).not.toHaveBeenCalled();
    // The return URL is cleared by the popstate that follows, not here.
    expect(entityReturnUrl.value).toBe("/#/operations");
  });

  it("requires BOTH a return URL and history.state.entity to go back", () => {
    // A cold-loaded overlay URL has no {entity} history state: replace, not back.
    setLocation("/#/operations?validator=v1");
    syncFromLocation();
    entityReturnUrl.value = "/#/operations";
    const back = vi.spyOn(history, "back");
    closeEntityRoute();
    expect(back).not.toHaveBeenCalled();
    expect(fullUrl()).toBe("/#/operations");
    expect(entityReturnUrl.value).toBeNull();
  });

  it("falls back to ENTITY_PAGES when no page route is present", () => {
    setLocation("/?agent=a1");
    syncFromLocation();
    closeEntityRoute();
    expect(fullUrl()).toBe("/#/submissions");
  });

  it("is a no-op on dedicated entity pages", () => {
    setLocation("/agent/a1");
    syncFromLocation();
    entityReturnUrl.value = "/#/overview";
    const back = vi.spyOn(history, "back");
    const replace = vi.spyOn(history, "replaceState");
    closeEntityRoute();
    expect(back).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
    expect(fullUrl()).toBe("/agent/a1");
    expect(entityReturnUrl.value).toBe("/#/overview");
  });

  it("leaves screener row-target params in the URL (no overlay URL rewrite)", () => {
    setLocation("/#/operations?screener=s1");
    syncFromLocation();
    entityReturnUrl.value = "/#/operations";
    closeEntityRoute();
    expect(fullUrl()).toBe("/#/operations?screener=s1");
    expect(entityReturnUrl.value).toBeNull();
  });

  it("only clears the return URL when no entity is addressed", () => {
    setLocation("/#/overview");
    syncFromLocation();
    entityReturnUrl.value = "/#/operations";
    const replace = vi.spyOn(history, "replaceState");
    closeEntityRoute();
    expect(replace).not.toHaveBeenCalled();
    expect(entityReturnUrl.value).toBeNull();
  });
});

describe("initRouteListeners", () => {
  const popPages: PageName[] = [];
  let popCount = 0;

  it("wires hashchange to re-sync the signals", () => {
    initRouteListeners(() => {
      popCount += 1;
      popPages.push(currentPage());
    });
    setLocation("/#/reviews");
    window.dispatchEvent(new Event("hashchange"));
    expect(currentPage()).toBe("reviews");
  });

  it("syncs the signals before invoking the popstate callback", () => {
    popPages.length = 0;
    popCount = 0;
    setLocation("/#/benchmark");
    window.dispatchEvent(new Event("popstate"));
    expect(popCount).toBe(1);
    expect(popPages[0]).toBe("benchmark");
  });

  it("clears entityReturnUrl when popstate lands on an entity-less URL", () => {
    entityReturnUrl.value = "/#/operations";
    setLocation("/#/overview");
    window.dispatchEvent(new Event("popstate"));
    expect(entityReturnUrl.value).toBeNull();
  });

  it("keeps entityReturnUrl when popstate lands on an entity URL", () => {
    entityReturnUrl.value = "/#/operations";
    setLocation("/#/operations?validator=v1");
    window.dispatchEvent(new Event("popstate"));
    expect(entityReturnUrl.value).toBe("/#/operations");
    expect(entityRoute()).toMatchObject({ kind: "validator", id: "v1" });
  });

  it("installs the listeners only once", () => {
    const second = vi.fn();
    initRouteListeners(second);
    popCount = 0;
    setLocation("/#/operations");
    window.dispatchEvent(new Event("popstate"));
    expect(second).not.toHaveBeenCalled();
    expect(popCount).toBe(1);
  });
});
