// Overview: a masthead band over a two-pane split.
//
// The band answers the three questions a visitor brings to the homepage in
// one reading line — who reigns (champion), what state the subnet is in
// (vitals ledger), and when the next payout lands (epoch clock) — so none of
// them sits below the fold or behind the chart. The split beneath keeps the
// incumbent arrangement: the memory timeline in the rail, the shared
// #leaderboard-block in its compact home in the main column.
//
// Standings are never hidden behind a click (ditto-platform#383): the board
// is compact here through page-scoped CSS, and the full column set lives on
// the dedicated Leaderboard page — compactness through a second surface,
// not through disclosure.
import type { JSX } from "solid-js";

import { LeaderboardBlock } from "../components/board/LeaderboardBlock";
import { leaderboardStore } from "../components/board/leaderboard-data";
import { ChampionBox } from "../components/overview/ChampionBox";
import { HarnessComparison } from "../components/overview/HarnessComparison";
import { SnapshotLedger } from "../components/overview/SnapshotLedger";
import { EpochClock } from "../components/shell/EpochClock";
import type { ResourceState } from "../data/useEndpoint";
import { weightsResource } from "../data/weights";
import type { OperationsPayload } from "../types/fleet";
import type { ChainEpoch } from "../types/leaderboard";

function latestEpoch(resource: ResourceState<{ epoch?: ChainEpoch | null }>): ChainEpoch | null {
  if (resource.error()) return null;
  try {
    return resource.data()?.epoch ?? null;
  } catch {
    return null;
  }
}

export function OverviewPage(
  props: {
    operations?: ResourceState<OperationsPayload>;
    /** /public/weights `epoch` for the masthead clock. Defaults to the shared
     * weights resource (the shell's tick refreshes it); tests may inject. */
    epoch?: () => ChainEpoch | null | undefined;
  } = {},
): JSX.Element {
  const store = leaderboardStore();
  const weights = props.epoch ? null : weightsResource();
  const epoch = (): ChainEpoch | null | undefined =>
    props.epoch ? props.epoch() : weights ? latestEpoch(weights) : null;
  return (
    <section class="page active" data-page="overview">
      <div class="overview-masthead" role="region" aria-label="Subnet at a glance">
        <ChampionBox store={store} />
        <SnapshotLedger store={store} operations={props.operations} />
        {/* The rail's clock is hidden while this page is on screen at desktop
            widths (shell.css), so the reading appears once. On the phone the
            sticky top bar keeps its compact clock and this one steps aside. */}
        <div class="overview-clock">
          <EpochClock epoch={epoch} id="overview-epoch-clock" />
        </div>
      </div>
      <div class="overview-split">
        <aside
          class="overview-rail"
          tabindex="0"
          role="region"
          aria-label="Memory timeline, scrolls separately from the leaderboard"
        >
          <HarnessComparison store={store} />
        </aside>
        <div
          class="overview-main"
          tabindex="0"
          role="region"
          aria-label="Leaderboard column, scrolls separately from the timeline rail"
        >
          <LeaderboardBlock mode="overview" />
        </div>
      </div>
    </section>
  );
}
