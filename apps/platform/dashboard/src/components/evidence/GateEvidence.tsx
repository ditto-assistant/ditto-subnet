// Bench v13+ gate verdict for one validator run (#1852). The scorer runs the
// v13 gates (catalog-present, claim-span provenance, causal answer_in_prompt,
// twin / counterfactual pair rule, shadow cost factor) in SHADOW first: it
// records what they would have done without changing the score. This panel
// shows that run-level verdict -- the folded posture, how many cases the gates
// would zero, the four gate summaries and per-finding counts -- so a miner sees
// a would-be zero before it enforces. Per-case notes are owner-only and live on
// the Account page (`/me/agents/{id}/gate-notes`).
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import type { GateEvidence, GatePosture } from "../../types/leaderboard";
import { StatusChip } from "../ui/StatusChip";

/** Human label for a closed-vocabulary gate finding; unknown findings echo their slug. */
export const GATE_NOTE_LABELS: Record<string, string> = {
  // Catalog-present gate.
  restraint_without_offer: "No-call credit without the tool offered",
  expected_tool_not_offered: "Expected tool never offered",
  swallowed_model_call: "Model-emitted call swallowed",
  semantic_preloading_safe_harbor: "Safe harbor: catalog offered",
  catalog_evidence_unavailable: "Catalog evidence unavailable",
  catalog_evidence_incomplete: "Catalog evidence incomplete",
  catalog_absent: "No tool catalog offered",
  catalog_gate_zeroed: "Catalog gate zeroed the case",
  memory_only_catalog: "Only memory tools offered",
  offer_inferred_from_execution: "Offer inferred from execution",
  catalog_present_lower_bound: "Catalog present (lower bound)",
  claim_attribution_uncorroborated: "Case attribution uncorroborated",
  no_model_completion: "No model completion",
  // Claim-span provenance + causal gate.
  served_text_not_model_emitted: "Served text not model-emitted",
  answer_in_prompt: "Answer present in harness prompt",
  claim_provenance_unavailable: "Claim provenance unavailable",
  claim_provenance_incomplete: "Claim provenance incomplete",
  claim_not_applicable: "No checkable claim",
  claim_provenance_zeroed: "Claim gate zeroed the case",
  // Twin post-pass.
  twin_concordant: "Decision twins answered concordantly",
  counterfactual_insensitive: "Counterfactual answered with the base answer",
};

/** Findings that zero the case when their gate runs in enforce. */
export const GATE_ZEROING_NOTES: ReadonlySet<string> = new Set([
  "restraint_without_offer",
  "expected_tool_not_offered",
  "swallowed_model_call",
  "catalog_gate_zeroed",
  "served_text_not_model_emitted",
  "answer_in_prompt",
  "claim_provenance_zeroed",
  "twin_concordant",
  "counterfactual_insensitive",
]);

export function gateNoteLabel(note: string): string {
  return GATE_NOTE_LABELS[note] ?? note.replace(/_/g, " ");
}

export function postureState(posture: GatePosture | null | undefined): readonly [string, string] {
  switch (posture) {
    case "enforce":
      return ["Enforced", "bad"];
    case "shadow":
    case "observe":
      return ["Shadow", "warn"];
    case "off":
      return ["Off", ""];
    default:
      return ["Posture unknown", ""];
  }
}

export function percentage(fraction: number): string {
  const percent = Math.max(0, Math.min(1, fraction)) * 100;
  return (percent > 0 && percent < 0.1 ? percent.toFixed(2) : percent.toFixed(1)) + "%";
}

