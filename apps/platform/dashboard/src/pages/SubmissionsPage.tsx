// The Recent submissions page: mounts the activity board, restores (and
// sanitizes) filter/page state from the URL, wires the shared search input's
// submissions tenant (250ms debounce, monolith 6681–6694), the popstate
// restore (9846–9852), and the 30s background refresh (visibility-aware,
// silent — only user actions flash aria-busy).
import { onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { ActivityBoard } from "../components/pipeline/ActivityBoard";
import { createActivityStore } from "../components/pipeline/activity-store";
import { agentCardOpen, hydrateOnAgentCardClose } from "../data/useEndpoint";
import { REFRESH_MS } from "../lib/config";

function onSubmissionsRoute(): boolean {
  return (location.hash || "").startsWith("#/submissions");
}

export function SubmissionsPage(): JSX.Element {
  const store = createActivityStore();

  onMount(() => {
    // One-time normalization: junk statuses, an over-long q, or an invalid
    // page are dropped from state AND rewritten out of the URL (replace, not
    // push — sanitize passes never mint history entries).
    const sanitized = store.restore();
    if (sanitized) store.write(false);
    const searchInput = document.getElementById("search-input") as HTMLInputElement | null;
    if (searchInput) searchInput.value = store.query();
    store.load(store.page(), null, false);

    // popstate: re-derive filters/page from the restored URL, sync the shared
    // search input, and refetch the addressed page (monolith 9846–9852).
    const onPopState = (): void => {
      const wasSanitized = store.restore();
      if (wasSanitized) store.write(false);
      const input = document.getElementById("search-input") as HTMLInputElement | null;
      if (input) input.value = store.query();
      if (onSubmissionsRoute()) store.load(store.page(), null, true);
    };
    window.addEventListener("popstate", onPopState);

    // The shared search input doubles as the server-side submission search
    // while this page is routed (250ms debounce; monolith 6681–6694).
    let searchTimer: ReturnType<typeof setTimeout> | undefined;
    const onSearchInput = (): void => {
      if (!onSubmissionsRoute()) return;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        if (!onSubmissionsRoute()) return;
        const input = document.getElementById("search-input") as HTMLInputElement | null;
        store.serverSearch(input ? input.value : "");
      }, 250);
    };
    // The search-clear button resets the input programmatically (no input
    // event), so the reset is caught here (monolith 6712–6725).
    const onSearchClear = (): void => {
      if (!onSubmissionsRoute()) return;
      clearTimeout(searchTimer);
      store.serverSearch("");
    };
    searchInput?.addEventListener("input", onSearchInput);
    const clearButton = document.getElementById("search-clear");
    clearButton?.addEventListener("click", onSearchClear);

    // Background refresh: silent (never aria-busy), skipped while hidden and
    // while an agent card is open — the board behind it is not the answer the
    // reader is waiting for (#648). Closing the card reloads the page once.
    const timer = setInterval(() => {
      if (document.hidden || agentCardOpen()) return;
      store.load(store.page(), null, false);
    }, REFRESH_MS);
    hydrateOnAgentCardClose(() => store.load(store.page(), null, false));

    onCleanup(() => {
      window.removeEventListener("popstate", onPopState);
      searchInput?.removeEventListener("input", onSearchInput);
      clearButton?.removeEventListener("click", onSearchClear);
      clearTimeout(searchTimer);
      clearInterval(timer);
    });
  });

  return (
    <section class="page active" data-page="submissions">
      <ActivityBoard store={store} />
    </section>
  );
}
