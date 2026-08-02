// The miner drawer's consensus block (monolith renderConsensus 6159–6212,
// compositeEquation 5861–5866, casesSection 6369–6398).
//
// The leaderboard row carries only the median composite, so the per-validator
// scores are fetched on demand from /public/agent/{id}/scores. What the block
// is FOR is the plural: three independent validators each scored this agent,
// the platform finalizes on the median, and every row shows its own equation
// and its own per-question results so a low number can be traced to the cases
// that produced it rather than taken on trust.
//
// Rows group under a per-benchmark-version heading (newest first). Composites
// compare only within a version, so a v6 and a v7 number in one flat list
// would read as comparable when they are not — and with more than one version
// present the endpoint's own median_composite would mix them, which is why the
// canonical row appears only for a single-version cohort.
import { For, Show, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { fx, shortKey } from "../../lib/format";
import { COMPOSITE_EQUATION_TITLE, compositeEquationText } from "../../lib/scoring";
import type { GlossaryPayload } from "../../types/bench";
import type { ConsensusScore, ScoresPayload } from "../../types/leaderboard";
import { Stat } from "../EntityPanel";
import { benchmarkVersionLabel } from "../pipeline/status";
import { CopyButton } from "../shell/CopyButton";
import { CasesSection } from "./Cases";
import { consensusCohortSummary, consensusCohorts } from "./cohorts";
import type { ConsensusCohort } from "./cohorts";

/** 6203–6205. */
const CONSENSUS_NOTE =
  "Independent validators that scored this agent; the platform finalizes on the median, so no " +
  "single validator decides the score.";
/** Appended when the agent carries scores from more than one version (6205). */
const CONSENSUS_MULTI_NOTE =
  "This agent was scored under more than one benchmark version; scores compare only within a " +
  "version, never across.";

/** One validator's row: elided hotkey with a copy control, its composite, the
 * inline equation that produced it, and its own per-question results
 * (6183–6187). */
function ConsensusRow(props: {
  score: ConsensusScore;
  glossary: () => GlossaryPayload | undefined;
}): JSX.Element {
  const hotkey = () => String(props.score.validator_hotkey || "");
  const equation = () => compositeEquationText(props.score.composite_breakdown);
  return (
    <div class="consensus-score">
      <div class="stat-row">
        <span class="k copyable" title={hotkey()}>
          <span>{shortKey(hotkey())}</span>
          <CopyButton value={hotkey()} label="validator hotkey" />
        </span>
        <span class="v">{fx(Number(props.score.composite))}</span>
      </div>
      <Show when={equation()}>
        {(text) => (
          <div class="score-calc-inline" title={COMPOSITE_EQUATION_TITLE}>
            {text()}
          </div>
        )}
      </Show>
      <CasesSection caseResults={props.score.case_results} glossary={props.glossary} />
    </div>
  );
}

/** One benchmark version's cohort: the heading with its own median, then a row
 * per validator (6188–6192). */
function Cohort(props: {
  cohort: ConsensusCohort;
  quorum: number;
  glossary: () => GlossaryPayload | undefined;
}): JSX.Element {
  return (
    <div class="benchmark-cohort">
      <div class="benchmark-cohort-heading">
        <h5>{benchmarkVersionLabel(props.cohort.key)}</h5>
        <span class="benchmark-cohort-summary">
          {consensusCohortSummary(props.cohort, props.quorum)}
        </span>
      </div>
      <For each={props.cohort.scores}>
        {(score) => <ConsensusRow score={score} glossary={props.glossary} />}
      </For>
    </div>
  );
}

export interface ConsensusProps {
  /** The board row's agent id. The block never renders without one — the
   * original returned early on a missing `e.agent_id` (6161). */
  agentId: string;
}

export function Consensus(props: ConsensusProps): JSX.Element {
  // Keyed on the agent id, so reopening the drawer for another miner refetches
  // and a slow response can never land in the wrong agent's block — what the
  // original's consensusToken guard did by hand (6157–6163).
  const scores = useEndpoint<ScoresPayload>(
    () => "/public/agent/" + encodeURIComponent(props.agentId) + "/scores",
  );
  // Reading data() on a rejected resource rethrows; an unreachable endpoint is
  // an absence of evidence here, not a page error.
  const payload = (): ScoresPayload | undefined => {
    try {
      return scores.error() ? undefined : scores.data();
    } catch {
      return undefined;
    }
  };
  const quorum = () => Math.max(1, Number(payload()?.quorum) || 3);
  const cohorts = createMemo(() => consensusCohorts(payload()?.scores || []));
  const multi = () => cohorts().length > 1;
  // With one version the endpoint's median IS that version's median; with
  // several it would mix incomparable composites, so the per-version medians
  // in the headings above are the only meaningful aggregates (6197–6200).
  const median = (): string | null => {
    const value = payload()?.median_composite;
    return !multi() && value != null ? fx(Number(value)) : null;
  };
  // Category names for the per-question rows; an effectively static payload.
  const glossary = useEndpoint<GlossaryPayload>("/public/bench/glossary");
  const glossaryData = (): GlossaryPayload | undefined => {
    try {
      return glossary.error() ? undefined : glossary.data();
    } catch {
      return undefined;
    }
  };
  return (
    <Show
      when={!scores.loading()}
      fallback={
        <p class="pipeline-detail-state loading" role="status">
          Loading validator scores…
        </p>
      }
    >
      <Show
        when={cohorts().length}
        fallback={
          <p class="pipeline-detail-state">
            No validator score has been published for this agent yet.
          </p>
        }
      >
        <div class="stat-group">
          <div class="stat-head">{"Consensus (k=" + quorum() + ")"}</div>
          <div class="muted" style={{ margin: "0 0 6px" }}>
            {CONSENSUS_NOTE}
            <Show when={multi()}>{" " + CONSENSUS_MULTI_NOTE}</Show>
          </div>
          <For each={cohorts()}>
            {(cohort) => <Cohort cohort={cohort} quorum={quorum()} glossary={glossaryData} />}
          </For>
          <Show when={median()}>{(value) => <Stat k="Median (canonical)" v={value()} />}</Show>
        </div>
      </Show>
    </Show>
  );
}
