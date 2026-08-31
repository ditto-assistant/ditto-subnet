// Reactive routing state + history actions. WHAT happens on an entity route
// (opening drawers/modals, forcing the operations page) is App-level; this
// store only keeps the signals in step with the URL and owns the history
// mutations.
import { batch, createSignal } from "solid-js";
import type { Accessor } from "solid-js";
import {
  ENTITY_PAGES,
  clearEntityParams,
  currentPageName,
  dashboardHref,
  entityHref,
  isPageName,
  operationsHref,
  operationsViewFromPathname,
  pageFromPathname,
  readEntityRoute,
  spaHref,
  spaQuery,
} from "../lib/router";
import type { EntityKind, EntityRoute, OperationsView, PageName } from "../lib/router";
import { rememberScroll, scrollToTop } from "../lib/scroll";

// URL to return to when closing an overlay opened via pushEntityRoute.
export const entityReturnUrl: { value: string | null } = { value: null };

function sameEntityRoute(a: EntityRoute | null, b: EntityRoute | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.kind === b.kind &&
    a.id === b.id &&
    a.key === b.key &&
    a.full === b.full &&
    a.legacy === b.legacy
  );
}

// Resolve the page the URL addresses. `current` is null only for the boot
// resolve, which must always compute a page so nav/title state initializes.
function hrefNow(): string {
  return location.pathname + location.search + location.hash;
}

function derivePage(current: PageName | null): PageName {
  const hash = location.hash || "";
  const m = /^#\/([a-z]+)/.exec(hash);
  const entity = readEntityRoute();
  const routeHash = hash.indexOf("#/") === 0;
  // A "#/…"-shaped hash is a leftover page route. Leave any other non-empty
  // fragment (e.g. the skip link's "#main-content") alone so it isn't hijacked.
  if (!entity && !m && hash && !routeHash && current !== null) return current;
  const candidate = m?.[1];
  const hashPage = candidate !== undefined && isPageName(candidate) ? candidate : null;
  const pathPage = pageFromPathname();
  const page: PageName =
    hashPage ?? pathPage ?? (entity ? (ENTITY_PAGES[entity.kind] ?? "overview") : "overview");
  if (entity?.full) return page;
  // Legacy #/agents/{id} owns the hash until EntityPanel normalizes it.
  if (entity?.legacy && routeHash && !hashPage) return page;
  const target = spaHref(page, spaQuery());
  if (hrefNow() !== target) {
    history.replaceState((history.state as unknown) ?? {}, "", target);
  }
  return page;
}

const [pageSignal, setPageSignal] = createSignal<PageName>(derivePage(null));
const [operationsViewSignal, setOperationsViewSignal] = createSignal<OperationsView>(
  operationsViewFromPathname(),
);
const [entitySignal, setEntitySignal] = createSignal<EntityRoute | null>(readEntityRoute(), {
  equals: sameEntityRoute,
});

// Driven by the document URL; defaults to "overview".
export const currentPage: Accessor<PageName> = pageSignal;
export const operationsViewRoute: Accessor<OperationsView> = operationsViewSignal;
export const entityRoute: Accessor<EntityRoute | null> = entitySignal;

// Recompute both signals from the current location. Batched: the two writes
// describe ONE location, and an unbatched page write flushes effects while
// the entity signal still holds the previous URL's route. EntityPanel reads
// both together to keep a fleet route on the operations page, so that torn
// pair reads as "a screener overlay is open on the leaderboard" and rewrites
// the URL straight back to `/operations?screener=…`, pinning the reader to
// the fleet page until they hand-edit the query.
export function syncFromLocation(): void {
  batch(() => {
    setPageSignal((prev) => derivePage(prev));
    setOperationsViewSignal(operationsViewFromPathname());
    setEntitySignal(readEntityRoute());
  });
}

/** Operations tabs are real routes, not local-only state. A copied URL and
 * browser Back/Forward therefore restore the selected fleet. */
export function navigateToOperationsView(
  view: OperationsView,
  options: { replace?: boolean; keepEntity?: boolean } = {},
): void {
  const query = spaQuery();
  if (!options.keepEntity) clearEntityParams(query);
  const target = operationsHref(view, query);
  if (hrefNow() === target) return;
  if (options.replace) history.replaceState((history.state as unknown) ?? {}, "", target);
  else history.pushState({}, "", target);
  if (!options.keepEntity) entityReturnUrl.value = null;
  syncFromLocation();
}

// Sidebar navigation: route through dashboardHref so an open overlay is
// dropped cleanly and the page-scoped view state is reset on a move to a
// different page (dashboardHref strips it from the URL; the caller keeps the
// in-memory mirrors in step so a cleared URL and the loaders never disagree).
export function navigateToPage(page: PageName): void {
  const target = dashboardHref(page);
  if (location.pathname + location.search + location.hash === target) return;
  rememberScroll();
  history.pushState({}, "", target);
  entityReturnUrl.value = null;
  syncFromLocation();
  // pushState never scrolls, so without this the reader keeps the offset they
  // had on the page they just left and lands mid-way down a page they have
  // not seen — or clamped to its bottom when the new page is shorter.
  scrollToTop();
}

export function pushEntityRoute(kind: EntityKind, id: string): void {
  const href = entityHref(kind, id);
  if (location.pathname + location.search + location.hash === href) return;
  entityReturnUrl.value = location.pathname + location.search + location.hash;
  rememberScroll();
  history.pushState({ entity: true }, "", href);
  syncFromLocation();
}

// The URL half of closing an entity overlay (the modal/drawer teardown is the
// caller's job).
export function closeEntityRoute(): void {
  const entity = readEntityRoute();
  // Dedicated entity pages (/agent/{id}) own their URL; closing is a no-op.
  if (entity && entity.full) return;
  if (
    entity &&
    (entity.kind === "agent" || entity.kind === "miner" || entity.kind === "validator")
  ) {
    const state = history.state as { entity?: unknown } | null;
    if (entityReturnUrl.value && state && state.entity) {
      // The overlay minted a history entry; going back restores the exact
      // pre-overlay URL (the popstate listener re-syncs the signals and
      // clears entityReturnUrl).
      history.back();
      return;
    }
    // Return to the page the overlay was opened over; ENTITY_PAGES is only
    // the fallback when no page route is present.
    history.replaceState(
      {},
      "",
      dashboardHref(currentPageName() ?? ENTITY_PAGES[entity.kind] ?? "overview"),
    );
  }
  entityReturnUrl.value = null;
  syncFromLocation();
}

let routeListenersInstalled = false;

// hashchange + popstate wiring; call once from App. The callback runs after
// the signals have been re-synced from the new location.
export function initRouteListeners(onPopState: () => void): void {
  if (routeListenersInstalled) return;
  routeListenersInstalled = true;
  window.addEventListener("hashchange", () => {
    syncFromLocation();
  });
  window.addEventListener("popstate", () => {
    syncFromLocation();
    // Leaving the overlay via Back lands on an entity-less URL; the saved
    // return URL has served its purpose (or become stale) either way.
    if (!readEntityRoute()) entityReturnUrl.value = null;
    onPopState();
  });
}
