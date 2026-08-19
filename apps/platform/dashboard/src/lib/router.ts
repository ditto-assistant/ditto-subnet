// URL logic for the SPA router.
//
// Pathname is the sidebar page (`/operations`, `/leaderboard`). Overlay and
// filter state lives in the real query (`/operations?validator=…`). Config
// knobs (`?api=`, `?wandb=`) share that query and are re-applied from the
// boot snapshot so minted URLs never keep stray search junk. The only hash
// that is not a leftover to flatten is a skip-link (`#main-content`).
// Hash-only bookmarks (`/#/operations?validator=…`) still resolve; the
// router canonicalizes them onto the path+query form.
import { bootParams } from "./config";

export type PageName =
  | "overview"
  | "leaderboard"
  | "pipeline"
  | "operations"
  | "submissions"
  | "reviews"
  | "ath"
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
  pipeline: {
    title: "Submission pipeline",
    sub: "Every submission from upload to scored · admission, validation, and integrity review",
  },
  operations: {
    title: "Fleet",
    sub: "Validator and screener capacity · live slot work · trusted miner-image builds",
  },
  submissions: {
    title: "Recent submissions",
    sub: "Screening evidence and validator quorum progress · select a row for history",
  },
  reviews: {
    title: "Miner sign-in",
    sub: "Sign in with your hotkey · manage your profile, submissions, and MCP",
  },
  ath: {
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

/** First path segment when it is a sidebar page (`/leaderboard`). */
export function pageFromPathname(pathname?: string): PageName | null {
  const raw = (pathname ?? location.pathname).replace(/^\/+|\/+$/g, "");
  const segment = raw.split("/")[0] ?? "";
  return segment !== "" && isPageName(segment) ? segment : null;
}

/** Crawlable pathname for a sidebar page. Overview canonicalizes to `/`. */
export function pagePathname(page: string): string {
  return page === "overview" ? "/" : "/" + page;
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
export const PAGE_SCOPED_PARAMS: string[] = [
  "status",
  "downloadable",
  "q",
  "page",
  "code",
  "login",
  "complete",
];

// The config knobs allowed to appear in the real query string.
const CONFIG_KEYS = ["api", "wandb"] as const;
const CONFIG_KEY_SET = new Set<string>(CONFIG_KEYS);
const SPA_QUERY_KEYS = new Set<string>([
  ...PAGE_SCOPED_PARAMS,
  "agent",
  "miner",
  "validator",
  "screener",
]);

function isConfigKey(key: string): boolean {
  return CONFIG_KEY_SET.has(key);
}

function copySpaParams(from: URLSearchParams, into: URLSearchParams): void {
  from.forEach((value, key) => {
    if (SPA_QUERY_KEYS.has(key)) into.append(key, value);
  });
}

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

/** Overlay/filter params from the current URL (config knobs stripped).
 * A leftover `#/page?…` hash still wins on colliding keys so one canonicalize
 * pass can flatten it into the real query. */
export function spaQuery(): URLSearchParams {
  const query = new URLSearchParams();
  copySpaParams(new URLSearchParams(location.search), query);
  const hashRoute = parseHashRoute();
  if (hashRoute.page === null) return query;
  const seen = new Set<string>();
  hashRoute.query.forEach((_, key) => {
    if (isConfigKey(key) || seen.has(key)) return;
    seen.add(key);
    query.delete(key);
  });
  copySpaParams(hashRoute.query, query);
  return query;
}

// Config knobs taken from the boot-time snapshot so stray query junk is
// never carried forward on minted URLs.
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
  const search = new URLSearchParams();
  for (const key of CONFIG_KEYS) {
    const value = bootParams.get(key);
    if (value !== null) search.set(key, value);
  }
  if (query) copySpaParams(query, search);
  const qs = search.toString();
  return pagePathname(page) + (qs ? "?" + qs : "");
}

// Pathname is canonical (`/leaderboard`). A leftover `#/page` still counts
// so a hash-only bookmark resolves before canonicalize. Null on a dedicated
// /agent/{id} page with no page route.
export function currentPageName(): PageName | null {
  const pathPage = pageFromPathname();
  if (pathPage) return pathPage;
  const hashPage = parseHashRoute().page;
  if (hashPage !== null && hashPage !== "" && isPageName(hashPage)) return hashPage;
  return null;
}

export function entityHref(kind: EntityKind, identifier: string, page?: string): string {
  const plural = ENTITY_PATHS[kind] || kind;
  // Keep the rest of the page query (activity filters) so opening an
  // overlay never resets the page under it.
  const query = spaQuery();
  clearEntityParams(query);
  query.set(ENTITY_PARAMS[plural] ?? kind, String(identifier));
  // Drilldowns are overlays over whatever page is active. ENTITY_PAGES is
  // only the fallback for cold links minted where no page route is present
  // (dedicated entity pages).
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
  const query = spaQuery();
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
// precedence order: full path /agent|miner/{id} → real-query param
// (canonical) → leftover hash-query param → legacy hash #/agents/{id} →
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

  match = /^\/h\/([^/]+)\/?$/.exec(location.pathname);
  if (match) {
    try {
      const id = decodeURIComponent(match[1] ?? "");
      return { kind: "miner", id, key: "miner:" + id, full: true, legacy: false };
    } catch {
      return null;
    }
  }

  const overlay = entityIn(spaQuery());
  if (overlay) {
    // A leftover `#/page?entity=` (or `?entity=#/page`) still needs a
    // flatten onto pathname + real query.
    const leftoverHashPage = parseHashRoute().page !== null;
    return leftoverHashPage ? { ...overlay, legacy: true } : overlay;
  }

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
