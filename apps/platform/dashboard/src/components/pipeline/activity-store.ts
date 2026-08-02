// Server-backed activity table state: URL restore/sanitize, history writes,
// paging, quick filters, and the fenced loader. Ports monolith 3313–3447
// (restoreActivityUrl, writeActivityUrl, navigateActivityPage, filterCount,
// activityRequestPath, applyActivityFilter, lockActivityFrameHeight) and
// loadActivity 9401–9432. The filter/page state lives in the HASH query
// (PAGE_SCOPED_PARAMS); the real query carries only deploy knobs. Legacy
// real-query filters are honored once and normalized into the hash form.
import { batch, createSignal } from "solid-js";
import type { Accessor } from "solid-js";

import { getJSON } from "../../lib/api";
import {
  ENTITY_PAGES,
  PAGE_SCOPED_PARAMS,
  currentPageName,
  parseHashRoute,
  readEntityRoute,
  spaHref,
} from "../../lib/router";
import type { ActivityPayload } from "../../types/pipeline";
import { ACTIVITY_FILTERS, ACTIVITY_PAGE_SIZE, ACTIVITY_STATUSES } from "./status";
import type { ActivityStatusEntry } from "./status";

export interface ActivityStore {
  statuses: Accessor<string[]>;
  downloadable: Accessor<boolean>;
  query: Accessor<string>;
  page: Accessor<number>;
  totalPages: Accessor<number>;
  total: Accessor<number | null>;
  statusCounts: Accessor<Record<string, number>>;
  downloadableCount: Accessor<number>;
  entries: Accessor<ActivityStatusEntry[]>;
  unavailable: Accessor<boolean>;
  /** True while a USER-initiated load is in flight (drives aria-busy and the
   * "Updating submissions…" flash; a silent background tick never does). */
  busy: Accessor<boolean>;
  /** True once any response (or failure) has arrived. */
  loaded: Accessor<boolean>;
  filtered: Accessor<boolean>;
  filterCount: (name: string) => number;
  filterSelected: (name: string) => boolean;
  /** Re-derive state from the URL; true means the URL needed sanitizing. */
  restore: () => boolean;
  /** Write the current state into the hash query (push or replace). */
  write: (push: boolean) => void;
  applyFilter: (name: string) => void;
  navigatePage: (page: number, anchor: HTMLElement | null, push: boolean, user: boolean) => void;
  load: (page: number, anchor: HTMLElement | null, userInitiated: boolean) => void;
  /** The shared search input's server-side submission search (250ms debounce
   * upstream): trimmed/sliced query, page 1, replaceState, reload. */
  serverSearch: (query: string) => void;
  /** The "Clear filters" control: statuses + query reset, page 1, push. */
  clearFilters: (anchor: HTMLElement | null) => void;
  /** The .activity-table-frame ratchet target (min-height keeps the bottom
   * pager still while a shorter page renders). */
  setFrame: (el: HTMLElement | null) => void;
  requestPath: (page: number) => string;
}