function countRows(counts: Record<string, number> | undefined): Array<[string, number]> {
  return Object.entries(counts ?? {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** One line per gate the scorer reported a summary for. */
export function gateSummaryRows(
  evidence: GateEvidence,
): Array<{ gate: string; label: string; posture: GatePosture | null | undefined; detail: string }> {
  const rows: Array<{
    gate: string;
    label: string;
    posture: GatePosture | null | undefined;
    detail: string;
  }> = [];
  const catalog = evidence.catalog_gate;
  if (catalog) {
    const parts = [
      `${catalog.attributed_cases ?? 0}/${catalog.tool_cases ?? 0} tool cases attributed`,
    ];
    if (typeof catalog.catalog_suppression_rate === "number") {
      parts.push(`catalog suppressed on ${percentage(catalog.catalog_suppression_rate)}`);
    }
    if (catalog.zeroed_cases) parts.push(`${catalog.zeroed_cases} zeroed`);
    rows.push({
      gate: "catalog",
      label: "Catalog present",
      posture: catalog.posture,
      detail: parts.join(" · "),
    });
  }
  const claim = evidence.claim_provenance;
  if (claim) {
    const parts = [`${claim.settled_cases ?? 0}/${claim.memory_cases ?? 0} memory cases settled`];
    if (claim.answer_in_prompt_cases)
      parts.push(`${claim.answer_in_prompt_cases} answer-in-prompt`);
    if (claim.not_model_emitted_cases)
      parts.push(`${claim.not_model_emitted_cases} not model-emitted`);
    if (claim.zeroed_cases) parts.push(`${claim.zeroed_cases} zeroed`);
    rows.push({
      gate: "claim_provenance",
      label: "Claim provenance",
      posture: claim.posture,
      detail: parts.join(" · "),
    });
  }
  const twins = evidence.twin_post_pass;
  if (twins) {
    const parts = [
      `rule ${twins.rule ?? "?"}`,
      `${twins.twin_groups_concordant ?? 0}/${twins.twin_groups ?? 0} twin groups concordant`,
      `${twins.counterfactual_insensitive ?? 0}/${twins.counterfactual_pairs ?? 0} pairs counterfactual-insensitive`,
    ];
    if (twins.applied) parts.push("applied");
    rows.push({
      gate: "twin_post_pass",
      label: "Twin post-pass",
      posture: twins.posture,
      detail: parts.join(" · "),
    });
  }
  const cost = evidence.inference_cost;
  if (cost) {
    const parts = [
      `${cost.cases_below_full_factor ?? 0}/${cost.attributed_cases ?? 0} attributed cases discounted`,
    ];
    if (typeof cost.mean_factor_bps === "number") {
      parts.push(`mean factor ${(cost.mean_factor_bps / 10_000).toFixed(3)}`);
    }
    rows.push({
      gate: "inference_cost",
      label: "Cost factor",
      posture: cost.posture,
      detail: parts.join(" · "),
    });
  }
  return rows;
}

export function GateEvidencePanel(props: {
  evidence: GateEvidence | null | undefined;
}): JSX.Element {
  const evidence = () => props.evidence;
  const state = () => postureState(evidence()?.posture);
  const flagged = () => evidence()?.flagged_case_count ?? 0;
  return (
    <Show when={evidence()}>
      {(ev) => (
        <section
          class="v9-gate-evidence gate-evidence"
          data-gate-evidence
          aria-label={"Bench " + ev().bench_version + " gate verdict"}
        >
          <div class="v9-gate-heading">
            <strong>
              {"Bench " + ev().bench_version + " gates"}
              {ev().posture === "enforce" ? "" : " (shadow)"}
            </strong>
            <StatusChip label={state()[0]} tone={state()[1]} />
          </div>
          <div class="v9-gate-row" data-gate-row="flagged">
            <div class="v9-gate-metrics">
              <span>
                <b data-gate-metric="flagged" class={flagged() > 0 ? "danger" : "good"}>
                  {flagged()}
                </b>{" "}
                {ev().posture === "enforce"
                  ? "cases zeroed or discounted"
                  : "cases the gates would zero or discount"}
                <Show when={typeof ev().flagged_case_share === "number"}>
                  {" "}
                  (<b data-gate-metric="share">
                    {percentage(ev().flagged_case_share as number)}
                  </b>{" "}
                  of the run)
                </Show>
              </span>
              <Show when={typeof ev().catalog_suppression_rate === "number"}>
                <span>
                  catalog suppressed on{" "}
                  <b data-gate-metric="suppression">
                    {percentage(ev().catalog_suppression_rate as number)}
                  </b>{" "}
                  of tool cases
                </span>
              </Show>
            </div>
          </div>
          <Show when={gateSummaryRows(ev()).length}>
            <div class="v9-gate-row" data-gate-row="gates">
              <div class="v9-gate-metrics gate-evidence-gates">
                <For each={gateSummaryRows(ev())}>
                  {(row) => (
                    <span data-gate-summary={row.gate}>
                      {row.label}{" "}
                      <StatusChip
                        label={postureState(row.posture)[0]}
                        tone={postureState(row.posture)[1]}
                      />{" "}
                      <span class="muted">{row.detail}</span>
                    </span>
                  )}
                </For>
              </div>
            </div>
          </Show>
          <Show when={countRows(ev().gate_counts).length}>
            <div class="v9-gate-row" data-gate-row="counts">
              <div class="v9-gate-metrics">
                <For each={countRows(ev().gate_counts)}>
                  {([note, n]) => (
                    <span
                      data-gate-note={note}
                      data-gate-zeroing={GATE_ZEROING_NOTES.has(note) ? "true" : "false"}
                      title={note}
                    >
                      {gateNoteLabel(note)} <b>×{n}</b>
                    </span>
                  )}
                </For>
              </div>
            </div>
          </Show>
          <p>
            {ev().posture === "enforce"
              ? "These gates changed the score. "
              : "Shadow: these gates did not change the score; the flagged cases are what enforcing them would zero. "}
            Per-case notes are visible to the submitting hotkey on the Account page and can be cited
            in a dispute.
          </p>
        </section>
      )}
    </Show>
  );
}
