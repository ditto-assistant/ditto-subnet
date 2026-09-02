import { beforeEach, describe, expect, it, vi } from "vitest";

// The router reads config knobs from the boot-time snapshot in lib/config;
// stub it with a mutable bag so tests control the boot params.
const { mockBootParams } = vi.hoisted(() => ({ mockBootParams: new URLSearchParams() }));
vi.mock("./config", () => ({ bootParams: mockBootParams }));

import {
  ENTITY_PAGES,
  ENTITY_PARAMS,
  ENTITY_PATHS,
  PAGES,
  PAGE_SCOPED_PARAMS,
  clearEntityParams,
  configSearch,
  currentPageName,
  dashboardHref,
  entityHref,
  fullEntityHref,
  operationsHref,
  operationsPathname,
  operationsViewFromPathname,
  pageFromPathname,
  parseHashRoute,
  readEntityRoute,
  spaHref,
} from "./router";

function setLocation(url: string): void {
  history.replaceState(null, "", url);
}

function setBoot(qs: string): void {
  for (const key of Array.from(mockBootParams.keys())) mockBootParams.delete(key);
  new URLSearchParams(qs).forEach((value, key) => {
    mockBootParams.append(key, value);
  });
}

beforeEach(() => {
  setBoot("");
  setLocation("/");
});

describe("PAGES registry", () => {
  it("keeps page titles and wayfinding subtitles explicit", () => {
    expect(PAGES.overview).toEqual({
      title: "Overview",
      sub: "Subnet snapshot and the full leaderboard · ranked by composite",
    });
    expect(PAGES.leaderboard).toEqual({
      title: "Leaderboard",
      sub: "Every scored miner · canonical ranks · sortable by any metric",
    });
    expect(PAGES.pipeline).toEqual({
      title: "Submission pipeline",
      sub: "Every submission from upload to scored · admission, validation, and integrity review",
    });
    expect(PAGES.operations).toEqual({
      title: "Fleet",
      sub: "Validator and screener capacity · live slot work · trusted miner-image builds",
    });
    expect(PAGES.submissions).toEqual({
      title: "Recent submissions",
      sub: "Screening evidence and validator quorum progress · select a row for history",
    });
    expect(PAGES.reviews).toEqual({
      title: "Miner sign-in",
      sub: "Sign in with your hotkey · manage your profile, submissions, and MCP",
    });
    expect(PAGES.ath).toEqual({
      title: "ATH reviews",
      sub: "Public queue of held high-score submissions · scores preserved, emissions paused",
    });
    expect(PAGES.benchmark).toEqual({
      title: "Benchmark",
      sub: "What the scoring benchmark measures and the frozen scoring setup",
    });
  });

  it("exposes the entity maps and page-scoped params from the original", () => {
    expect(ENTITY_PATHS).toEqual({
      agent: "agents",
      miner: "miners",
      validator: "validators",
      screener: "screeners",
    });
    expect(ENTITY_PARAMS).toEqual({
      agents: "agent",
      miners: "miner",
      validators: "validator",
      screeners: "screener",
    });
    expect(ENTITY_PAGES.agents).toBe("submissions");
    expect(ENTITY_PAGES.miners).toBe("overview");
    expect(ENTITY_PAGES.validators).toBe("operations");
    expect(ENTITY_PAGES.screeners).toBe("operations");
    // Singular aliases so EntityRoute.kind can index directly.
    expect(ENTITY_PAGES.agent).toBe("submissions");
    expect(ENTITY_PAGES.miner).toBe("overview");
    expect(ENTITY_PAGES.validator).toBe("operations");
    expect(ENTITY_PAGES.screener).toBe("operations");
    expect(PAGE_SCOPED_PARAMS).toEqual([
      "status",
      "downloadable",
      "q",
      "page",
      "code",
      "login",
      "complete",
    ]);
  });
});

describe("parseHashRoute", () => {
  it("returns a null page for an empty hash", () => {
    const route = parseHashRoute("");
    expect(route.page).toBeNull();
    expect(route.query.toString()).toBe("");
  });

  it("returns a null page for a non-route fragment", () => {
    expect(parseHashRoute("#main-content").page).toBeNull();
  });

  it("parses a bare page route", () => {
    const route = parseHashRoute("#/overview");
    expect(route.page).toBe("overview");
    expect(route.query.toString()).toBe("");
  });

  it("parses page + query", () => {
    const route = parseHashRoute("#/submissions?status=rejected&page=2");
    expect(route.page).toBe("submissions");
    expect(route.query.get("status")).toBe("rejected");
    expect(route.query.get("page")).toBe("2");
  });

  it("parses '#/' as an empty page name", () => {
    const route = parseHashRoute("#/");
    expect(route.page).toBe("");
    expect(route.query.toString()).toBe("");
  });

  it("parses a trailing '?' as an empty query", () => {
    const route = parseHashRoute("#/reviews?");
    expect(route.page).toBe("reviews");
    expect(route.query.toString()).toBe("");
  });

  it("defaults to location.hash", () => {
    setLocation("/#/operations?agent=a1");
    const route = parseHashRoute();
    expect(route.page).toBe("operations");
    expect(route.query.get("agent")).toBe("a1");
  });
});