export function createActivityStore(): ActivityStore {
  const [statuses, setStatuses] = createSignal<string[]>([]);
  const [downloadable, setDownloadable] = createSignal(false);
  const [query, setQuery] = createSignal("");
  const [page, setPage] = createSignal(1);
  const [totalPages, setTotalPages] = createSignal(1);
  const [total, setTotal] = createSignal<number | null>(null);
  const [statusCounts, setStatusCounts] = createSignal<Record<string, number>>({});
  const [downloadableCount, setDownloadableCount] = createSignal(0);
  const [entries, setEntries] = createSignal<ActivityStatusEntry[]>([]);
  const [unavailable, setUnavailable] = createSignal(false);
  const [busy, setBusy] = createSignal(false);
  const [loaded, setLoaded] = createSignal(false);

  let requestId = 0;
  let frame: HTMLElement | null = null;

  function lockFrameHeight(): void {
    if (!frame) return;
    const height = Math.ceil(frame.getBoundingClientRect().height);
    const minimum = parseFloat(frame.style.minHeight) || 0;
    if (height > minimum) frame.style.minHeight = height + "px";
  }

  // Port of restoreActivityUrl (3315–3347): canonical filters live in the
  // hash query; legacy links carried them in the real query, honored once
  // and reported for normalization. "page" is shared with the board pager,
  // so it is read only while the submissions view owns it.
  function restore(): boolean {
    const hashQuery = parseHashRoute().query;
    const searchQuery = new URLSearchParams(location.search);
    const hasIn = (q: URLSearchParams): boolean => PAGE_SCOPED_PARAMS.some((key) => q.has(key));
    const legacy = !hasIn(hashQuery) && hasIn(searchQuery);
    const source = legacy ? searchQuery : hashQuery;
    const values: string[] = [];
    source.getAll("status").forEach((value) => {
      value.split(",").forEach((part) => values.push(part.trim()));
    });
    const requested = values.filter(Boolean);
    const valid = requested.filter(
      (value, index) => ACTIVITY_STATUSES.indexOf(value) >= 0 && requested.indexOf(value) === index,
    );
    const nextStatuses = ACTIVITY_STATUSES.filter((value) => valid.indexOf(value) >= 0);
    const nextDownloadable = source.get("downloadable") === "true";
    const nextQuery = (source.get("q") || "").trim().slice(0, 200);
    const requestedPage = currentPageName() === "submissions" ? source.get("page") : null;
    const parsedPage = Number(requestedPage);
    const nextPage =
      requestedPage !== null &&
      /^[1-9][0-9]*$/.test(requestedPage) &&
      Number.isSafeInteger(parsedPage)
        ? parsedPage
        : 1;
    batch(() => {
      setStatuses(nextStatuses);
      setDownloadable(nextDownloadable);
      setQuery(nextQuery);
      setPage(nextPage);
    });
    const pageNeedsSanitizing =
      requestedPage !== null && (nextPage === 1 || String(nextPage) !== requestedPage);
    return (
      legacy ||
      valid.length !== values.length ||
      (source.has("downloadable") && !nextDownloadable) ||
      nextQuery !== (source.get("q") || "") ||
      pageNeedsSanitizing
    );
  }

  // Port of writeActivityUrl (3349–3368). Dedicated entity pages have no
  // filter UI and no page hash to write into. An open overlay's entity param
  // is kept in the URL.
  function write(push: boolean): void {
    if (/^\/(agent|miner)s?\//.test(location.pathname)) return;
    const urlQuery = parseHashRoute().query;
    urlQuery.delete("status");
    statuses().forEach((status) => urlQuery.append("status", status));
    if (downloadable()) urlQuery.set("downloadable", "true");
    else urlQuery.delete("downloadable");
    if (query()) urlQuery.set("q", query());
    else urlQuery.delete("q");
    // "page" is owned by whichever pager is on the active page; only manage
    // the submissions pager's value here so it never clobbers the board's.
    if (currentPageName() === "submissions") {
      if (page() > 1) urlQuery.set("page", String(page()));
      else urlQuery.delete("page");
    }
    const entity = readEntityRoute();
    if (entity && !entity.full) urlQuery.set(entity.kind, entity.id);
    const pageName =
      currentPageName() || (entity && !entity.full ? ENTITY_PAGES[entity.kind] : "overview");
    history[push ? "pushState" : "replaceState"]({}, "", spaHref(pageName || "overview", urlQuery));
  }

  function requestPath(pageNumber: number): string {
    const requestQuery = new URLSearchParams();
    requestQuery.set("page", String(pageNumber));
    requestQuery.set("limit", String(ACTIVITY_PAGE_SIZE));
    statuses().forEach((status) => requestQuery.append("status", status));
    if (downloadable()) requestQuery.set("downloadable", "true");
    if (query()) requestQuery.set("q", query());
    return "/public/activity?" + requestQuery.toString();
  }

  // Port of loadActivity (9401–9432): request-id fenced; only user-initiated
  // loads flash aria-busy; an out-of-range page redirects to the last page;
  // the clicked pager control keeps its viewport position across the render.
  function load(pageNumber: number, anchor: HTMLElement | null, userInitiated: boolean): void {
    const anchorTop = anchor ? anchor.getBoundingClientRect().top : null;
    const id = ++requestId;
    lockFrameHeight();
    if (userInitiated) setBusy(true);
    getJSON<ActivityPayload>(requestPath(pageNumber))
      .then((data) => {
        if (id !== requestId) return;
        const list = (data.entries || []) as ActivityStatusEntry[];
        if (!list.length && pageNumber > 1 && (data.total_pages || 1) < pageNumber) {
          navigatePage(Math.max(1, data.total_pages || 1), anchor, false, userInitiated === true);
          return;
        }
        batch(() => {
          setBusy(false);
          setLoaded(true);
          setUnavailable(false);
          setStatusCounts(data.status_counts || {});
          setDownloadableCount(Number(data.downloadable_count || 0));
          setEntries(list);
          setPage(data.page || 1);
          setTotalPages(data.total_pages || 1);
          setTotal(data.total ?? list.length);
        });
        if (typeof requestAnimationFrame === "function") {
          requestAnimationFrame(() => {
            lockFrameHeight();
            if (anchor && anchorTop != null) {
              const shift = anchor.getBoundingClientRect().top - anchorTop;
              if (shift) window.scrollBy(0, shift);
              anchor.focus({ preventScroll: true });
            }
          });
        }
      })
      .catch(() => {
        if (id !== requestId) return;
        batch(() => {
          setBusy(false);
          setLoaded(true);
          setUnavailable(true);
          setEntries([]);
        });
      });
  }

  function navigatePage(
    pageNumber: number,
    anchor: HTMLElement | null,
    push: boolean,
    user: boolean,
  ): void {
    setPage(pageNumber);
    write(push !== false);
    load(pageNumber, anchor, user === true);
  }

  function filterCount(name: string): number {
    if (name === "downloadable") return downloadableCount();
    const names = name === "all" ? ACTIVITY_STATUSES : ACTIVITY_FILTERS[name] || [];
    const counts = statusCounts();
    return names.reduce((sum, status) => sum + Number(counts[status] || 0), 0);
  }

  function filterSelected(name: string): boolean {
    const active = statuses();
    if (name === "downloadable") return downloadable();
    if (name === "all") return active.length === 0 && !downloadable();
    const names = ACTIVITY_FILTERS[name] || [];
    return names.length > 0 && names.every((status) => active.indexOf(status) >= 0);
  }

  // Port of applyActivityFilter (3410–3427): a filter toggles its status
  // set; the result is re-ordered against the whitelist; paging resets.
  function applyFilter(name: string): void {
    if (name === "all") {
      setStatuses([]);
      setDownloadable(false);
    } else if (name === "downloadable") {
      setDownloadable((value) => !value);
    } else {
      const names = ACTIVITY_FILTERS[name] || [];
      const current = statuses().slice();
      const removing = names.every((status) => current.indexOf(status) >= 0);
      names.forEach((status) => {
        const index = current.indexOf(status);
        if (removing && index >= 0) current.splice(index, 1);
        if (!removing && index < 0) current.push(status);
      });
      setStatuses(ACTIVITY_STATUSES.filter((status) => current.indexOf(status) >= 0));
    }
    setPage(1);
    write(true);
    load(1, null, true);
  }

  // The shared search input's submissions tenant (monolith 6681–6694).
  function serverSearch(value: string): void {
    batch(() => {
      setQuery(value.trim().slice(0, 200));
      setPage(1);
    });
    write(false);
    load(1, null, true);
  }

  // The "Clear filters" control (monolith 6529–6538); the caller also empties
  // the shared search input.
  function clearFilters(anchor: HTMLElement | null): void {
    batch(() => {
      setStatuses([]);
      setDownloadable(false);
      setQuery("");
      setPage(1);
    });
    write(true);
    load(1, anchor, true);
  }

  return {
    statuses,
    downloadable,
    query,
    page,
    totalPages,
    total,
    statusCounts,
    downloadableCount,
    entries,
    unavailable,
    busy,
    loaded,
    filtered: () => statuses().length > 0 || downloadable() || Boolean(query()),
    filterCount,
    filterSelected,
    restore,
    write,
    applyFilter,
    navigatePage,
    load,
    serverSearch,
    clearFilters,
    setFrame: (el) => {
      frame = el;
    },
    requestPath,
  };
}
