// Overview: the two-pane split (markup 2607–2736). The rail carries the
// memory timeline and the snapshot ledger; the main column carries the
// champion box and the shared #leaderboard-block in its compact home.
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

export function OverviewPage(): JSX.Element {
  const store = leaderboardStore();
  return (
    <section class="page active" data-page="overview">
      <div class="overview-split">
        <aside
          class="overview-rail"
          tabindex="0"
          role="region"
          aria-label="Memory timeline and subnet snapshot, scrolls separately from the leaderboard"
        >
          <HarnessComparison store={store} />
          <SnapshotLedger store={store} />
        </aside>
        <div
          class="overview-main"
          tabindex="0"
          role="region"
          aria-label="Champion and leaderboard column, scrolls separately from the timeline rail"
        >
          <ChampionBox store={store} />
          <LeaderboardBlock mode="overview" />
        </div>
      </div>
    </section>
  );
}
