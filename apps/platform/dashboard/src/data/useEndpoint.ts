import { createEffect, createResource, getOwner, onCleanup } from "solid-js";
import type { Accessor } from "solid-js";

import { getJSON } from "../lib/api";
import { entityRoute } from "../stores/routeStore";

export interface ResourceState<T> {
  data: Accessor<T | undefined>;
  loading: Accessor<boolean>;
  error: Accessor<unknown>;
  refresh: () => void;
}

export interface UseEndpointOptions {
  /** Refresh cadence. Hidden tabs skip network refreshes entirely and catch
   * up once on return, so idle dashboards stop polling the API (the
   * monolith's document.hidden rule around its load() tick). */
  pollMs?: number;
}

// Every live endpoint's refresh, so the shell's manual refresh reaches data
// owned by pages and module-scope stores, not just the resources App holds —
// the monolith's single load() had this property for free.
const liveRefreshes = new Set<() => void>();

export function refreshAllEndpoints(): void {
  liveRefreshes.forEach((refresh) => refresh());
}

/**
 * True while an agent card owns the surface (monolith load() 10086–10095,
 * #648). An agent deep link is an entity-first surface: the global board,
 * fleet, search-corpus, and timeline reads are unrelated to the answer the
 * reader is waiting for, so every periodic read pauses while the card is open.
 * The card's OWN summary and pipeline requests are not routed through here and
 * are never paused. Reactive — callers may gate a timer with it or watch it.
 */
export function agentCardOpen(): boolean {
  const route = entityRoute();
  return route !== null && route.kind === "agent";
}

/**
 * Run `hydrate` once when an agent card closes, and only if a card was open
 * (monolith closeModal 6449–6454: "Closing the card hydrates the dashboard
 * once"). Must be called under an owner — a component body or onMount.
 */
export function hydrateOnAgentCardClose(hydrate: () => void): void {
  let paused = false;
  createEffect(() => {
    if (agentCardOpen()) {
      paused = true;
      return;
    }
    if (!paused) return;
    paused = false;
    hydrate();
  });
}

export function useEndpoint<T>(
  path: Accessor<string> | string,
  options?: UseEndpointOptions,
): ResourceState<T> {
  const source = typeof path === "string" ? () => path : path;
  const [data, { refetch }] = createResource(source, (next) => getJSON<T>(next));
  // The page-level refresh and a faster page-local poll can land together.
  // Share one in-flight request so a slower duplicate cannot fail after a
  // successful response and replace live data with an error state
  // (loadOperations 9789–9793). A refresh that arrives mid-flight is not
  // dropped — one trailing refetch runs after the current request settles,
  // so "refresh now" always eventually observes the live API.
  let inFlight = false;
  let trailing = false;
  const refresh = (): void => {
    if (inFlight) {
      trailing = true;
      return;
    }
    inFlight = true;
    Promise.resolve(refetch())
      .catch(() => undefined)
      .finally(() => {
        inFlight = false;
        if (trailing) {
          trailing = false;
          refresh();
        }
      });
  };
  liveRefreshes.add(refresh);

  let timer: ReturnType<typeof setInterval> | undefined;
  let onVisibility: (() => void) | undefined;
  if (options?.pollMs) {
    let refreshStale = false;
    timer = setInterval(() => {
      if (document.hidden) {
        refreshStale = true;
        return;
      }
      // Paused, not deferred: closing the card hydrates once (App.tsx), so a
      // stale flag here would only queue a second, redundant read.
      if (agentCardOpen()) return;
      refresh();
    }, options.pollMs);
    onVisibility = () => {
      if (!document.hidden && refreshStale) {
        refreshStale = false;
        refresh();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
  }

  // Component-owned endpoints unregister on unmount; module-scope stores
  // (created under createRoot) live for the page lifetime, like the monolith's
  // globals, so their lack of an owner is fine.
  if (getOwner()) {
    onCleanup(() => {
      liveRefreshes.delete(refresh);
      if (timer !== undefined) clearInterval(timer);
      if (onVisibility) document.removeEventListener("visibilitychange", onVisibility);
    });
  }

  return {
    data,
    loading: () => data.loading,
    error: () => data.error,
    refresh,
  };
}
