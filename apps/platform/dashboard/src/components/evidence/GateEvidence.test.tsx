import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { GateEvidence } from "../../types/leaderboard";
import {
  GATE_ZEROING_NOTES,
  GateEvidencePanel,
  gateNoteLabel,
  gateSummaryRows,
  percentage,
  postureState,
} from "./GateEvidence";

afterEach(cleanup);

// The projection of the captured v13 scorer report the Platform tests pin
// (apps/platform/ditto/tests/api_server/endpoints/test_gate_notes.py).
const shadow: GateEvidence = {
  bench_version: 13,
  posture: "shadow",
  catalog_gate: {
    posture: "shadow",
    tool_cases: 2,
    attributed_cases: 2,
    catalog_absent_cases: 1,
    catalog_suppression_rate: 0.5,
    restraint_without_offer: 1,
    swallowed_model_call: 1,
    zeroed_cases: 0,
    attribution_coverage_bps: 10000,
  },
  claim_provenance: {
    posture: "shadow",
    memory_cases: 3,
    attributed_cases: 3,
    applicable_cases: 2,
    settled_cases: 2,
    answer_in_prompt_cases: 1,
    zeroed_cases: 0,
  },
  twin_post_pass: {
    posture: "observe",
    rule_requested: "concordant_zero",
    rule: "concordant_zero",
    twin_groups: 1,
    twin_groups_concordant: 1,
    counterfactual_pairs: 1,
    counterfactual_insensitive: 1,
    cases_affected: 3,
    cases_affected_share: 0.6,
    applied: false,
  },
  inference_cost: {
    posture: "shadow",
    applied: false,
    cases: 5,
    attributed_cases: 5,
    cases_below_full_factor: 1,
    mean_factor_bps: 9733,
  },
  catalog_suppression_rate: 0.5,
  flagged_case_count: 4,
  flagged_case_share: 0.666667,
  gate_counts: {
    answer_in_prompt: 1,
    catalog_absent: 1,
    claim_not_applicable: 1,
    counterfactual_insensitive: 2,
    restraint_without_offer: 1,
    swallowed_model_call: 1,
    twin_concordant: 1,
  },
};

function panel(): HTMLElement | null {
  return document.querySelector("[data-gate-evidence]");
}

describe("GateEvidencePanel (#1852 shadow verdict)", () => {
  it("renders nothing without evidence", () => {
    render(() => <GateEvidencePanel evidence={null} />);
    expect(panel()).toBeNull();
    cleanup();
    render(() => <GateEvidencePanel evidence={undefined} />);
    expect(panel()).toBeNull();
  });

  it("shows the flagged cases, their share and the catalog suppression rate", () => {
    render(() => <GateEvidencePanel evidence={shadow} />);
    const el = panel();
    expect(el).not.toBeNull();
    expect(el?.getAttribute("aria-label")).toBe("Bench 13 gate verdict");
    expect(el?.textContent).toContain("(shadow)");
    const flagged = el?.querySelector('[data-gate-metric="flagged"]');
    expect(flagged?.textContent).toBe("4");
    expect(flagged?.className).toBe("danger");
    expect(el?.textContent).toContain("cases the gates would zero or discount");
    expect(el?.querySelector('[data-gate-metric="share"]')?.textContent).toBe("66.7%");
    expect(el?.querySelector('[data-gate-metric="suppression"]')?.textContent).toBe("50.0%");
    expect(el?.querySelector(".stage")?.textContent).toBe("Shadow");
    // Shadow copy says the score did not move.
    expect(el?.textContent).toContain("did not change the score");
  });

  it("lists one line per gate summary with its own posture", () => {
    render(() => <GateEvidencePanel evidence={shadow} />);
    const gates = Array.from(document.querySelectorAll("[data-gate-summary]")).map((node) =>
      node.getAttribute("data-gate-summary"),
    );
    expect(gates).toEqual(["catalog", "claim_provenance", "twin_post_pass", "inference_cost"]);
    const twins = document.querySelector('[data-gate-summary="twin_post_pass"]');
    // `observe` is the twin pass's name for shadow.
    expect(twins?.querySelector(".stage")?.textContent).toBe("Shadow");
    expect(twins?.textContent).toContain("rule concordant_zero");
    expect(twins?.textContent).toContain("1/1 twin groups concordant");
    expect(document.querySelector('[data-gate-summary="inference_cost"]')?.textContent).toContain(
      "mean factor 0.973",
    );
    expect(document.querySelector('[data-gate-summary="catalog"]')?.textContent).toContain(
      "catalog suppressed on 50.0%",
    );
  });

  it("lists per-finding counts with human labels, most frequent first, marking would-be zeros", () => {
    render(() => <GateEvidencePanel evidence={shadow} />);
    const notes = Array.from(document.querySelectorAll("[data-gate-note]")).map((node) =>
      node.getAttribute("data-gate-note"),
    );
    expect(notes[0]).toBe("counterfactual_insensitive");
    expect(notes).toHaveLength(7);
    const answer = document.querySelector('[data-gate-note="answer_in_prompt"]');
    expect(answer?.textContent).toBe("Answer present in harness prompt ×1");
    expect(answer?.getAttribute("data-gate-zeroing")).toBe("true");
    expect(
      document
        .querySelector('[data-gate-note="claim_not_applicable"]')
        ?.getAttribute("data-gate-zeroing"),
    ).toBe("false");
    expect(document.querySelector('[data-gate-note="twin_concordant"]')?.textContent).toBe(
      "Decision twins answered concordantly ×1",
    );
  });

  it("names an enforced posture and drops the shadow qualifier", () => {
    render(() => (
      <GateEvidencePanel
        evidence={{ ...shadow, posture: "enforce", flagged_case_count: 0, gate_counts: {} }}
      />
    ));
    const el = panel();
    expect(el?.textContent).not.toContain("(shadow)");
    expect(el?.querySelector(".stage")?.textContent).toBe("Enforced");
    expect(el?.querySelector('[data-gate-metric="flagged"]')?.className).toBe("good");
    expect(el?.textContent).toContain("cases zeroed or discounted");
    expect(el?.querySelector('[data-gate-row="counts"]')).toBeNull();
  });

  it("renders a bare verdict without summaries", () => {
    render(() => (
      <GateEvidencePanel evidence={{ bench_version: 14, posture: null, flagged_case_count: 1 }} />
    ));
    const el = panel();
    expect(el?.querySelector('[data-gate-metric="share"]')).toBeNull();
    expect(el?.querySelector('[data-gate-row="gates"]')).toBeNull();
    expect(el?.querySelector(".stage")?.textContent).toBe("Posture unknown");
  });

  it("labels the closed vocabulary and falls back to the slug", () => {
    expect(gateNoteLabel("swallowed_model_call")).toBe("Model-emitted call swallowed");
    expect(gateNoteLabel("some_future_gate")).toBe("some future gate");
    expect(postureState("shadow")).toEqual(["Shadow", "warn"]);
    expect(postureState("observe")).toEqual(["Shadow", "warn"]);
    expect(postureState(undefined)).toEqual(["Posture unknown", ""]);
    expect(percentage(0.0005)).toBe("0.05%");
    expect(GATE_ZEROING_NOTES.has("twin_concordant")).toBe(true);
    expect(GATE_ZEROING_NOTES.has("catalog_absent")).toBe(false);
    expect(gateSummaryRows({ bench_version: 13 })).toEqual([]);
  });
});
