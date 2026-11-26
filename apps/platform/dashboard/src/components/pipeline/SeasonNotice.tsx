// The page-level dueling-season notice (bench v16+): while no season is open
// for the bench version the queue scores at, validators turn scoring work
// away, so every waiting submission is waiting for the next season, not for
// validators. Read from /public/bench/rollout's `dueling_season`; hidden
// whenever a season is open, below bench v16, or the read fails.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { REFRESH_MS } from "../../lib/config";
import type { DuelingSeasonState, RolloutState } from "../../types/leaderboard";
import { seasonWaitSentence } from "./status";

/** The closed season to announce, or null when there is nothing to say. */
export function closedSeason(rollout: RolloutState | null | undefined): DuelingSeasonState | null {
  const season = rollout?.dueling_season;
  return season && season.open === false ? season : null;
}

export function SeasonNotice(): JSX.Element {
  const rollout = useEndpoint<RolloutState>("/public/bench/rollout", { pollMs: REFRESH_MS });
  const season = (): DuelingSeasonState | null => {
    // An errored resource throws on read; a failed read announces nothing.
    if (rollout.error()) return null;
    try {
      return closedSeason(rollout.data());
    } catch {
      return null;
    }
  };
  return (
    <Show when={season()}>
      {(closed) => (
        <p class="season-notice" id="season-notice" role="status">
          <strong>Scoring is paused between dueling seasons.</strong>{" "}
          {seasonWaitSentence(closed().bench_version, closed().next_opens_at)} Waiting submissions
          stay queued.
        </p>
      )}
    </Show>
  );
}
