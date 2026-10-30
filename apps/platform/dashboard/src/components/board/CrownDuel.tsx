// The crown, explained: one King and the challengers dueling it.
//
// Protocol 34 moves the crown only on a head-to-head over shared seeds, so
// the question every miner brings is "how close is each challenger, and what
// does it still need?". This answers it from `emissions.king`,
// `emissions.challengers`, and `emissions.crown_rules` alone. Rendered above
// the board on both mounts; the overview's masthead already names the King,
// so that mount drops the King line.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { fx, publicDisplayName, shortKey } from "../../lib/format";
import { challengerNeeds, confirmationProgress, pairedLeadText } from "../../lib/crown";
import type { CrownChallenger, CrownRules } from "../../types/leaderboard";
import { EntityButton } from "../ui/EntityButton";
import { leaderboardVersionView } from "./board-state";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

const STATUS_LABEL: Record<string, string> = {
  would_dethrone: "Would dethrone",
  dueling: "Dueling",
  needs_confirmations: "Gathering evidence",
  outside_standing: "Outside the top",
};

export function CrownDuel(props: { store: LeaderboardStore; showKing?: boolean }): JSX.Element {
  const store = props.store;
  const emissions = () => store.emissions();
  const rules = (): CrownRules | null => emissions()?.crown_rules ?? null;
  const king = () => emissions()?.king ?? null;
  const challengers = (): CrownChallenger[] => emissions()?.challengers ?? [];
  const shown = (): boolean =>
    leaderboardVersionView() === "current" &&
    !store.unavailable() &&
    rules() != null &&
    king() != null &&
    emissions()?.allocation_mode !== "score_ceiling_pool";
  const entryFor = (agentId: string): BoardEntry | undefined =>
    store.entries().find((entry) => String(entry.agent_id) === String(agentId));
  const nameFor = (agentId: string, hotkey: string): string => {
    const entry = entryFor(agentId);
    return entry ? publicDisplayName(entry.agent_name, entry.name_handle) : shortKey(hotkey);
  };
  const versionFor = (agentId: string): string => {
    const version = entryFor(agentId)?.agent_version;
    return version != null ? " v" + version : "";
  };

  return (
    <Show when={shown()}>
      <section class="crown-duel" id="crown-duel" aria-labelledby="crown-duel-title">
        <Show when={props.showKing !== false ? king() : null}>
          {(current) => (
            <div class="crown-duel-king" id="crown-duel-king">
              <span class="crown-duel-crown" aria-hidden="true">
                ♛
              </span>
              <span class="crown-duel-king-label">King</span>
              <span class="crown-duel-king-name">
                <EntityButton
                  kind="agent"
                  id={current().agent_id}
                  label={nameFor(current().agent_id, current().miner_hotkey)}
                />
                <span class="crown-duel-version">{versionFor(current().agent_id)}</span>
              </span>
              <span class="crown-duel-king-facts">
                <span>
                  Official score <b>{fx(current().official_score)}</b>
                </span>
                <span>
                  Rank <b>{"#" + current().official_rank}</b>
                </span>
                <span>
                  <b>{current().shared_seed_confirmations ?? 0}</b> shared-seed confirmations
                </span>
              </span>
            </div>
          )}
        </Show>
        <div class="crown-duel-head">
          <h3 id="crown-duel-title">Challengers dueling the King</h3>
          <span class="crown-duel-hint">
            {"A duel counts after " +
              (rules()?.min_dethrone_confirmations ?? 3) +
              " shared-seed confirmations"}
          </span>
        </div>
        <Show
          when={challengers().length > 0}
          fallback={<p class="crown-duel-empty">No other miner is ranked yet.</p>}
        >
          <ol class="crown-duel-list">
            <For each={challengers()}>
              {(challenger) => {
                const progress = () => confirmationProgress(challenger);
                return (
                  <li class="crown-duel-row" data-status={challenger.status}>
                    <span class="crown-duel-rank" title="Official rank (the board's rank column)">
                      {"#" + challenger.official_rank}
                    </span>
                    <span class="crown-duel-name">
                      <EntityButton
                        kind="agent"
                        id={challenger.agent_id}
                        label={nameFor(challenger.agent_id, challenger.miner_hotkey)}
                      />
                      <span class="crown-duel-version">{versionFor(challenger.agent_id)}</span>
                    </span>
                    <span class="crown-duel-score" title="Official score">
                      {fx(challenger.official_score)}
                    </span>
                    <span
                      class="crown-duel-confs"
                      title={
                        challenger.shared_seed_confirmations +
                        " seeds shared with the King; " +
                        progress().required +
                        " needed for the duel to count"
                      }
                    >
                      <span class="crown-duel-bar" aria-hidden="true">
                        <span style={{ width: Math.round(progress().fraction * 100) + "%" }} />
                      </span>
                      <span class="crown-duel-confs-text">
                        {progress().counted + " of " + progress().required + " confirmations"}
                      </span>
                    </span>
                    <span
                      class="crown-duel-lead"
                      classList={{
                        ahead: (challenger.paired_lead ?? 0) > 0,
                        behind: (challenger.paired_lead ?? 0) < 0,
                      }}
                      title="Paired lead over the King on their shared seeds"
                    >
                      <span class="visually-hidden">Paired lead </span>
                      {pairedLeadText(challenger)}
                    </span>
                    <span class="crown-duel-needs">
                      <span class="crown-duel-status">
                        {STATUS_LABEL[challenger.status] ?? challenger.status}
                      </span>
                      {" · " + challengerNeeds(challenger, rules())}
                    </span>
                  </li>
                );
              }}
            </For>
          </ol>
        </Show>
        <Show when={rules()?.active === false}>
          <p class="crown-duel-rollout" id="crown-duel-rollout">
            {"Rolling out — validators enforce this once every weight setter reports protocol " +
              (rules()?.required_protocol ?? 34) +
              "."}
          </p>
        </Show>
        <details class="fold crown-duel-fold">
          <summary>How the crown works</summary>
          <p class="crown-duel-explainer">
            {"The King holds the crown until a challenger beats it head-to-head on the same shared seeds. " +
              "A challenger needs at least " +
              (rules()?.min_dethrone_confirmations ?? 3) +
              " shared-seed confirmations against the King before that duel counts. " +
              "The King must stay in the top " +
              (rules()?.champion_standing_rank ?? 3) +
              " official places, or the best-qualified challenger takes over. " +
              "Rank orders by official score — the continual mean of the three quorum scores plus completed retest waves — not by the single three-validator canonical composite."}
          </p>
        </details>
      </section>
    </Show>
  );
}