describe("configSearch", () => {
  it("is empty without boot config knobs", () => {
    expect(configSearch()).toBe("");
  });

  it("carries ?api and ?wandb from the boot snapshot", () => {
    setBoot("api=/api/v2&wandb=https://wandb.ai/x");
    expect(configSearch()).toBe("?api=%2Fapi%2Fv2&wandb=https%3A%2F%2Fwandb.ai%2Fx");
  });

  it("always orders api before wandb", () => {
    setBoot("wandb=w&api=a");
    expect(configSearch()).toBe("?api=a&wandb=w");
  });

  it("never carries stray boot query junk forward", () => {
    setBoot("utm_source=x&api=a&agent=a1");
    expect(configSearch()).toBe("?api=a");
  });

  it("reads the boot snapshot, not the live location.search", () => {
    setBoot("api=boot");
    setLocation("/?api=live&junk=1#/overview");
    expect(configSearch()).toBe("?api=boot");
  });
});

describe("spaHref", () => {
  it("mints overview as /", () => {
    expect(spaHref("overview")).toBe("/");
  });

  it("puts other pages in the pathname", () => {
    expect(spaHref("operations")).toBe("/operations");
    expect(spaHref("leaderboard")).toBe("/leaderboard");
  });

  it("preserves a recognized operations child route", () => {
    setLocation("/operations/screeners");
    expect(spaHref("operations")).toBe("/operations/screeners");
    setLocation("/operations/not-a-view");
    expect(spaHref("operations")).toBe("/operations");
  });

  it("appends overlay and filter params to the real query", () => {
    expect(spaHref("submissions", new URLSearchParams("status=rejected&page=2"))).toBe(
      "/submissions?status=rejected&page=2",
    );
  });

  it("omits the '?' for an empty query object", () => {
    expect(spaHref("overview", new URLSearchParams())).toBe("/");
  });

  it("re-applies boot config knobs and drops stray search junk", () => {
    setBoot("api=x");
    setLocation("/agents/a1?utm=1#/whatever?noise=1");
    expect(spaHref("overview")).toBe("/?api=x");
    expect(spaHref("operations")).toBe("/operations?api=x");
    expect(spaHref("operations", new URLSearchParams("validator=v1"))).toBe(
      "/operations?api=x&validator=v1",
    );
  });
});

describe("operations routes", () => {
  it("keeps /operations as validators and accepts named child routes", () => {
    expect(operationsViewFromPathname("/operations")).toBe("validators");
    expect(operationsViewFromPathname("/operations/validators")).toBe("validators");
    expect(operationsViewFromPathname("/operations/screeners/")).toBe("screeners");
    expect(operationsViewFromPathname("/operations/builds")).toBe("builds");
    expect(operationsViewFromPathname("/operations/nope")).toBe("validators");
  });

  it("mints explicit child URLs for tab navigation", () => {
    expect(operationsPathname("validators")).toBe("/operations/validators");
    expect(operationsHref("screeners", new URLSearchParams("screener=s1"))).toBe(
      "/operations/screeners?screener=s1",
    );
  });
});

describe("currentPageName", () => {
  it("returns the page for a valid hash route", () => {
    setLocation("/#/reviews");
    expect(currentPageName()).toBe("reviews");
  });

  it("returns null for an unknown page", () => {
    setLocation("/#/bogus");
    expect(currentPageName()).toBeNull();
  });

  it("returns null for '#/' and for no hash", () => {
    setLocation("/#/");
    expect(currentPageName()).toBeNull();
    setLocation("/");
    expect(currentPageName()).toBeNull();
  });

  it("returns null on a dedicated entity page", () => {
    setLocation("/agent/a1");
    expect(currentPageName()).toBeNull();
  });

  it("returns the page for a crawlable pathname", () => {
    setLocation("/leaderboard");
    expect(currentPageName()).toBe("leaderboard");
    expect(pageFromPathname()).toBe("leaderboard");
  });

  it("prefers the pathname over a leftover hash page", () => {
    setLocation("/leaderboard#/benchmark");
    expect(currentPageName()).toBe("leaderboard");
  });
});

describe("handle profile path", () => {
  it("treats /h/{stem} as a full miner profile route", () => {
    setLocation("/h/jupiter");
    expect(readEntityRoute()).toEqual({
      kind: "miner",
      id: "jupiter",
      key: "miner:jupiter",
      full: true,
      legacy: false,
    });
  });
});

