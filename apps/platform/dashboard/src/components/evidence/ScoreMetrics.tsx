import type { JSX } from "solid-js";

import { fx } from "../../lib/format";
import type { V9BaseEvidence } from "../../types/leaderboard";
import type { PublishedRunScore } from "./run-score";

export function ScoreMetrics(props: {
  score?: PublishedRunScore | null;
  composite?: number | null;
  v9Base?: V9BaseEvidence | null;
}): JSX.Element {
  const value = (n: number | null | undefined) =>
    typeof n === "number" && Number.isFinite(n) ? fx(n) : "—";
  const trustedGates = () => props.score?.v9_base ?? props.v9Base;
  const passed = () => {
    const gates = trustedGates()?.score_gates;
    if (!gates) return null;
    return (
      Number(gates.model_use.result === "passed") +
      Number(gates.authoritative_tool.result === "passed")
    );
  };

  return (
    <dl class="score-metrics" aria-label="Run score metrics">
      <div>
        <dt>Tool</dt>
        <dd>{value(props.score?.tool_mean)}</dd>
      </div>
      <div>
        <dt>Memory</dt>
        <dd>{value(props.score?.memory_mean)}</dd>
      </div>
      <div title="Trusted model-use and authoritative-tool gate verdicts; see detailed evidence below.">
        <dt>Gates</dt>
        <dd>
          {passed() == null ? "—" : `${passed()}/2 pass`}
          {trustedGates()?.score_gates.rollout_mode === "shadow" ? <small>shadow</small> : null}
        </dd>
      </div>
      <div>
        <dt>Composite</dt>
        <dd>{value(props.score?.composite ?? props.composite)}</dd>
      </div>
    </dl>
  );
}
