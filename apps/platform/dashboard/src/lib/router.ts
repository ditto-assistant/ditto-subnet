// URL logic for the SPA router.
//
// All SPA state (the entity overlay params and the activity filters) lives in
// a query string INSIDE the hash ("#/submissions?agent=…&status=…"). The real
// (server-visible) query string carries only deploy/config knobs (?api=,
// ?wandb=), so the document URL and its HTTP cache entry stay stable while
// the SPA navigates. Legacy links with SPA state in the real query are
// recognized once and normalized into the hash form.
import { bootParams } from "./config";

export type PageName =
  | "overview"
  | "leaderboard"
  | "operations"
  | "submissions"
  | "reviews"
  | "benchmark";

// Sidebar pages (title, subtitle) in sidebar order. Deliberately mutable: the
// benchmark subtitle is rewritten in place once the live bench version is
// known.
export const PAGES: Record<PageName, { title: string; sub: string }> = {
  overview: {
    title: "Overview",
    sub: "Subnet snapshot and the full leaderboard · ranked by composite",
  },
  leaderboard: {
    title: "Leaderboard",
    sub: "Every scored miner · canonical ranks · sortable by any metric",
  },
  operations: {
    title: "Network operations",
    sub: "Live submission pipeline and validator / screener fleet health",
  },
  submissions: {
    title: "Recent submissions",
    sub: "Screening evidence and validator quorum progress · select a row for history",
  },
  reviews: {
    title: "ATH reviews",
    sub: "Public queue of held high-score submissions · scores preserved, emissions paused",
  },
  benchmark: {
    title: "Benchmark",
    sub: "What the scoring benchmark measures and the frozen scoring setup",
  },
};

export function isPageName(value: string): value is PageName {
  return Object.prototype.hasOwnProperty.call(PAGES, value);
}

export type EntityKind = "agent" | "miner" | "validator" | "screener";

// Singular kind → plural path segment (legacy /agents/{id} style paths).
export const ENTITY_PATHS: Record<EntityKind, string> = {
  agent: "agents",
  miner: "miners",
  validator: "validators",
  screener: "screeners",
};

// Plural kind → overlay query param. The param name is the singular kind.
export const ENTITY_PARAMS: Record<string, string> = {
  agents: "agent",
  miners: "miner",
  validators: "validator",
  screeners: "screener",
};

// Fallback page for cold entity links with no page route. Keyed by both the
// plural form (as in the original) and the singular EntityKind so
// `ENTITY_PAGES[route.kind]` works with the normalized EntityRoute.
export const ENTITY_PAGES: Record<string, PageName> = {
  agents: "submissions",
  miners: "overview",
  validators: "operations",
  screeners: "operations",
  agent: "submissions",
  miner: "overview",
  validator: "operations",
  screener: "operations",
};

// Per-page view state (submissions filters + either pager's "page"). It is
// scoped to the page that owns it, so it must not ride along to another page.
export const PAGE_SCOPED_PARAMS: string[] = ["status", "downloadable", "q", "page"];

// The config knobs allowed to appear in the real query string.
const CONFIG_KEYS = ["api", "wandb"] as const;

// Entity param precedence when more than one is present (original key order).
const KIND_PRECEDENCE: readonly EntityKind[] = ["agent", "miner", "validator", "screener"];

const PLURAL_TO_KIND: Record<string, EntityKind> = {
  agents: "agent",
  miners: "miner",
  validators: "validator",
  screeners: "screener",
};

export function clearEntityParams(query: URLSearchParams): void {
  Object.keys(ENTITY_PARAMS).forEach((kind) => {
    const param = ENTITY_PARAMS[kind];
    if (param !== undefined) query.delete(param);
  });
}

export interface HashRoute {
  page: string | null;
  query: URLSearchParams;
}

export function parseHashRoute(hash?: string): HashRoute {
  const raw = hash ?? (location.hash || "");
  if (raw.indexOf("#/") !== 0) return { page: null, query: new URLSearchParams() };
  const rest = raw.slice(2);
  const split = rest.indexOf("?");
  return {
    page: split === -1 ? rest : rest.slice(0, split),
    query: new URLSearchParams(split === -1 ? "" : rest.slice(split + 1)),
  };
}

// The real query string of every SPA-minted URL: config knobs only, taken
// from the boot-time params so stray query junk is never carried forward.
export function configSearch(): string {
  const config = new URLSearchParams();
  for (const key of CONFIG_KEYS) {
    const value = bootParams.get(key);
    if (value !== null) config.set(key, value);
  }
  const qs = config.toString();
  return qs ? "?" + qs : "";
}

