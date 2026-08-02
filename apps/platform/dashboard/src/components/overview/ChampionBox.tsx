// The reigning-KOTH box heading the overview's leaderboard column (markup
// 2646–2651 + renderChampionBox 4739–4771). It repeats the emissions strip's
// headline identity on purpose: box = who reigns at a glance, strip = why.
// Solid's fine-grained updates replace the lastChampionHtml signature gate —
// unchanged content never rewrites the aria-live region.
import { Show, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import { agentName, fx, pct, relTime, shortKey } from "../../lib/format";
import { displayComposite } from "../../lib/scoring";
import { ChipTip } from "../board/chips";
import { leaderboardVersionView } from "../board/board-state";
import { EntityButton } from "../ui/EntityButton";
import type { BoardEntry, LeaderboardStore } from "../board/leaderboard-data";

export function ChampionBox(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const emissions = store.emissions;
  const championEntry = createMemo<BoardEntry | null>(() => {
    const e = emissions();
    if (!e || e.champion_agent_id == null) return null;
    return (
      store.entries().find((entry) => String(entry.agent_id) === String(e.champion_agent_id)) ??
      null
    );
  });
  const hasChampion = (): boolean => emissions()?.champion_agent_id != null;
  const name = (): string => {
    const entry = championEntry();
    return (
      (entry ? agentName(entry.agent_name) : shortKey(emissions()?.champion_miner_hotkey)) ||
      "Unidentified champion"
    );
  };
  const share = (): number | undefined => {
    const e = emissions();
    const recipient = (e?.recipients || []).find((r) => r.role === "champion");
    return recipient && Number.isFinite(recipient.share_of_miner_pool)
      ? recipient.share_of_miner_pool
      : e?.champion_share;
  };
  const composite = (): number | null => {
    const entry = championEntry();
    return entry ? displayComposite(entry, store.settledView()) : null;
  };

  return (
    <section class="champion-box" aria-labelledby="champion-box-kicker">
      <div class="champion-kicker" id="champion-box-kicker">
        <span class="crown" aria-hidden="true">
          ♛
        </span>
        KOTH · reigning champion
      </div>
      <div class="champion-body" id="champion-body" role="status" aria-live="polite">
        <Show
          when={hasChampion()}
          fallback={
            <Show
              when={leaderboardVersionView() !== "current"}
              fallback={
                <Show
                  when={store.payload()}
                  fallback={<div class="champion-state">Waiting for the emissions projection…</div>}
                >
                  <div class="champion-state">
                    No reigning champion in the current pool — the KOTH fold has no eligible
                    finalized run yet. Raw rank #1 leads the board below.
                  </div>
                </Show>
              }
            >
              {/* Historical bench views carry no emissions projection; saying
                  "no champion" there would misstate the live subnet. */}
              <div class="champion-state">
                {"Viewing the Bench v" +
                  leaderboardVersionView() +
                  " archive. KOTH emissions apply to the current rollout — switch to Current rollout to see the reigning champion."}
              </div>
            </Show>
          }
        >
          <div class="champion-row">
            <div class="champion-name">
              <Show when={championEntry()} fallback={name()}>
                {(entry) => <EntityButton kind="agent" id={entry().agent_id} label={name()} />}
              </Show>
            </div>
            <Show when={composite() != null}>
              <ChipTip
                tag="div"
                class="champion-score tip-chip"
                text="Finalized composite — the headline score in [0,1] that drives rank."
              >
                <span class="champion-score-label">Composite</span>
                {fx(composite() as number)}
              </ChipTip>
            </Show>
          </div>
          <div class="champion-meta">
            <Show when={championEntry()?.rank}>
              <span>
                Raw rank <b>{"#" + championEntry()?.rank}</b>
              </span>
            </Show>
            <Show when={Number.isFinite(share())}>
              <span>
                Miner pool <b>{pct(share() as number)}</b>
              </span>
            </Show>
            <Show when={championEntry()?.first_seen}>
              <span>
                First seen <b>{relTime(championEntry()?.first_seen)}</b>
              </span>
            </Show>
            <Show when={championEntry()?.bench_version != null}>
              <span>
                Bench <b>{"v" + championEntry()?.bench_version}</b>
              </span>
            </Show>
          </div>
        </Show>
      </div>
    </section>
  );
}