describe("entityHref", () => {
  it("keeps the current page and appends the entity param", () => {
    setLocation("/#/operations");
    expect(entityHref("agent", "a1")).toBe("/operations?agent=a1");
  });

  it("preserves the rest of the page query (activity filters)", () => {
    setLocation("/#/submissions?status=rejected&page=2");
    expect(entityHref("agent", "a1")).toBe("/submissions?status=rejected&page=2&agent=a1");
  });

  it("replaces any other open entity param (one entity at a time)", () => {
    setLocation("/#/overview?miner=hk1");
    expect(entityHref("agent", "a1")).toBe("/?agent=a1");
  });

  it("falls back to ENTITY_PAGES for cold links with no page route", () => {
    setLocation("/");
    expect(entityHref("agent", "a1")).toBe("/submissions?agent=a1");
    expect(entityHref("miner", "hk")).toBe("/?miner=hk");
    expect(entityHref("validator", "v1")).toBe("/operations?validator=v1");
    expect(entityHref("screener", "s1")).toBe("/operations?screener=s1");
  });

  it("falls back to ENTITY_PAGES on a dedicated entity page", () => {
    setLocation("/agent/a1");
    expect(entityHref("agent", "a1")).toBe("/submissions?agent=a1");
  });

  it("honors an explicit page argument", () => {
    setLocation("/#/overview");
    expect(entityHref("validator", "v1", "operations")).toBe("/operations?validator=v1");
  });

  it("carries the boot config knobs in the real query", () => {
    setBoot("api=x");
    setLocation("/?api=x#/operations");
    expect(entityHref("miner", "hk")).toBe("/operations?api=x&miner=hk");
  });
});

describe("fullEntityHref", () => {
  it("uses the singular path segment with an encoded id", () => {
    expect(fullEntityHref("agent", "a 1")).toBe("/agent/a%201");
    expect(fullEntityHref("miner", "hk1")).toBe("/miner/hk1");
  });

  it("carries only the config knobs", () => {
    setBoot("api=x&wandb=y");
    setLocation("/#/submissions?status=rejected");
    expect(fullEntityHref("agent", "a1")).toBe("/agent/a1?api=x&wandb=y");
  });
});

describe("dashboardHref", () => {
  it("keeps page-scoped view state on same-page navigation", () => {
    setLocation("/#/submissions?status=rejected&q=needle&page=3");
    expect(dashboardHref("submissions")).toBe("/submissions?status=rejected&q=needle&page=3");
  });

  it("drops page-scoped view state on cross-page navigation", () => {
    setLocation("/#/submissions?status=rejected&q=needle&page=3");
    expect(dashboardHref("overview")).toBe("/");
  });

  it("clears entity params even on same-page navigation (overlay close)", () => {
    setLocation("/#/submissions?agent=a1&status=rejected");
    expect(dashboardHref("submissions")).toBe("/submissions?status=rejected");
  });

  it("drops the leaderboard pager page when leaving overview", () => {
    setLocation("/#/overview?page=4");
    expect(dashboardHref("submissions")).toBe("/submissions");
  });

  it("treats a dedicated entity page as cross-page (no page route present)", () => {
    setLocation("/agent/a1");
    expect(dashboardHref("submissions")).toBe("/submissions");
  });
});

describe("clearEntityParams", () => {
  it("removes all four entity params and nothing else", () => {
    const query = new URLSearchParams("agent=a&miner=m&validator=v&screener=s&status=rejected");
    clearEntityParams(query);
    expect(query.toString()).toBe("status=rejected");
  });
});

