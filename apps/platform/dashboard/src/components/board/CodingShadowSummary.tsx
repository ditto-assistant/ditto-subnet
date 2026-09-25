import { createMemo } from "solid-js";
import type { JSX } from "solid-js";

import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

export type CodingShadowState = "complete" | "in_progress" | "not_evaluated" | "stale";

export interface CodingShadowCounts {
  all: number;
  complete: number;
  collecting: number;
  scheduled: number;
  in_progress: number;
  not_evaluated: number;
  stale: number;
}

export function codingShadowState(entry: BoardEntry): CodingShadowState {
  const coding = entry.coding_shadow;
  if (!coding) return "not_evaluated";
  if (coding.status === "complete" && coding.score != null) return "complete";
  if (coding.status === "scheduled" || coding.status === "collecting") return "in_progress";
  return "stale";
}

export function codingShadowCounts(entries: readonly BoardEntry[]): CodingShadowCounts {
  const counts: CodingShadowCounts = {
    all: entries.length,
    complete: 0,
    collecting: 0,
    scheduled: 0,
    in_progress: 0,
    not_evaluated: 0,
    stale: 0,
  };
  entries.forEach((entry) => {
    const state = codingShadowState(entry);
    counts[state] += 1;
    if (entry.coding_shadow?.status === "scheduled") counts.scheduled += 1;
    if (entry.coding_shadow?.status === "collecting") counts.collecting += 1;
  });
  return counts;
}

export function CodingShadowSummary(props: { store: LeaderboardStore }): JSX.Element {
  const counts = createMemo(() => codingShadowCounts(props.store.entries()));
  const loaded = (): boolean => props.store.payload() !== null && !props.store.unavailable();
  return (
    <section
      class="coding-shadow-summary"
      aria-labelledby="coding-shadow-summary-title"
      aria-busy={loaded() ? undefined : "true"}
    >
      <div class="coding-shadow-summary-copy">
        <div class="coding-shadow-summary-heading">
          <h3 id="coding-shadow-summary-title">Coding Bench coverage</h3>
          <span class="coding-shadow-mode">Shadow only</span>
        </div>
        <p>
          Exact-artifact Coding results are public only as aggregates. They never change composite,
          rank, validator weights, or emissions.
        </p>
      </div>
      <dl class="coding-shadow-summary-metrics">
        <div>
          <dt>Complete</dt>
          <dd data-coding-summary="complete">
            {loaded() ? counts().complete + "/" + counts().all : "–"}
          </dd>
        </div>
        <div>
          <dt>Collecting</dt>
          <dd data-coding-summary="collecting">{loaded() ? counts().collecting : "–"}</dd>
        </div>
        <div>
          <dt>Scheduled</dt>
          <dd data-coding-summary="scheduled">{loaded() ? counts().scheduled : "–"}</dd>
        </div>
        <div>
          <dt>Stale</dt>
          <dd data-coding-summary="stale">{loaded() ? counts().stale : "–"}</dd>
        </div>
        <div>
          <dt>Quorum</dt>
          <dd data-coding-summary="quorum">3 validators</dd>
        </div>
      </dl>
    </section>
  );
}
