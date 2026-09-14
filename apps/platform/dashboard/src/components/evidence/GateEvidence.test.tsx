import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { GateEvidence } from "../../types/leaderboard";
import { GateEvidencePanel, gateNoteLabel, postureState } from "./GateEvidence";

afterEach(cleanup);

const shadow: GateEvidence = {
  bench_version: 13,
  posture: "shadow",
  composite_with_gates: 0.61,
  composite_without_gates: 0.87,
  gate_induced_loss: 0.26,
  catalog_suppression_rate: 0.02,
  flagged_case_count: 3,
  gate_counts: { answer_in_prompt: 1, restraint_without_offer: 2 },
  relation_outcome_counts: { concordant_zero: 1 },
};

function panel(): HTMLElement | null {
  return document.querySelector("[data-gate-evidence]");
}

describe("GateEvidencePanel (#1852 gate-induced loss)", () => {
  it("renders nothing without evidence", () => {
    render(() => <GateEvidencePanel evidence={null} />);
    expect(panel()).toBeNull();
    cleanup();
    render(() => <GateEvidencePanel evidence={undefined} />);
    expect(panel()).toBeNull();
  });

  it("shows the shadow verdict as composite with/without gates and the loss", () => {
    render(() => <GateEvidencePanel evidence={shadow} />);
    const el = panel();
    expect(el).not.toBeNull();
    expect(el?.getAttribute("aria-label")).toBe("Bench 13 gate-induced loss");
    expect(el?.textContent).toContain("(shadow)");
    expect(el?.querySelector('[data-gate-metric="without"]')?.textContent).toBe("0.870");
    expect(el?.querySelector('[data-gate-metric="with"]')?.textContent).toBe("0.610");
    const loss = el?.querySelector('[data-gate-metric="loss"]');
    expect(loss?.textContent).toBe("0.260");
    expect(loss?.className).toBe("danger");
    expect(el?.querySelector('[data-gate-metric="suppression"]')?.textContent).toBe("2.0%");
    expect(el?.querySelector('[data-gate-metric="flagged"]')?.textContent).toBe("3");
    expect(el?.querySelector(".stage")?.textContent).toBe("Shadow");
    // Shadow copy says the score did not move.
    expect(el?.textContent).toContain("did not change the score");
  });

  it("lists per-gate counts with human labels, most frequent first", () => {
    render(() => <GateEvidencePanel evidence={shadow} />);
    const notes = Array.from(document.querySelectorAll("[data-gate-note]")).map((node) =>
      node.getAttribute("data-gate-note"),
    );
    expect(notes).toEqual(["restraint_without_offer", "answer_in_prompt"]);
    expect(document.querySelector('[data-gate-note="answer_in_prompt"]')?.textContent).toBe(
      "Answer present in harness prompt ×1",
    );
    expect(document.querySelector('[data-gate-relation="concordant_zero"]')?.textContent).toBe(
      "Twin pair concordant (zeroed) ×1",
    );
  });

  it("names an enforced posture and drops the shadow qualifier", () => {
    render(() => (
      <GateEvidencePanel
        evidence={{ ...shadow, posture: "enforce", gate_induced_loss: 0, gate_counts: {} }}
      />
    ));
    const el = panel();
    expect(el?.textContent).not.toContain("(shadow)");
    expect(el?.querySelector(".stage")?.textContent).toBe("Enforced");
    expect(el?.querySelector('[data-gate-metric="loss"]')?.className).toBe("good");
    expect(el?.querySelector('[data-gate-row="counts"]')).toBeNull();
  });

  it("renders dashes for a verdict without composites", () => {
    render(() => <GateEvidencePanel evidence={{ bench_version: 14, posture: null }} />);
    const el = panel();
    expect(el?.querySelector('[data-gate-metric="loss"]')?.textContent).toBe("—");
    expect(el?.querySelector(".stage")?.textContent).toBe("Posture unknown");
  });

  it("labels the closed vocabulary and falls back to the slug", () => {
    expect(gateNoteLabel("swallowed_model_call")).toBe("Model-emitted call swallowed");
    expect(gateNoteLabel("some_future_gate")).toBe("some future gate");
    expect(postureState("shadow")).toEqual(["Shadow", "warn"]);
    expect(postureState(undefined)).toEqual(["Posture unknown", ""]);
  });
});
