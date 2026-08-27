// Window-scroll continuity across history entries.
//
// Every page here paints from an async read, so at the instant the browser
// would restore a scroll offset — on reload, and on back/forward — the
// document is barely taller than the viewport. Native restoration lands
// clamped near the top and never runs again once the rows arrive, which is
// why a reload or a Back always dropped the reader at the top of the board.
//
// Restoration is therefore taken over here: offsets are keyed by URL in
// sessionStorage (so a reload keeps them) and applied only once the document
// is genuinely tall enough to hold them, or abandoned when the reader starts
// scrolling on their own. A fresh push navigation is the opposite case and
// resets to the top explicitly (scrollToTop) — pushState never scrolls, so
// without it the reader lands mid-page on a page they have not seen.

const STORE_KEY = "ditto.scroll.v1";
/** Entries are evicted oldest-first; a session never addresses this many. */
const MAX_KEYS = 40;
/** How long to keep waiting for async content to make the document tall
 * enough for the saved offset before landing wherever it can reach. */
const RESTORE_DEADLINE_MS = 4000;
/** Poll cadence while waiting for the document to grow. Deliberately a timer
 * and not requestAnimationFrame: rAF is frozen in a backgrounded tab, which
 * would strand the restore (and the writes it suppresses) indefinitely. */
const RESTORE_POLL_MS = 50;

let positions: Record<string, number> = {};
let restorePending = false;
let cancelRestore: (() => void) | null = null;
/** The entry whose saved offset this module applied. A component's own
 * "reset to the top on open" must not fight a restore the reader asked for
 * by reloading or going Back, so it is sticky for the whole entry. */
let ownedEntry: string | null = null;

/** The hash is never canonical here — derivePage() rewrites every route into
 * the path+query form — so path+query identifies the history entry. */
function entryKey(): string {
  return location.pathname + location.search;
}

function readStored(): void {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    positions = parsed && typeof parsed === "object" ? (parsed as Record<string, number>) : {};
  } catch {
    positions = {};
  }
}

function persist(): void {
  try {
    const keys = Object.keys(positions);
    // Keys are re-inserted on every write, so insertion order is recency.
    keys.slice(0, Math.max(0, keys.length - MAX_KEYS)).forEach((key) => delete positions[key]);
    sessionStorage.setItem(STORE_KEY, JSON.stringify(positions));
  } catch {
    // Private mode / quota: scroll memory is a convenience, never a hard fail.
  }
}

/** Record the live offset in memory. Skipped while a restore is in flight —
 * the clamped offsets seen on the way there must not overwrite the target. */
function remember(): void {
  if (restorePending) return;
  const key = entryKey();
  delete positions[key];
  positions[key] = Math.round(window.scrollY);
}

function settle(target: number): void {
  restorePending = false;
  cancelRestore = null;
  window.scrollTo(0, target);
}

function restore(target: number): void {
  cancelRestore?.();
  const key = entryKey();
  if (target <= 0) {
    ownedEntry = null;
    settle(0);
    return;
  }
  ownedEntry = key;
  restorePending = true;
  const deadline = Date.now() + RESTORE_DEADLINE_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;
  // Wait without moving the viewport: jumping the reader down a half-built
  // document and again as each section lands reads as a broken page. One
  // jump, once the offset is actually reachable.
  const step = (): void => {
    const reachable = document.documentElement.scrollHeight - window.innerHeight;
    if (reachable >= target) {
      settle(target);
      return;
    }
    if (Date.now() >= deadline) {
      // Never past the target: this branch only runs while the document is
      // still too short, so `reachable` is the furthest honest landing.
      settle(Math.max(0, reachable));
      return;
    }
    timer = setTimeout(step, RESTORE_POLL_MS);
  };
  cancelRestore = () => {
    if (timer !== undefined) clearTimeout(timer);
    restorePending = false;
    cancelRestore = null;
  };
  step();
}

/**
 * Record the live offset for the current history entry right now. Callers
 * that are about to push a new entry must call this FIRST: the scroll stream
 * is throttled, so the last few hundred pixels before a click would otherwise
 * be filed under the URL the reader is leaving for.
 */
export function rememberScroll(): void {
  remember();
  persist();
}

/**
 * Land at the top for a navigation the reader has not seen before, and drop
 * any offset remembered for that URL from an earlier visit.
 */
export function scrollToTop(): void {
  cancelRestore?.();
  const key = entryKey();
  delete positions[key];
  ownedEntry = null;
  persist();
  window.scrollTo(0, 0);
}

/**
 * True when this module applied (or is applying) a saved offset for the
 * current history entry. Callers that reset scroll on their own — the
 * full-page entity route — must defer to it.
 */
export function scrollRestoreOwnsEntry(): boolean {
  return ownedEntry === entryKey();
}

/** Wire the listeners and restore the entry the document booted on. Returns
 * the teardown. Call once, from the shell. */
export function installScrollMemory(): () => void {
  readStored();
  // Native restoration runs before the async reads paint; this module runs
  // after the content exists.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  let queued: ReturnType<typeof setTimeout> | undefined;
  const onScroll = (): void => {
    // In-memory synchronously so a navigation in the same tick cannot file
    // this offset under the next URL; the sessionStorage write is throttled.
    remember();
    if (queued !== undefined) return;
    queued = setTimeout(() => {
      queued = undefined;
      persist();
    }, 120);
  };
  const onPopState = (): void => {
    restore(positions[entryKey()] ?? 0);
  };
  const onPageHide = (): void => {
    cancelRestore?.();
    remember();
    persist();
  };
  // A reader who starts scrolling has chosen their own position; a pending
  // restore would yank them away from it seconds later.
  const onReaderInput = (): void => cancelRestore?.();

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("popstate", onPopState);
  window.addEventListener("pagehide", onPageHide);
  window.addEventListener("wheel", onReaderInput, { passive: true });
  window.addEventListener("touchstart", onReaderInput, { passive: true });
  window.addEventListener("keydown", onReaderInput);

  restore(positions[entryKey()] ?? 0);

  return () => {
    cancelRestore?.();
    if (queued !== undefined) clearTimeout(queued);
    window.removeEventListener("scroll", onScroll);
    window.removeEventListener("popstate", onPopState);
    window.removeEventListener("pagehide", onPageHide);
    window.removeEventListener("wheel", onReaderInput);
    window.removeEventListener("touchstart", onReaderInput);
    window.removeEventListener("keydown", onReaderInput);
  };
}
