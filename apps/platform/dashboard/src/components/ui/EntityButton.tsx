// Entity anchor widget: the `<a data-entity-link>` contract (monolith
// entityAnchor 3234–3238 + the document-level click intercept 6449–6457).
// Plain left clicks push the overlay route; modified clicks keep native
// navigation. With no identifier the original rendered the bare label text,
// and so does this.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import { entityAnchorAttrs } from "../../lib/entity-links";
import type { EntityKind } from "../../lib/router";
import { pushEntityRoute } from "../../stores/routeStore";

export interface EntityButtonProps {
  kind: EntityKind;
  id: string | null | undefined;
  label?: string | null;
  class?: string | null;
  /** Hover hint naming what the link opens (e.g. "Open the miner profile") —
   * the two board click targets look alike and route differently. */
  title?: string | null;
  /** Custom anchor content; defaults to the label (or the identifier). */
  children?: JSX.Element;
}

export function EntityButton(props: EntityButtonProps): JSX.Element {
  const attrs = () =>
    entityAnchorAttrs(props.kind, props.id, props.label ?? null, props.class ?? null);
  function onClick(ev: MouseEvent): void {
    if (
      ev.defaultPrevented ||
      ev.button !== 0 ||
      ev.metaKey ||
      ev.ctrlKey ||
      ev.shiftKey ||
      ev.altKey
    ) {
      return;
    }
    ev.preventDefault();
    const a = attrs();
    if (!a || !props.id) return;
    if (location.pathname + location.search + location.hash === a.href) return;
    pushEntityRoute(props.kind, String(props.id));
  }
  return (
    <Show when={attrs()} fallback={<>{props.children ?? props.label ?? ""}</>}>
      {(a) => (
        <a
          class={a().class}
          href={a().href}
          title={props.title ?? undefined}
          data-entity-link={a()["data-entity-link"]}
          onClick={onClick}
        >
          {props.children ?? a().label}
        </a>
      )}
    </Show>
  );
}
