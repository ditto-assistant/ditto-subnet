// Tooltips: a single fixed #tip-bubble measured-then-placed and viewport-
// clamped (monolith IIFE 5201–5242), plus the screen-reader path — each tip
// gets a hidden #tipdesc-N description span in the #tip-descs host, wired via
// aria-describedby (wireTips 5244–5263; here per-Tip instead of a full-page
// rescan).
import { createEffect, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

let tipDescSeq = 0;

function descHost(): HTMLElement {
  let host = document.getElementById("tip-descs");
  if (!host) {
    host = document.createElement("div");
    host.id = "tip-descs";
    host.hidden = true;
    document.body.appendChild(host);
  }
  return host;
}

export interface TipProps {
  text: string;
  class?: string;
  children: JSX.Element;
}

/** `<span class="tip" tabindex="0" data-tooltip>` with its SR description. */
export function Tip(props: TipProps): JSX.Element {
  const id = "tipdesc-" + ++tipDescSeq;
  let span: HTMLSpanElement | null = null;
  onMount(() => {
    span = document.createElement("span");
    span.id = id;
    descHost().appendChild(span);
    onCleanup(() => {
      span?.remove();
      span = null;
    });
  });
  // The description has to FOLLOW the tooltip, not snapshot it at mount:
  // `data-tooltip` below is reactive, so a tip whose text changes after mount
  // (the epoch countdown gains its "projected" caveat once the chain read goes
  // stale) would show one thing on hover and announce another to a screen
  // reader. ChipTip already does this; Tip did not.
  createEffect(() => {
    const text = props.text;
    if (span) span.textContent = text;
  });
  return (
    <span
      class={"tip" + (props.class ? " " + props.class : "")}
      tabindex="0"
      data-tooltip={props.text}
      aria-describedby={id}
    >
      {props.children}
    </span>
  );
}

let tooltipsInstalled = false;

function findTip(target: EventTarget | null): Element | null {
  return target instanceof Element ? target.closest("[data-tooltip]") : null;
}

/**
 * Install the shared fixed bubble + document-level listeners. Returns a
 * teardown; a second call while installed is a no-op returning a no-op.
 */
export function installTooltips(): () => void {
  if (tooltipsInstalled) return () => undefined;
  tooltipsInstalled = true;
  const bubble = document.createElement("div");
  bubble.id = "tip-bubble";
  bubble.setAttribute("aria-hidden", "true");
  bubble.hidden = true;
  document.body.appendChild(bubble);
  let current: Element | null = null;
  function hide(): void {
    current = null;
    bubble.hidden = true;
  }
  function show(el: Element): void {
    const text = el.getAttribute("data-tooltip");
    if (!text) return;
    current = el;
    bubble.textContent = text;
    bubble.hidden = false;
    // Measure at a neutral position first, then place (clamped 8px inside
    // the viewport, flipped above when it would overflow the bottom).
    bubble.style.visibility = "hidden";
    bubble.style.left = "0px";
    bubble.style.top = "0px";
    const r = el.getBoundingClientRect();
    const b = bubble.getBoundingClientRect();
    const x = Math.min(Math.max(r.left, 8), window.innerWidth - b.width - 8);
    let y = r.bottom + 8;
    if (y + b.height > window.innerHeight - 8) y = r.top - b.height - 8;
    bubble.style.left = x + "px";
    bubble.style.top = Math.max(8, y) + "px";
    bubble.style.visibility = "";
  }
  const onHover = (ev: Event): void => {
    const el = findTip(ev.target);
    if (el) show(el);
    else if (current) hide();
  };
  // Any scroll shifts the trigger out from under the bubble; hiding beats
  // tracking (capture catches the inner scroll containers too).
  const onScroll = (): void => {
    if (current) hide();
  };
  document.addEventListener("mouseover", onHover);
  document.addEventListener("focusin", onHover);
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", hide);
  return () => {
    tooltipsInstalled = false;
    document.removeEventListener("mouseover", onHover);
    document.removeEventListener("focusin", onHover);
    document.removeEventListener("focusout", hide);
    document.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", hide);
    bubble.remove();
  };
}
