// Overview: vitals first, then the evidence beside the crown, then the board.
//
// Reading rows, top to bottom. The vitals (four population cards over a strip
// of score readings) say what state the subnet is in. Chain economics sits
// under that (preview-only SN118 surface). The memory timeline shares a row
// with who reigns and when the next payout lands. The leaderboard then takes
// the full width, so standings are never squeezed into a rail.
//
// Standings are never hidden behind a click (ditto-platform#383): the board
// is compact here through page-scoped CSS, and the full column set lives on
// the dedicated Leaderboard page — compactness through a second surface,
// not through disclosure.
import type { JSX } from "solid-js";

import { CrownHistory } from "../components/board/CrownHistory";
import { LeaderboardBlock } from "../components/board/LeaderboardBlock";
import { leaderboardStore } from "../components/board/leaderboard-data";
import { ChampionBox } from "../components/overview/ChampionBox";
import { ChainEconomicsStrip } from "../components/overview/ChainEconomicsStrip";
import { HarnessComparison } from "../components/overview/HarnessComparison";
import { SnapshotLedger } from "../components/overview/SnapshotLedger";
import { EpochClock } from "../components/shell/EpochClock";
import type { ResourceState } from "../data/useEndpoint";
import { weightsResource } from "../data/weights";
import type { PublicChainResponse } from "../types/chain";
import type { OperationsPayload } from "../types/fleet";
import type { ChainEpoch, PinAgreementSummary } from "../types/leaderboard";

function latestEpoch(resource: ResourceState<{ epoch?: ChainEpoch | null }>): ChainEpoch | null {
  if (resource.error()) return null;
  try {
    return resource.data()?.epoch ?? null;
  } catch {
    return null;
  }
}

function latestPin(
  resource: ResourceState<{ pin_agreement?: PinAgreementSummary | null }>,
): PinAgreementSummary | null {
  if (resource.error()) return null;
  try {
    return resource.data()?.pin_agreement ?? null;
  } catch {
    return null;
  }
}

export function OverviewPage(
  props: {
    operations?: ResourceState<OperationsPayload>;
    chain?: ResourceState<PublicChainResponse>;
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
      <div class="overview-vitals" role="region" aria-label="Subnet at a glance">
        <SnapshotLedger store={store} operations={props.operations} />
      </div>
      <ChainEconomicsStrip chain={props.chain} />
      <div class="overview-split">
        <div class="overview-rail" role="region" aria-label="Memory timeline">
          <HarnessComparison store={store} />
        </div>
        <aside class="overview-side" aria-label="Reigning champion and next payout">
          <ChampionBox store={store} />
          {/* The rail's clock is hidden while this page is on screen at desktop
              widths (shell.css), so the reading appears once. On the phone the
              sticky top bar keeps its compact clock and this one steps aside. */}
          <div class="overview-clock">
            <EpochClock
              epoch={epoch}
              pin={() => (weights ? latestPin(weights) : null)}
              id="overview-epoch-clock"
            />
          </div>
        </aside>
      </div>
      <div class="overview-main" role="region" aria-label="Current rollout leaderboard">
        <LeaderboardBlock mode="overview" />
        <CrownHistory mode="overview" />
      </div>
    </section>
  );
}