export function spaHref(page: string, query?: URLSearchParams): string {
  const qs = query ? query.toString() : "";
  return "/" + configSearch() + "#/" + page + (qs ? "?" + qs : "");
}

// The page currently addressed by the hash, or null when the hash is not a
// valid page route (e.g. on a dedicated /agent/{id} page).
export function currentPageName(): PageName | null {
  const page = parseHashRoute().page;
  return page !== null && page !== "" && isPageName(page) ? page : null;
}

export function entityHref(kind: EntityKind, identifier: string, page?: string): string {
  const plural = ENTITY_PATHS[kind] || kind;
  // Keep the rest of the hash state (activity filters) so opening an
  // overlay never resets the page under it.
  const query = parseHashRoute().query;
  clearEntityParams(query);
  query.set(ENTITY_PARAMS[plural] ?? kind, String(identifier));
  // Drilldowns are overlays: they open over whatever page is active, so the
  // hash keeps the current page. ENTITY_PAGES is only the fallback for cold
  // links minted where no page route is present (dedicated entity pages).
  return spaHref(page || currentPageName() || ENTITY_PAGES[plural] || "overview", query);
}

export function fullEntityHref(kind: EntityKind, identifier: string): string {
  // Dedicated pages use the singular path segment and carry only the config knobs.
  const singular = ENTITY_PARAMS[ENTITY_PATHS[kind] || kind] ?? kind;
  const query = new URLSearchParams();
  for (const key of CONFIG_KEYS) {
    const value = bootParams.get(key);
    if (value !== null) query.set(key, value);
  }
  return (
    "/" +
    singular +
    "/" +
    encodeURIComponent(String(identifier)) +
    (query.toString() ? "?" + query.toString() : "")
  );
}

export function dashboardHref(page: PageName): string {
  const query = parseHashRoute().query;
  clearEntityParams(query);
  // Same page (e.g. closing an overlay) keeps that page's view state; moving
  // to a different page drops it so it never reappears as stale filters or a
  // stale page number, and so both pagers can safely share the "page" key.
  if (page !== currentPageName()) {
    PAGE_SCOPED_PARAMS.forEach((key) => query.delete(key));
  }
  return spaHref(page, query);
}

export interface EntityRoute {
  kind: EntityKind;
  id: string;
  key: string;
  full: boolean;
  legacy: boolean;
}

// First kind whose overlay param is present wins; a present-but-empty value
// is not a route and does NOT fall through to the next kind.
function entityIn(query: URLSearchParams): EntityRoute | null {
  const kind = KIND_PRECEDENCE.find((candidate) => query.has(candidate));
  const id = kind ? query.get(kind) : null;
  return kind && id ? { kind, id, key: kind + ":" + id, full: false, legacy: false } : null;
}

// Resolve which entity (if any) the URL addresses, across 5 forms in
// precedence order: full path /agent|miner/{id} → hash-query param
// (canonical) → real-query param (legacy) → legacy hash #/agents/{id} →
// legacy path /agents/{id}.
export function readEntityRoute(): EntityRoute | null {
  let match = /^\/(agent|miner)\/([^/]+)\/?$/.exec(location.pathname);
  if (match) {
    try {
      const kind: EntityKind = match[1] === "agent" ? "agent" : "miner";
      const id = decodeURIComponent(match[2] ?? "");
      return { kind, id, key: kind + ":" + id, full: true, legacy: false };
    } catch {
      return null;
    }
  }

  // Canonical: entity params in the hash query ("#/operations?agent=…").
  const hashEntity = entityIn(parseHashRoute().query);
  if (hashEntity) return hashEntity;

  // Legacy: entity params in the real query ("?agent=…#/submissions").
  // Marked legacy so the entity resolver normalizes the URL to the hash form.
  const searchEntity = entityIn(new URLSearchParams(location.search));
  if (searchEntity) return { ...searchEntity, legacy: true };

  match = /^#\/(agents|miners|validators|screeners)\/([^/?#]+)\/?$/.exec(location.hash);
  if (!match) {
    match = /^\/(agents|miners|validators|screeners)\/([^/]+)\/?$/.exec(location.pathname);
  }
  if (!match) return null;
  try {
    const kind = PLURAL_TO_KIND[match[1] ?? ""];
    if (!kind) return null;
    const id = decodeURIComponent(match[2] ?? "");
    return { kind, id, key: kind + ":" + id, full: false, legacy: true };
  } catch {
    return null;
  }
}
