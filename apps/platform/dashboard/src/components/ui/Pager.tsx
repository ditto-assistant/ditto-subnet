// Shared pager: the board pager (id'd buttons, monolith 2701–2705) and the
// activity pager (data-activity-page buttons, 2880–2884) are the same markup
// with different addressing, so both are props here.
import type { JSX } from "solid-js";

export interface PagerProps {
  /** aria-label for the <nav>. */
  label: string;
  /** The "Page X of Y" status line (aria-live). */
  info: string;
  /** nav class; the board pager uses "pager bottom", default "pager". */
  class?: string;
  id?: string;
  prevId?: string;
  nextId?: string;
  infoId?: string;
  /** Address the buttons via data-activity-page="prev|next" (submissions). */
  activityData?: boolean;
  prevDisabled?: boolean;
  nextDisabled?: boolean;
  hidden?: boolean;
  onPrev: () => void;
  onNext: () => void;
}

export function Pager(props: PagerProps): JSX.Element {
  return (
    <nav
      class={props.class ?? "pager"}
      id={props.id}
      aria-label={props.label}
      hidden={props.hidden}
    >
      <button
        class="btn ghost"
        type="button"
        id={props.prevId}
        data-activity-page={props.activityData ? "prev" : undefined}
        disabled={props.prevDisabled}
        onClick={() => props.onPrev()}
      >
        <span aria-hidden="true">←</span> Previous
      </button>
      <span class="page-status" id={props.infoId} aria-live="polite">
        {props.info}
      </span>
      <button
        class="btn ghost"
        type="button"
        id={props.nextId}
        data-activity-page={props.activityData ? "next" : undefined}
        disabled={props.nextDisabled}
        onClick={() => props.onNext()}
      >
        Next <span aria-hidden="true">→</span>
      </button>
    </nav>
  );
}