describe("readEntityRoute", () => {
  it("returns null when the URL addresses no entity", () => {
    setLocation("/");
    expect(readEntityRoute()).toBeNull();
    setLocation("/#/overview?page=2");
    expect(readEntityRoute()).toBeNull();
    setLocation("/#main-content");
    expect(readEntityRoute()).toBeNull();
  });

  describe("form 1: full path /agent/{id} or /miner/{id}", () => {
    it("resolves with full=true and legacy=false", () => {
      setLocation("/agent/a1");
      expect(readEntityRoute()).toEqual({
        kind: "agent",
        id: "a1",
        key: "agent:a1",
        full: true,
        legacy: false,
      });
      setLocation("/miner/hk1/");
      expect(readEntityRoute()).toEqual({
        kind: "miner",
        id: "hk1",
        key: "miner:hk1",
        full: true,
        legacy: false,
      });
    });

    it("decodes the id, including encoded slashes", () => {
      setLocation("/agent/a%201");
      expect(readEntityRoute()?.id).toBe("a 1");
      setLocation("/agent/a%2Fb");
      expect(readEntityRoute()?.id).toBe("a/b");
    });

    it("returns null on malformed percent-encoding instead of throwing", () => {
      setLocation("/agent/%E0%A4%A");
      expect(readEntityRoute()).toBeNull();
    });

    it("does not match deeper paths or other kinds", () => {
      setLocation("/agent/a1/b");
      expect(readEntityRoute()).toBeNull();
      // Full-page routes exist only for agents and miners.
      setLocation("/validator/v1");
      expect(readEntityRoute()).toBeNull();
    });
  });

  describe("form 2: leftover entity param in the hash query", () => {
    it("resolves and marks legacy so the router can flatten it", () => {
      setLocation("/#/operations?agent=a1");
      expect(readEntityRoute()).toEqual({
        kind: "agent",
        id: "a1",
        key: "agent:a1",
        full: false,
        legacy: true,
      });
    });

    it("keeps working with other hash state present", () => {
      setLocation("/#/submissions?status=rejected&miner=hk&page=2");
      expect(readEntityRoute()).toMatchObject({ kind: "miner", id: "hk", legacy: true });
    });

    it("prefers agent over miner over validator over screener", () => {
      setLocation("/#/overview?miner=hk&agent=a1");
      expect(readEntityRoute()?.kind).toBe("agent");
      setLocation("/#/overview?screener=s1&validator=v1");
      expect(readEntityRoute()?.kind).toBe("validator");
    });

    it("a present-but-empty first param blocks instead of falling through", () => {
      setLocation("/#/overview?agent=&miner=hk");
      expect(readEntityRoute()).toBeNull();
    });
  });

  describe("form 3: entity param in the real query (canonical)", () => {
    it("still flattens when a leftover hash page is present", () => {
      setLocation("/?agent=a1#/submissions");
      expect(readEntityRoute()).toEqual({
        kind: "agent",
        id: "a1",
        key: "agent:a1",
        full: false,
        legacy: true,
      });
    });

    it("is canonical without any hash", () => {
      setLocation("/operations?validator=v1");
      expect(readEntityRoute()).toMatchObject({
        kind: "validator",
        id: "v1",
        legacy: false,
      });
    });

    it("a present-but-empty first param blocks here too", () => {
      setLocation("/?agent=&miner=hk");
      expect(readEntityRoute()).toBeNull();
    });
  });

  describe("form 4: legacy hash #/agents/{id}", () => {
    it("resolves each plural kind to its singular, legacy=true", () => {
      setLocation("/#/agents/a1");
      expect(readEntityRoute()).toEqual({
        kind: "agent",
        id: "a1",
        key: "agent:a1",
        full: false,
        legacy: true,
      });
      setLocation("/#/validators/v1/");
      expect(readEntityRoute()).toMatchObject({ kind: "validator", id: "v1", legacy: true });
    });

    it("decodes the id and rejects malformed encoding", () => {
      setLocation("/#/agents/a%201");
      expect(readEntityRoute()?.id).toBe("a 1");
      setLocation("/#/agents/%2");
      expect(readEntityRoute()).toBeNull();
    });

    it("does not match once a query string follows the id", () => {
      setLocation("/#/agents/a1?x=1");
      expect(readEntityRoute()).toBeNull();
    });
  });

  describe("form 5: legacy plural path /agents/{id}", () => {
    it("resolves with legacy=true", () => {
      setLocation("/agents/a1");
      expect(readEntityRoute()).toEqual({
        kind: "agent",
        id: "a1",
        key: "agent:a1",
        full: false,
        legacy: true,
      });
      setLocation("/screeners/s1/");
      expect(readEntityRoute()).toMatchObject({
        kind: "screener",
        id: "s1",
        key: "screener:s1",
        legacy: true,
      });
    });
  });

  describe("precedence across forms", () => {
    it("full path (1) beats hash query (2)", () => {
      setLocation("/agent/a1#/overview?miner=hk");
      expect(readEntityRoute()).toMatchObject({ kind: "agent", id: "a1", full: true });
    });

    it("full path (1) beats real query (3)", () => {
      setLocation("/miner/hk1?agent=a1");
      expect(readEntityRoute()).toMatchObject({ kind: "miner", id: "hk1", full: true });
    });

    it("hash query overlays real query while flattening", () => {
      setLocation("/?miner=hk#/overview?agent=a1");
      expect(readEntityRoute()).toMatchObject({ kind: "agent", id: "a1", legacy: true });
    });

    it("real query (3) beats legacy hash (4)", () => {
      setLocation("/?agent=x#/agents/y");
      expect(readEntityRoute()).toMatchObject({ kind: "agent", id: "x", legacy: true });
    });

    it("legacy hash (4) beats legacy path (5)", () => {
      setLocation("/agents/p1#/miners/h1");
      expect(readEntityRoute()).toMatchObject({ kind: "miner", id: "h1", legacy: true });
    });
  });
});
