// Shared leaderboard view state. The monolith kept ONE #leaderboard-block DOM
// node and re-parented it between the overview column and the dedicated
// Leaderboard page; here the block is one component rendered by two routes,
// and this module is what makes the two mounts the same instrument: tab,
// sort, filter, expanded families, and the selected bench view live at module
// scope so navigating between the two homes never resets them (the page
// number is hash-owned, mirroring restoreBoardPage/writeBoardPage 3894–3928).
import { createSignal } from "solid-js";

import { currentPageName, parseHashRoute, spaHref } from "../../lib/router";

export type BoardTab = "all" | "scored" | "provisional";
export type BoardSortKey = "rank" | "composite" | "cost" | "latency" | "first_seen";

/** The board pages 25 rows at a time (boardView.pageSize, monolith 3855). */
export const boardPageSize = 25;

// Scored is the default tab because a provisional run is pre-quorum
// feedback, not a standing (monolith 3851–3855).
const [tab, setTab] = createSignal<BoardTab>("scored");
const [sort, setSort] = createSignal<BoardSortKey>("rank");
const [dir, setDir] = createSignal<1 | -1>(1);
const [page, setPage] = createSignal(1);
const [query, setQuery] = createSignal("");
const [families, setFamilies] = createSignal<ReadonlySet<string>>(new Set<string>());
const [versionView, setVersionView] = createSignal("current");

export const boardTab = tab;
export const setBoardTab = setTab;
export const boardSort = sort;
export const setBoardSort = setSort;
export const boardDir = dir;
export const setBoardDir = setDir;
export const boardPage = page;
export const setBoardPage = setPage;
export const boardQuery = query;
export const setBoardQuery = setQuery;
export const expandedFamilies = families;
/** "current" or a bench version as a string (leaderboardVersionView). */
export const leaderboardVersionView = versionView;
export const setLeaderboardVersionView = setVersionView;

export function toggleFamily(key: string): boolean {
  const next = new Set(families());
  const opening = !next.has(key);
  if (opening) next.add(key);
  else next.delete(key);
  setFamilies(next);
  return opening;
}

/** Reset the view to its defaults (used by tests and the unmount-free
 * equivalents of the monolith's full-page reload paths). */
export function resetBoardState(): void {
  setTab("scored");
  setSort("rank");
  setDir(1);
  setPage(1);
  setQuery("");
  setFamilies(new Set<string>());
}

// ── Hash-owned pager state (monolith 3894–3928) ──────────────
// The leaderboard pager, like the submissions pager, is query-param state:
// the hash query is the source of truth, boardPage is its mirror. Both
// pagers use the same "page" key; they never collide because it is read and
// written only on the page that owns it and navigation between pages clears
// the page-scoped params (dashboardHref).

/** Re-read boardPage from the hash. Returns true when the URL carried junk
 * that should be rewritten out (page=1 or a non-canonical integer). */
export function restoreBoardPage(): boolean {
  // The board lives on two pages (overview pane and the dedicated
  // Leaderboard page); both own the "page" param.
  const owner = currentPageName();
  const requested =
    owner === "overview" || owner === "leaderboard" ? parseHashRoute().query.get("page") : null;
  const parsed = Number(requested);
  setPage(
    requested !== null && /^[1-9][0-9]*$/.test(requested) && Number.isSafeInteger(parsed)
      ? parsed
      : 1,
  );
  return requested !== null && (page() === 1 || String(page()) !== requested);
}

export function writeBoardPage(push: boolean): void {
  // The overview and Leaderboard pages own "page"; dedicated entity pages
  // have no board to page through.
  const owner = currentPageName();
  if (owner !== "overview" && owner !== "leaderboard") return;
  if (/^\/(agent|miner)s?\//.test(location.pathname)) return;
  const hashQuery = parseHashRoute().query;
  if (page() > 1) hashQuery.set("page", String(page()));
  else hashQuery.delete("page");
  history[push ? "pushState" : "replaceState"]({}, "", spaHref(owner, hashQuery));
}

export function navigateBoardPage(next: number): void {
  setPage(next);
  writeBoardPage(true);
}

// ── Rank-movement baseline (monolith 4031–4049) ──────────────
// The ranks from the viewer's last visit. Every render compares against this
// snapshot (kept for the session) and then rewrites localStorage, so the
// arrows read "since your last visit". Guarded so a sandbox / disabled
// storage just yields no arrows.
let prevRanks: Record<string, number> = {};
let prevRanksHasData = false;
try {
  const stored = typeof localStorage !== "undefined" ? localStorage.getItem("ditto:ranks") : null;
  prevRanks = (JSON.parse(stored || "{}") as Record<string, number>) || {};
  prevRanksHasData = Object.keys(prevRanks).length > 0;
} catch {
  prevRanks = {};
}

export function persistRanks(entries: ReadonlyArray<{ miner_hotkey: string }>): void {
  try {
    if (typeof localStorage === "undefined") return;
    const m: Record<string, number> = {};
    entries.forEach((e, i) => {
      m[e.miner_hotkey] = i + 1;
    });
    localStorage.setItem("ditto:ranks", JSON.stringify(m));
  } catch {
    /* storage unavailable, arrows just won't persist */
  }
}

export interface RankMoveState {
  kind: "new" | "up" | "down";
  delta: number;
}

/** Rank movement since the viewer's last visit (rankMove, 5737–5746). */
export function rankMoveState(hotkey: string, currRank: number): RankMoveState | null {
  if (!(hotkey in prevRanks)) {
    return prevRanksHasData ? { kind: "new", delta: 0 } : null;
  }
  const d = (prevRanks[hotkey] as number) - currRank;
  if (d > 0) return { kind: "up", delta: d };
  if (d < 0) return { kind: "down", delta: -d };
  return null;
}
