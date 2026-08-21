import type { JSX } from "solid-js";

import type {
  V9AuthoritativeToolGate,
  V9BaseEvidence,
  V9GateResult,
  V9ModelUseGate,
} from "../../types/leaderboard";
import { StatusChip } from "../ui/StatusChip";

function percentage(basisPoints: number): string {
  const percent = Math.max(0, Math.min(10_000, basisPoints)) / 100;
  return (percent > 0 && percent < 0.1 ? percent.toFixed(2) : percent.toFixed(1)) + "%";
}

function resultState(result: V9GateResult): readonly [string, string] {
  switch (result) {
    case "passed":
      return ["Passed", "good"];
    case "not_applicable":
      return ["Not applicable", ""];
    case "below_threshold":
      return ["Below threshold", "bad"];
    case "zero_inference":
      return ["No inference observed", "bad"];
    case "insufficient_evidence":
      return ["Insufficient evidence", "bad"];
  }
}

function ModelUseRow(props: { gate: V9ModelUseGate }): JSX.Element {
  const state = () => resultState(props.gate.result);
  return (
    <div class="v9-gate-row" data-v9-gate="model-use">
      <div class="v9-gate-title">
        <strong>Model use</strong>
        <StatusChip label={state()[0]} tone={state()[1]} />
      </div>
      <div class="v9-gate-metrics">
        <span>
          Coverage {percentage(props.gate.coverage_bps)} · threshold{" "}
          {percentage(props.gate.threshold_bps)}
        </span>
        <span>
          {props.gate.successful_inference_cases} of {props.gate.eligible_cases} eligible cases
          covered by successful inference requests · {props.gate.missing_inference_cases} uncovered
        </span>
        <span>
          {props.gate.successful_requests} of {props.gate.observed_requests} relay requests
          succeeded
        </span>
      </div>
    </div>
  );
}

function ToolRow(props: { gate: V9AuthoritativeToolGate }): JSX.Element {
  const state = () => resultState(props.gate.result);
  return (
    <div class="v9-gate-row" data-v9-gate="authoritative-tool">
      <div class="v9-gate-title">
        <strong>Authoritative tool use</strong>
        <StatusChip label={state()[0]} tone={state()[1]} />
      </div>
      <div class="v9-gate-metrics">
        <span>
          Coverage {percentage(props.gate.coverage_bps)} · threshold{" "}
          {percentage(props.gate.threshold_bps)}
        </span>
        <span>
          {props.gate.matched_executions} of {props.gate.expected_executions} expected executions
          matched · {props.gate.missing_executions} missing
        </span>
        <span>
          {props.gate.observed_executions} observed · {props.gate.unexpected_executions} unexpected
        </span>
      </div>
    </div>
  );
}

export function V9GateEvidence(props: { evidence: V9BaseEvidence }): JSX.Element {
  const gates = () => props.evidence.score_gates;
  return (
    <section
      class="v9-gate-evidence"
      aria-label={"Bench " + props.evidence.bench_version + " trusted score gates"}
    >
      <div class="v9-gate-heading">
        <strong>{"Trusted Bench " + props.evidence.bench_version + " score gates"}</strong>
        <span>{gates().rollout_mode === "enforce" ? "Enforced" : "Shadow"}</span>
      </div>
      <ModelUseRow gate={gates().model_use} />
      <ToolRow gate={gates().authoritative_tool} />
      <p>
        Counts come from trusted relay and tool-server evidence. Prompts, responses, and answer
        content are not included.
      </p>
    </section>
  );
}
