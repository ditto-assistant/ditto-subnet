// The dedicated Leaderboard page (markup 2738–2740): a single host that
// receives the shared block. The monolith re-parented one DOM node here;
// this port renders the one <LeaderboardBlock> component over the same
// module-scope state, so the two mounts stay the same instrument. Entering
// this page resets the version view to the current rollout (route()
// 9793–9797) — an archive can never render here; archive browsing lives on
// the overview. The page-scoped CSS un-hides every hide-md/hide-sm column:
// this page's promise is every column at every viewport.
import type { JSX } from "solid-js";

import { LeaderboardBlock } from "../components/board/LeaderboardBlock";

export function LeaderboardPage(): JSX.Element {
  return (
    <section class="page active" data-page="leaderboard">
      <div id="leaderboard-page-host">
        <LeaderboardBlock mode="page" />
      </div>
    </section>
  );
}
