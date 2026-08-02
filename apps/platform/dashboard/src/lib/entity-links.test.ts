// Parity tests for entity link construction (assert-inventory row 30,
// test_dashboard_entities_use_query_popovers_and_pages). From the original's
// inline comments: entity params live in the hash query (the real query
// carries config knobs only); drilldowns are overlays over the current page;
// ENTITY_PAGES is only the cold-link fallback; legacy real-query and
// path-style entity links are recognized and normalized (the recognition
// itself lives in router.readEntityRoute — these tests close the loop by
// asserting the links minted here resolve back to the same entity).

import { beforeEach, describe, expect, it, vi } from "vitest";

// The router reads config knobs from the boot-time snapshot in lib/config;
// stub it with a mutable bag so tests control the boot params.
const { mockBootParams } = vi.hoisted(() => ({ mockBootParams: new URLSearchParams() }));
vi.mock("./config", () => ({ bootParams: mockBootParams }));

import {
  canonicalEntityUrl,
  entityActions,
  entityAnchorAttrs,
  entityBackHref,
  hasFullEntityPage,
} from "./entity-links";
import { parseHashRoute, readEntityRoute } from "./router";

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

describe("entityAnchorAttrs", () => {
  it("builds the entity-link anchor contract", () => {
    setLocation("/#/submissions");
    const attrs = entityAnchorAttrs("agent", "agent-1", "My agent");
    expect(attrs).toEqual({
      class: "entity-link",
      href: "/#/submissions?agent=agent-1",
      "data-entity-link": "agent",
      label: "My agent",
    });
  });

  it("keeps the page's hash state so opening an overlay never resets it", () => {
    setLocation("/#/submissions?status=rejected&q=probe&page=3");
    const attrs = entityAnchorAttrs("agent", "agent-1");
    const route = parseHashRoute(new URL(attrs?.href ?? "", location.href).hash);
    expect(route.page).toBe("submissions");
    expect(route.query.get("status")).toBe("rejected");
    expect(route.query.get("q")).toBe("probe");
    expect(route.query.get("page")).toBe("3");
    expect(route.query.get("agent")).toBe("agent-1");
  });

  it("replaces any other entity param instead of stacking overlays", () => {
    setLocation("/#/operations?validator=5Val");
    const attrs = entityAnchorAttrs("screener", "5Scr");
    const route = parseHashRoute(new URL(attrs?.href ?? "", location.href).hash);
    expect(route.query.get("validator")).toBeNull();
    expect(route.query.get("screener")).toBe("5Scr");
  });

  it("falls back to ENTITY_PAGES only for cold links with no page route", () => {
    // No "#/page" in the URL: each kind lands on its home page.
    expect(entityAnchorAttrs("agent", "a1")?.href).toBe("/#/submissions?agent=a1");
    expect(entityAnchorAttrs("miner", "5Miner")?.href).toBe("/#/overview?miner=5Miner");
    expect(entityAnchorAttrs("validator", "5Val")?.href).toBe("/#/operations?validator=5Val");
    expect(entityAnchorAttrs("screener", "5Scr")?.href).toBe("/#/operations?screener=5Scr");
    // With a live page route, the overlay opens over the current page.
    setLocation("/#/leaderboard");
    expect(entityAnchorAttrs("agent", "a1")?.href).toBe("/#/leaderboard?agent=a1");
  });

  it("uses the identifier as the label fallback and appends extra classes", () => {
    const attrs = entityAnchorAttrs("miner", "5Miner", null, "featured");
    expect(attrs?.label).toBe("5Miner");
    expect(attrs?.class).toBe("entity-link featured");
  });

  it("returns null without an identifier (callers render the bare label)", () => {
    expect(entityAnchorAttrs("agent", null, "label")).toBeNull();
    expect(entityAnchorAttrs("agent", "", "label")).toBeNull();
  });

  it("mints hrefs that readEntityRoute recognizes (link → route round trip)", () => {
    setLocation("/#/submissions?status=rejected");
    const attrs = entityAnchorAttrs("agent", "agent 1/x");
    setLocation(attrs?.href ?? "/");
    const route = readEntityRoute();
    expect(route).toMatchObject({ kind: "agent", id: "agent 1/x", legacy: false, full: false });
  });

  it("keeps config knobs in the real query, never in the hash", () => {
    setBoot("api=https://api.example");
    setLocation("/?api=https%3A%2F%2Fapi.example#/submissions");
    const href = entityAnchorAttrs("agent", "a1")?.href ?? "";
    expect(href.startsWith("/?api=")).toBe(true);
    expect(new URL(href, location.href).hash).toBe("#/submissions?agent=a1");
  });
});

describe("canonicalEntityUrl", () => {
  it("resolves the dedicated entity page to an absolute URL", () => {
    // jsdom's origin is http://localhost:3000 by default; derive it so the
    // assertion follows the environment.
    const expected = new URL("/agent/agent-1", location.href).href;
    expect(canonicalEntityUrl("agent", "agent-1")).toBe(expected);
  });

  it("percent-encodes the identifier", () => {
    expect(canonicalEntityUrl("miner", "5G/rw v")).toBe(
      new URL("/miner/5G%2Frw%20v", location.href).href,
    );
  });

  it("carries the config knobs so the pasted link hits the same deploy", () => {
    setBoot("api=https://api.example");
    expect(canonicalEntityUrl("agent", "a1")).toBe(
      new URL("/agent/a1?api=https%3A%2F%2Fapi.example", location.href).href,
    );
  });
});

describe("entityActions / full-page routes", () => {
  it("full-page routes exist only for agents and miners", () => {
    expect(hasFullEntityPage("agent")).toBe(true);
    expect(hasFullEntityPage("miner")).toBe(true);
    expect(hasFullEntityPage("validator")).toBe(false);
    expect(hasFullEntityPage("screener")).toBe(false);
  });

  it("offers the full-page link for agents and miners, none for validators", () => {
    expect(entityActions("agent", "a1").openFullHref).toBe("/agent/a1");
    expect(entityActions("miner", "5Miner").openFullHref).toBe("/miner/5Miner");
    expect(entityActions("validator", "5Val").openFullHref).toBeNull();
    expect(entityActions("screener", "5Scr").openFullHref).toBeNull();
  });

  it("points back at the page under the overlay", () => {
    setLocation("/#/operations?validator=5Val");
    expect(entityBackHref("validator")).toBe("/#/operations");
    expect(entityActions("validator", "5Val").backHref).toBe("/#/operations");
  });

  it("falls back to the entity's home page for cold links", () => {
    // A dedicated /agent/{id} page has no hash page route.
    setLocation("/agent/a1");
    expect(entityBackHref("agent")).toBe("/#/submissions");
    expect(entityBackHref("miner")).toBe("/#/overview");
    expect(entityBackHref("validator")).toBe("/#/operations");
  });

  it("keeps the page's view state and drops only the entity param on close", () => {
    // dashboardHref semantics: closing an overlay stays on the same page, so
    // its filters and page number survive; only the overlay param goes away.
    setLocation("/#/submissions?status=rejected&page=2&agent=a1");
    expect(entityBackHref("agent")).toBe("/#/submissions?status=rejected&page=2");
    expect(entityBackHref("miner")).toBe("/#/submissions?status=rejected&page=2");
  });
});
