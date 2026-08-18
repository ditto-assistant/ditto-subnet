// The reigning-KOTH box heading the overview's leaderboard column (markup
// 2646–2651 + renderChampionBox 4739–4771). It repeats the emissions strip's
// headline identity on purpose: box = who reigns at a glance, strip = why.
// Solid's fine-grained updates replace the lastChampionHtml signature gate —
// unchanged content never rewrites the aria-live region.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import { fx, pct, publicDisplayName, relTime, shortKey } from "../../lib/format";
import { crownContest, crownWhyHigh, displayComposite, signedScore } from "../../lib/scoring";
import { ChipTip } from "../board/chips";
import { leaderboardVersionView } from "../board/board-state";
import { EntityButton } from "../ui/EntityButton";
import { HandleBadge } from "../ui/HandleBadge";
import { MinerAvatar } from "../ui/MinerAvatar";
import type { BoardEntry, LeaderboardStore } from "../board/leaderboard-data";

export function ChampionBox(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const emissions = store.emissions;
  const championEntry = (): BoardEntry | null => store.champion();
  const hasChampion = (): boolean => emissions()?.champion_agent_id != null;
  const scoreCeilingPool = (): boolean => emissions()?.allocation_mode === "score_ceiling_pool";
  /** True only when the crown rests on an earlier generation than the one on
   * display, which is the case readers cannot otherwise account for. */
  const heldFromLineage = (): boolean => {
    const entry = championEntry();
    const anchor = entry?.crown_first_seen;
    return anchor != null && entry?.first_seen != null && anchor !== entry.first_seen;
  };
  const jointChampions = () =>
    (emissions()?.recipients || []).filter((recipient) => recipient.role === "joint_champion");
  const name = (): string => {
    const entry = championEntry();
    return (
      (entry
        ? publicDisplayName(entry.agent_name, entry.name_handle)
        : shortKey(emissions()?.champion_miner_hotkey)) || "Unidentified champion"
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
        {scoreCeilingPool() ? "KOTH · score-ceiling joint crown" : "KOTH · reigning champion"}
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
              <Show
                when={!scoreCeilingPool()}
                fallback={jointChampions().length + " evidence-tied agents"}
              >
                <Show when={championEntry()} fallback={name()}>
                  {(entry) => (
                    <>
                      <MinerAvatar url={entry().avatar_url} size="lg" />
                      <EntityButton kind="agent" id={entry().agent_id} label={name()} />
                      <HandleBadge handle={entry().name_handle} />
                    </>
                  )}
                </Show>
              </Show>
            </div>
            <Show when={!scoreCeilingPool() && composite() != null}>
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
            <Show when={scoreCeilingPool()}>
              <span>
                Joint champions <b>{jointChampions().length}</b>
              </span>
            </Show>
            <Show when={!scoreCeilingPool() && championEntry()?.rank}>
              <span>
                Raw rank <b>{"#" + championEntry()?.rank}</b>
              </span>
            </Show>
            <Show
              when={Number.isFinite(
                scoreCeilingPool() ? jointChampions()[0]?.share_of_miner_pool : share(),
              )}
            >
              <span>
                {scoreCeilingPool() ? "Miner pool each " : "Miner pool "}
                <b>
                  {pct(
                    (scoreCeilingPool()
                      ? jointChampions()[0]?.share_of_miner_pool
                      : share()) as number,
                  )}
                </b>
              </span>
            </Show>
            <Show when={!scoreCeilingPool() && championEntry()?.first_seen}>
              <span>
                First seen <b>{relTime(championEntry()?.first_seen)}</b>
              </span>
            </Show>
            {/* The anchor, shown only when it disagrees with the upload time.
                That disagreement is the whole of the "champion is newer than
                the agents above it" complaint, and the board used to publish
                only the number that made the fold look wrong. */}
            <Show when={!scoreCeilingPool() && heldFromLineage()}>
              <span>
                Holds from <b>{relTime(championEntry()?.crown_first_seen as string)}</b>
              </span>
            </Show>
            <Show when={!scoreCeilingPool() && championEntry()?.bench_version != null}>
              <span>
                Bench <b>{"v" + championEntry()?.bench_version}</b>
              </span>
            </Show>
          </div>
          {/* The held-crown note: the champion sitting below raw #1 is the
              standing that confuses readers most, so the box says why in
              place instead of leaving it to the strip on the other page. */}
          <Show when={scoreCeilingPool()}>
            <div class="champion-note" id="champion-note">
              The single-winner dethrone threshold cannot be exceeded within the score range. Every
              highest evidence-tied agent shares the full miner pool equally, including ties beyond
              the normal tail cutoff.
            </div>
          </Show>
          <Show when={!scoreCeilingPool() && ((championEntry()?.rank as number) || 1) > 1}>
            <div class="champion-note" id="champion-note">
              {(() => {
                const contest = crownContest(emissions()?.raw_leader_decision, emissions());
                const rank = championEntry()?.rank as number;
                const above = rank - 1;
                const aboveText =
                  above === 1 ? "1 agent scores higher" : above + " agents score higher";
                if (!contest) {
                  return (
                    "Holds the crown from raw #" +
                    rank +
                    ": " +
                    aboveText +
                    ", but none by enough on the shared-seed head-to-head. Rank is not the crown."
                  );
                }
                return (
                  "Holds the crown from raw #" +
                  rank +
                  ". " +
                  aboveText +
                  ", but the head-to-head lead is " +
                  signedScore(contest.challengerLead) +
                  " and the crown needs " +
                  signedScore(contest.requiredLead) +
                  ". " +
                  crownWhyHigh(contest)
                );
              })()}
            </div>
          </Show>
        </Show>
      </div>
    </section>
  );
}
