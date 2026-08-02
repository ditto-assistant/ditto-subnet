// Entity link construction on top of the router primitives. Entity params
// live in the hash query (the real query carries config knobs only);
// drilldowns are overlays over the current page, and ENTITY_PAGES is only
// the cold-link fallback when no page route is present. Ports of the
// original's entityAnchor (monolith 3234–3238), canonicalEntityUrl
// (6063–6065) and configureEntityActions (3297–3304); the router half
// (entityHref / fullEntityHref / readEntityRoute) lives in lib/router and is
// reused here, not duplicated.

import {
  ENTITY_PAGES,
  ENTITY_PATHS,
  currentPageName,
  dashboardHref,
  entityHref,
  fullEntityHref,
} from "./router";
import type { EntityKind } from "./router";

/** Attributes for an entity link `<a>`; label is its text content. */
export interface EntityAnchorAttrs {
  class: string;
  href: string;
  "data-entity-link": EntityKind;
  label: string;
}

/**
 * The entity anchor contract (3234–3238): class "entity-link" (+ optional
 * extra class), overlay href from entityHref (keeps the page under it and
 * its hash state), a data-entity-link="{kind}" marker for the document-level
 * click intercept, and the identifier as the label fallback. Null when there
 * is no identifier — the original rendered the bare label text instead of a
 * link, and callers do the same.
 */
export function entityAnchorAttrs(
  kind: EntityKind,
  identifier: string | null | undefined,
  label?: string | null,
  className?: string | null,
): EntityAnchorAttrs | null {
  if (!identifier) return null;
  return {
    class: "entity-link" + (className ? " " + className : ""),
    href: entityHref(kind, String(identifier)),
    "data-entity-link": kind,
    label: label || String(identifier),
  };
}

/**
 * Absolute URL of an entity's dedicated page (6063–6065):
 *   new URL(fullEntityHref(kind, identifier), location.href).href
 * — what the review packet and share/copy affordances quote.
 */
export function canonicalEntityUrl(kind: EntityKind, identifier: string): string {
  return new URL(fullEntityHref(kind, identifier), location.href).href;
}

/** Full-page routes exist only for agents and miners (3300–3301); validator
 * and screener detail is overlay-only. */
export function hasFullEntityPage(kind: EntityKind): boolean {
  return kind === "agent" || kind === "miner";
}

/**
 * The modal's "Back to dashboard" target (3303): the page currently under
 * the overlay, falling back through ENTITY_PAGES for cold links with no
 * page route.
 */
export function entityBackHref(kind: EntityKind): string {
  const plural = ENTITY_PATHS[kind] || kind;
  return dashboardHref(currentPageName() ?? ENTITY_PAGES[plural] ?? "overview");
}

export interface EntityActions {
  /** href for "Open full page", or null when the kind has no full route. */
  openFullHref: string | null;
  /** href for "Back to dashboard". */
  backHref: string;
}

/** Port of configureEntityActions (3297–3304) as data for the modal shell. */
export function entityActions(kind: EntityKind, identifier: string): EntityActions {
  return {
    openFullHref: hasFullEntityPage(kind) ? fullEntityHref(kind, identifier) : null,
    backHref: entityBackHref(kind),
  };
}
