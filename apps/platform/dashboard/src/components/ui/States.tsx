// Loading / explicit-unavailable / empty states. The monolith NEVER renders
// sample data on an API failure: failures render *stated absence* (dashes,
// "unavailable" copy, the #banner strip) and never fabricated or
// stale-as-fresh data. The unavailable copy here is verbatim from the
// monolith (banner 2602–2605, setStatus 4113–4118).
import type { JSX } from "solid-js";

/** The status-pill text while the first load is in flight. */
export const CONNECTING_COPY = "Connecting…";
/** The status-pill text during a manual refresh. */
export const REFRESHING_COPY = "Refreshing…";
/** The status-pill text when the leaderboard fetch failed. */
export const DATA_UNAVAILABLE_COPY = "Data unavailable";

/**
 * The shell banner (monolith 2602–2605); shown iff the status mode is
 * "error" (setStatus toggles .show). The copy is the API-failure contract:
 * an explicit unavailable statement, never example data.
 */
export function UnavailableBanner(props: { show: boolean }): JSX.Element {
  return (
    <div
      id="banner"
      class="banner"
      classList={{ show: props.show }}
      role="status"
      aria-live="polite"
    >
      <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
        <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
        <path d="M12 9v4" />
        <path d="M12 17h.01" />
      </svg>
      <span id="banner-text">
        <b>Live data unavailable.</b> The leaderboard could not be loaded. No example data is shown.
        Try refreshing in a moment.
      </span>
    </div>
  );
}

/** A full-width table state row: `<td class="empty"><div class="empty-msg">`
 * (the loading / empty / unavailable cell used by every board table). */
export function EmptyRow(props: { colspan: number; children: JSX.Element }): JSX.Element {
  return (
    <tr>
      <td colspan={props.colspan} class="empty">
        <div class="empty-msg">{props.children}</div>
      </td>
    </tr>
  );
}

/**
 * A block-level section state (the `.harness-comparison-state`,
 * `.ath-state`, `.pipeline-detail-state` pattern): pass the monolith's class
 * and it appends " error" / " loading" for the styled variants.
 */
export function SectionState(props: {
  class: string;
  error?: boolean;
  loading?: boolean;
  children: JSX.Element;
}): JSX.Element {
  return (
    <div
      class={props.class + (props.error ? " error" : "") + (props.loading ? " loading" : "")}
      role="status"
    >
      {props.children}
    </div>
  );
}
