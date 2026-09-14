// Bench v13+ gate verdict for one validator run (#1852). The scorer runs the
// v13 gates (catalog-present, claim-span provenance, causal answer_in_prompt,
// twin / counterfactual pair rule, shadow cost factor) in SHADOW first: it
// reports what they would have done beside the ungated composite without
// changing the score. This panel shows that run-level verdict -- posture,
// composite with/without gates, the gate-induced loss, per-gate counts -- so a
// miner sees a would-be zero before it enforces. Per-case notes are owner-only
// and live on the Account page (`/me/agents/{id}/gate-notes`).
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { fx } from "../../lib/format";
import type { GateEvidence } from "../../types/leaderboard";
import { StatusChip } from "../ui/StatusChip";

/** Human label for a closed-vocabulary gate note; unknown notes echo their id. */
export const GATE_NOTE_LABELS: Record<string, string> = {
  restraint_without_offer: "No-call credit without the tool offered",
  expected_tool_not_offered: "Expected tool never offered",
  swallowed_model_call: "Model-emitted call swallowed",
  served_text_not_model_emitted: "Served text not model-emitted",
  slot_not_in_prose: "Slot value not in prose",
  answer_in_prompt: "Answer present in harness prompt",
  concordant_zero: "Twin pair concordant (zeroed)",
  pair_product: "Twin pair product",
  counterfactual_insensitive: "Counterfactual-insensitive pair",
  cost_factor_shadow: "Cost factor (shadow)",
};

export function gateNoteLabel(note: string): string {
  return GATE_NOTE_LABELS[note] ?? note.replace(/_/g, " ");
}

export function postureState(posture: GateEvidence["posture"]): readonly [string, string] {
  switch (posture) {
    case "enforce":
      return ["Enforced", "bad"];
    case "shadow":
      return ["Shadow", "warn"];
    case "off":
      return ["Off", ""];
    default:
      return ["Posture unknown", ""];
  }
}

function percentage(fraction: number): string {
  const percent = Math.max(0, Math.min(1, fraction)) * 100;
  return (percent > 0 && percent < 0.1 ? percent.toFixed(2) : percent.toFixed(1)) + "%";
}

function countRows(counts: Record<string, number> | undefined): Array<[string, number]> {
  return Object.entries(counts ?? {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export function GateEvidencePanel(props: {
  evidence: GateEvidence | null | undefined;
}): JSX.Element {
  const evidence = () => props.evidence;
  const state = () => postureState(evidence()?.posture);
  const loss = () => evidence()?.gate_induced_loss;
  const hasLoss = () => typeof loss() === "number";
  return (
    <Show when={evidence()}>
      {(ev) => (
        <section
          class="v9-gate-evidence gate-evidence"
          data-gate-evidence
          aria-label={"Bench " + ev().bench_version + " gate-induced loss"}
        >
          <div class="v9-gate-heading">
            <strong>
              {"Bench " + ev().bench_version + " gates · gate-induced loss"}
              {ev().posture === "enforce" ? "" : " (shadow)"}
            </strong>
            <StatusChip label={state()[0]} tone={state()[1]} />
          </div>
          <div class="v9-gate-row" data-gate-row="loss">
            <div class="v9-gate-metrics">
              <span>
                Composite without gates{" "}
                <b data-gate-metric="without">
                  {typeof ev().composite_without_gates === "number"
                    ? fx(ev().composite_without_gates as number)
                    : "—"}
                </b>
              </span>
              <span>
                with gates{" "}
                <b data-gate-metric="with">
                  {typeof ev().composite_with_gates === "number"
                    ? fx(ev().composite_with_gates as number)
                    : "—"}
                </b>
              </span>
              <span>
                loss{" "}
                <b
                  data-gate-metric="loss"
                  class={hasLoss() && (loss() as number) > 0 ? "danger" : "good"}
                >
                  {hasLoss() ? fx(loss() as number) : "—"}
                </b>
              </span>
              <Show when={typeof ev().catalog_suppression_rate === "number"}>
                <span>
                  catalog suppressed on{" "}
                  <b data-gate-metric="suppression">
                    {percentage(ev().catalog_suppression_rate as number)}
                  </b>{" "}
                  of deciding turns
                </span>
              </Show>
              <span>
                <b data-gate-metric="flagged">{ev().flagged_case_count ?? 0}</b> flagged cases
              </span>
            </div>
          </div>
          <Show when={countRows(ev().gate_counts).length}>
            <div class="v9-gate-row" data-gate-row="counts">
              <div class="v9-gate-metrics">
                <For each={countRows(ev().gate_counts)}>
                  {([note, n]) => (
                    <span data-gate-note={note} title={note}>
                      {gateNoteLabel(note)} <b>×{n}</b>
                    </span>
                  )}
                </For>
              </div>
            </div>
          </Show>
          <Show when={countRows(ev().relation_outcome_counts).length}>
            <div class="v9-gate-row" data-gate-row="relations">
              <div class="v9-gate-metrics">
                <For each={countRows(ev().relation_outcome_counts)}>
                  {([outcome, n]) => (
                    <span data-gate-relation={outcome}>
                      {gateNoteLabel(outcome)} <b>×{n}</b>
                    </span>
                  )}
                </For>
              </div>
            </div>
          </Show>
          <p>
            {ev().posture === "enforce"
              ? "These gates changed the score. "
              : "Shadow: these gates did not change the score; the loss is what enforcing them would take. "}
            Per-case notes are visible to the submitting hotkey on the Account page and can be cited
            in a dispute.
          </p>
        </section>
      )}
    </Show>
  );
}
