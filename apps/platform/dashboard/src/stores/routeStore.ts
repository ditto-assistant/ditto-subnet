// Reactive routing state + history actions. WHAT happens on an entity route
// (opening drawers/modals, forcing the operations page) is App-level; this
// store only keeps the signals in step with the URL and owns the history
// mutations.
import { createSignal } from "solid-js";
import type { Accessor } from "solid-js";
import {
  ENTITY_PAGES,
  currentPageName,
  dashboardHref,
  entityHref,
  isPageName,
  readEntityRoute,
} from "../lib/router";
import type { EntityKind, EntityRoute, PageName } from "../lib/router";

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
function derivePage(current: PageName | null): PageName {
  const hash = location.hash || "";
  const m = /^#\/([a-z]+)/.exec(hash);
  const entity = readEntityRoute();
  const routeHash = hash.indexOf("#/") === 0;
  // A "#/…"-shaped hash is a page route; leave any other non-empty fragment
  // (e.g. the skip link's "#main-content") alone so it isn't hijacked.
  if (!entity && !m && hash && !routeHash && current !== null) return current;
  // The hash owns the page. An entity param is an overlay on top of that
  // page, not a page itself; ENTITY_PAGES only decides where a cold link
  // with no page route lands. (Hash must win or the URL and the sidebar
  // disagree after closing an overlay.)
  const candidate = m?.[1];
  const hashPage = candidate !== undefined && isPageName(candidate) ? candidate : null;
  const page: PageName =
    hashPage ?? (entity ? (ENTITY_PAGES[entity.kind] ?? "overview") : "overview");
  // An unknown "#/…" route (e.g. "#/bogus", "#/") resolves to overview both
  // at boot and after; normalize the URL so it doesn't advertise a route that
  // doesn't exist. Entity routes own their hash and are left untouched.
  if (!entity && hash && routeHash && !hashPage) {
    history.replaceState(
      (history.state as unknown) ?? {},
      "",
      location.pathname + location.search + "#/" + page,
    );
  }
  return page;
}

const [pageSignal, setPageSignal] = createSignal<PageName>(derivePage(null));
const [entitySignal, setEntitySignal] = createSignal<EntityRoute | null>(readEntityRoute(), {
  equals: sameEntityRoute,
});

// Driven by the hash; defaults to "overview".
export const currentPage: Accessor<PageName> = pageSignal;
export const entityRoute: Accessor<EntityRoute | null> = entitySignal;

// Recompute both signals from the current location.
export function syncFromLocation(): void {
  setEntitySignal(readEntityRoute());
  setPageSignal((prev) => derivePage(prev));
}

// Sidebar navigation: route through dashboardHref so an open overlay is
// dropped cleanly and the page-scoped view state is reset on a move to a
// different page (dashboardHref strips it from the URL; the caller keeps the
// in-memory mirrors in step so a cleared URL and the loaders never disagree).
export function navigateToPage(page: PageName): void {
  const target = dashboardHref(page);
  if (location.pathname + location.search + location.hash === target) return;
  history.pushState({}, "", target);
  entityReturnUrl.value = null;
  syncFromLocation();
}

export function pushEntityRoute(kind: EntityKind, id: string): void {
  const href = entityHref(kind, id);
  if (location.pathname + location.search + location.hash === href) return;
  entityReturnUrl.value = location.pathname + location.search + location.hash;
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
    // Return to the page the overlay was opened over (the hash still holds
    // it); ENTITY_PAGES is only the fallback when no page route is present.
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
