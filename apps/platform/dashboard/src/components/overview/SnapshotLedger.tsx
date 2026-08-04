// The subnet snapshot ledger in the overview rail (markup 2629–2642 +
// renderHealth 6756–6768 + the headline-stat half of render() 4817–4830 +
// the h-validators fill from loadOperations 9443). API failures render
// stated absence — every value falls back to an en dash, never sample data.
import { Show, createMemo, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { operationsResource } from "../../data/operations";
import { REFRESH_MS } from "../../lib/config";
import type { ResourceState } from "../../data/useEndpoint";
import { fx, median, relTime } from "../../lib/format";
import { displayComposite, isEligible, isFinalized } from "../../lib/scoring";
import type { HealthPayload, OperationsPayload } from "../../types";
import { Tip } from "../ui/Tooltip";
import type { LeaderboardStore } from "../board/leaderboard-data";

function latest<T>(resource: ResourceState<T>): T | undefined {
  if (resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

export function SnapshotLedger(props: {
  store: LeaderboardStore;
  operations?: ResourceState<OperationsPayload>;
}): JSX.Element {
  const store = props.store;
  const health = useEndpoint<HealthPayload>("/public/health", { pollMs: REFRESH_MS });
  const operations = props.operations ?? operationsResource();
  if (!props.operations) onMount(() => operations.refresh());

  const h = (): HealthPayload | undefined => latest(health);

  // Finalized submissions head the field; the headline stats fall back to
  // the provisional pool only when nothing is finalized, and say so
  // (render() 4815–4830).
  const boardStats = createMemo(() => {
    if (store.unavailable()) return null;
    const d = store.payload();
    if (!d) return null;
    const entries = store.entries();
    const finalizedEntries = entries.filter((e) => isFinalized(e));
    const finalized = finalizedEntries.filter((e) => isEligible(e));
    const provisional = entries.filter((e) => !isFinalized(e) && isEligible(e));
    const headline = finalized.length ? finalized : provisional;
    const comps = headline.map((e) => displayComposite(e, store.settledView()));
    return {
      count: entries.length,
      comps,
      provisionalHeadline: !finalized.length && provisional.length > 0,
    };
  });

  const provisionalSmall = (): JSX.Element | null =>
    boardStats()?.provisionalHeadline ? <small>provisional</small> : null;

  return (
    <section class="snapshot" id="cards" aria-labelledby="snapshot-title">
      <h2 class="visually-hidden" id="snapshot-title">
        Subnet snapshot
      </h2>
      <dl class="stat-ledger">
        <div class="ledger-line featured">
          <dt class="ledger-label">
            <Tip text="Distinct miners who have submitted to Subnet 118.">Miners total</Tip>
          </dt>
          <dd class="ledger-value" id="h-miners">
            {h()?.miners ?? "–"}
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">
            <Tip text="Distinct miners with at least one scored run on the board (provisional runs included).">
              Miners scored
            </Tip>
          </dt>
          <dd class="ledger-value" id="c-miners">
            {boardStats() ? boardStats()?.count : "–"}
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">
            <Tip text="The highest composite among ranked full-benchmark runs. Start with 0.5 × tool mean + 0.5 × memory mean, apply the benchmark quality gates, then apply the v5 token-efficiency multiplier. Token efficiency can remove at most 10%. Provisional runs are excluded.">
              Top composite
            </Tip>
          </dt>
          <dd class="ledger-value" id="c-top">
            <Show when={boardStats()?.comps.length} fallback={"–"}>
              {fx(boardStats()?.comps[0] as number)}
              {provisionalSmall() ? " " : ""}
              {provisionalSmall()}
            </Show>
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">
            <Tip text="Median composite across ranked (full-benchmark) runs. The middle of the field, robust to one outlier.">
              Median composite
            </Tip>
          </dt>
          <dd class="ledger-value" id="c-median">
            <Show when={boardStats()?.comps.length} fallback={"–"}>
              {fx(median(boardStats()?.comps ?? []))}
              {provisionalSmall() ? " " : ""}
              {provisionalSmall()}
            </Show>
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">
            <Tip text="All validator score records stored by the platform, including the independent scores that make up each finalized result.">
              Total scores
            </Tip>
          </dt>
          <dd class="ledger-value" id="h-scores">
            {h()?.total_scores ?? "–"}
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">
            <Tip text="Validators currently reporting heartbeat-capable software to the platform.">
              Validators
            </Tip>
          </dt>
          <dd class="ledger-value" id="h-validators">
            {latest(operations)?.validators?.reported_count ?? "–"}
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">Scored agents</dt>
          <dd class="ledger-value" id="h-agents">
            {h()?.scored_agents ?? "–"}
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">
            <Tip text="How contested #1 is: the #1 composite minus the #2 composite among ranked runs. A small gap means a near-tie at the top.">
              Top gap
            </Tip>
          </dt>
          <dd class="ledger-value" id="c-spread">
            <Show when={(boardStats()?.comps.length ?? 0) >= 2} fallback={"–"}>
              {fx((boardStats()?.comps[0] as number) - (boardStats()?.comps[1] as number))}{" "}
              <small>
                {(boardStats()?.provisionalHeadline ? "provisional · " : "") + "#1 → #2"}
              </small>
            </Show>
          </dd>
        </div>
        <div class="ledger-line">
          <dt class="ledger-label">Last scored</dt>
          <dd class="ledger-value" id="h-last">
            {h()?.last_scored_at ? relTime(h()?.last_scored_at) : "–"}
          </dd>
        </div>
      </dl>
    </section>
  );
}
