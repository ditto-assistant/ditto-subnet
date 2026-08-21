// The screen-reader half of a tip is a separate DOM node in a shared host, so
// it can silently drift from what the tip actually says. Both drifts below were
// live defects: the epoch countdown's tooltip gains a "projected" caveat after
// mount, and the board renders `Tip` and `ChipTip` on the same page.
import { cleanup, render } from "@solidjs/testing-library";
import { createSignal } from "solid-js";
import { afterEach, describe, expect, it } from "vitest";

import { ChipTip } from "../board/chips";
import { Tip } from "./Tooltip";

afterEach(() => {
  cleanup();
  document.getElementById("tip-descs")?.remove();
});

function descriptionOf(trigger: Element | null | undefined): string | null {
  const id = trigger?.getAttribute("aria-describedby");
  return id ? (document.getElementById(id)?.textContent ?? null) : null;
}

describe("Tip", () => {
  it("keeps the SR description in step with the visible tooltip", () => {
    // Mounting-time snapshot was the bug: `data-tooltip` is reactive, so a tip
    // whose text changes later showed one thing on hover and announced another.
    const [text, setText] = createSignal("Folds in 3:20.");
    const { container } = render(() => <Tip text={text()}>clock</Tip>);
    const trigger = container.querySelector("[data-tooltip]");

    expect(descriptionOf(trigger)).toBe("Folds in 3:20.");

    setText("Folds in 3:20. The tick shown is projected.");

    expect(trigger).toHaveAttribute("data-tooltip", "Folds in 3:20. The tick shown is projected.");
    expect(descriptionOf(trigger)).toBe("Folds in 3:20. The tick shown is projected.");
  });

  it("does not share a description-id namespace with ChipTip", () => {
    // Both mint ids from their OWN module counter, each starting at 0. While
    // they also shared the `tipdesc-` prefix, any page rendering both emitted
    // duplicate DOM ids and aria-describedby resolved to whichever span came
    // first — so a tip announced some unrelated element's text.
    //
    // Asserted on the prefixes, not on one render's ids: whether two given
    // renders happen to collide depends on how many tips of each kind mounted
    // earlier, so an id comparison passes vacuously as often as not. Distinct
    // namespaces is the property that actually holds regardless of order.
    const { container } = render(() => (
      <>
        <ChipTip text="chip description">chip</ChipTip>
        <Tip text="tip description">tip</Tip>
      </>
    ));
    const [chip, tip] = Array.from(container.querySelectorAll("[data-tooltip]"));
    const namespace = (el: Element | undefined): string =>
      (el?.getAttribute("aria-describedby") ?? "").replace(/\d+$/, "");

    expect(namespace(chip)).not.toBe("");
    expect(namespace(chip)).not.toBe(namespace(tip));
    expect(descriptionOf(chip)).toBe("chip description");
    expect(descriptionOf(tip)).toBe("tip description");

    const ids = Array.from(document.querySelectorAll("#tip-descs > span")).map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
