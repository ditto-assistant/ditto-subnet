// Overview strip for the generator competition: the newest scored round, the
// leading generator and when the next round opens, linking to /generators.
// Renders nothing until the generator endpoints answer, so a Platform build
// without them leaves the overview unchanged.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { REFRESH_MS } from "../../lib/config";
import { fx, relTimeUntil, shortKey } from "../../lib/format";
import { dashboardHref } from "../../lib/router";
import type { ChainEpoch } from "../../types/leaderboard";
import type { GeneratorBoard, GeneratorRoundList } from "./generator-data";
import { latestScoredRound, nextRoundEta, projectGeneratorPayouts } from "./generator-data";

export function GeneratorOverviewCard(props: {
  epoch: () => ChainEpoch | null | undefined;
}): JSX.Element {
  const board = useEndpoint<GeneratorBoard>("/generator/board", { pollMs: REFRESH_MS });
  const rounds = useEndpoint<GeneratorRoundList>("/generator/rounds", { pollMs: REFRESH_MS });
  const boardData = () => (board.error() ? undefined : board.data());
  const roundData = () => (rounds.error() ? undefined : rounds.data());
  const leader = () => {
    const entries = boardData()?.entries;
    if (!entries) return null;
    const top = projectGeneratorPayouts(entries)[0];
    return top && top.position !== null ? top.entry : null;
  };
  const eta = () => nextRoundEta(roundData()?.schedule, props.epoch());
  const visible = () => boardData() !== undefined && boardData()?.mode !== "off";
  return (
    <Show when={visible()}>
      <section class="gen-overview" aria-label="Generator competition">
        <a class="gen-overview-link" href={dashboardHref("generators")}>
          <strong>Generator competition</strong>
          <Show when={latestScoredRound(roundData())}>
            {(round) => <span>Round {round().round_index} scored</span>}
          </Show>
          <Show
            when={leader()}
            fallback={<span>No eligible generator · its share pays agent miners</span>}
          >
            {(entry) => (
              <span>
                Leader {shortKey(entry().miner_hotkey)} · {fx(entry().score)}
              </span>
            )}
          </Show>
          <Show when={eta()}>
            {(at) => <span>Next round {relTimeUntil(at().toISOString())}</span>}
          </Show>
          <span class="gen-overview-go" aria-hidden="true">
            →
          </span>
        </a>
      </section>
    </Show>
  );
}
