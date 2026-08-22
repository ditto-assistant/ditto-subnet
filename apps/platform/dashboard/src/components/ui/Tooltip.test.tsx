// The screen-reader half of a tip is a separate DOM node in a shared host, so
// it can silently drift from what the tip actually says. Both drifts below were
// live defects: the epoch countdown's tooltip gains a "projected" caveat after
// mount, and the board renders plain `Tip`s and board chips on the same page.
import { cleanup, render } from "@solidjs/testing-library";
import { createSignal } from "solid-js";
import { afterEach, describe, expect, it } from "vitest";

import { QualityGateChip } from "../board/chips";
import { Tip, TipTarget } from "./Tooltip";

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

  it("mints description ids from the same counter as every other tip", () => {
    // `Tip` and the board's chip trigger used to be two components, each with
    // its own module-local counter starting at 0 and both minting `tipdesc-N`.
    // Any page rendering both emitted duplicate DOM ids, and aria-describedby
    // resolves to whichever span the document holds first — so a tip announced
    // some unrelated element's text. They are one component now; this asserts
    // the property that made the collision impossible, rather than that two
    // particular renders happen not to collide (which passes vacuously
    // whenever the two counters are out of step for unrelated reasons).
    render(() => (
      <>
        <TipTarget text="chip description">chip</TipTarget>
        <Tip text="tip description">tip</Tip>
        <TipTarget text="third description">third</TipTarget>
      </>
    ));
    const triggers = Array.from(document.querySelectorAll("[data-tooltip]"));
    const ids = triggers.map((el) => el.getAttribute("aria-describedby") ?? "");

    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
    for (const id of ids) expect(document.querySelectorAll("[id='" + id + "']")).toHaveLength(1);
    expect(triggers.map(descriptionOf)).toEqual([
      "chip description",
      "tip description",
      "third description",
    ]);
  });

  it("wires a board chip's description through the same host", () => {
    // Guards the seam the consolidation created: chips.tsx no longer owns a
    // descHost/minter of its own, so a chip must still land a resolvable
    // description span in #tip-descs.
    const { container } = render(() => (
      <QualityGateChip
        entry={
          {
            composite_breakdown: { benchmark_quality_multiplier: 0.5, base_accuracy: 0.8 },
          } as never
        }
      />
    ));
    const chip = container.querySelector("[data-tooltip]");
    const id = chip?.getAttribute("aria-describedby") ?? "";

    expect(id).toMatch(/^tipdesc-\d+$/);
    expect(document.querySelector("#tip-descs")?.contains(document.getElementById(id))).toBe(true);
    expect(descriptionOf(chip)).toBe(chip?.getAttribute("data-tooltip"));
  });
});
