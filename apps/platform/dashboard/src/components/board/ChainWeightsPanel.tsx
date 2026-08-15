// The raw on-chain weight matrix, one click away from the board: a toggle
// that unfolds every revealed validator vector as it sits on Subtensor
// storage — validator identity, then its miner destinations heaviest-first
// with normalized shares. The per-row badges/meters summarize this matrix;
// this panel IS the matrix, so "what are the weights actually set to right
// now" needs no telemetry spelunking. Hidden entirely on historical views
// (the store nulls the snapshot there) and while the chain is unreachable.
import { For, Show, createMemo, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { agentName, pct, relDuration, shortKey } from "../../lib/format";
import { validatorWeightViews } from "../../lib/scoring";
import type { ValidatorWeightEntry, ValidatorWeightView } from "../../lib/scoring";
import { EntityButton } from "../ui/EntityButton";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

function entryTitle(entry: ValidatorWeightEntry, boardEntry: BoardEntry | undefined): string {
  return (
    (boardEntry ? agentName(boardEntry.agent_name) + " · " : "") +
    entry.hotkey +
    " · raw u16 " +
    entry.value +
    " · " +
    pct(entry.share) +
    " of this vector's miner weight" +
    (entry.top ? " · this vector's top choice" : "")
  );
}

export function ChainWeightsPanel(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const [open, setOpen] = createSignal(false);
  const snapshot = (): ReturnType<LeaderboardStore["chainWeights"]> => store.chainWeights();
  const views = createMemo<ValidatorWeightView[]>(
    () => (validatorWeightViews(snapshot()) ?? []).filter((view) => view.entries.length),
    [],
  );
  const shown = createMemo(() => !store.unavailable() && views().length > 0);
  const byHotkey = createMemo(() => {
    const out = new Map<string, BoardEntry>();
    store.entries().forEach((entry) => out.set(entry.miner_hotkey, entry));
    return out;
  });
  const blockLabel = (): string => {
    const block = snapshot()?.block;
    return block == null ? "" : "block " + Number(block).toLocaleString();
  };
  return (
    <Show when={shown()}>
      <div class="chain-weights" id="chain-weights">
        <button
          type="button"
          class="chain-weights-toggle"
          id="chain-weights-toggle"
          aria-expanded={open() ? "true" : "false"}
          aria-controls="chain-weights-panel"
          onClick={() => setOpen(!open())}
        >
          <span class="chain-weights-toggle-dot" aria-hidden="true" />
          {(open() ? "Hide" : "Show") +
            " on-chain weights · " +
            views().length +
            " validator vectors · " +
            blockLabel()}
          <Show when={snapshot()?.stale}>
            <span class="chain-weights-stale">stale</span>
          </Show>
          <span class="chain-weights-caret" aria-hidden="true">
            {open() ? "▴" : "▾"}
          </span>
        </button>
        <div
          class="chain-weights-panel"
          id="chain-weights-panel"
          role="region"
          aria-label="Revealed on-chain validator weight matrix"
          hidden={!open()}
        >
          <p class="chain-weights-hint">
            The weight matrix as it is currently set on chain — each validator's most recently
            revealed vector, heaviest destination first, normalized within the vector. Commit-reveal
            can make this lag active commitments; Yuma folds these vectors stake-weighted, so equal
            rows here are not equal influence.
            <Show when={snapshot()?.stale}>
              {" The chain re-read is currently failing; this is the last matrix successfully read" +
                (snapshot()?.age_seconds
                  ? ", " + relDuration(snapshot()?.age_seconds) + " ago"
                  : "") +
                "."}
            </Show>
          </p>
          <For each={views()}>
            {(view) => (
              <div class="chain-vector">
                <span class="chain-vector-validator">
                  <Show when={view.validatorHotkey} fallback={<span>Unknown validator</span>}>
                    <EntityButton
                      kind="validator"
                      id={view.validatorHotkey}
                      label={shortKey(view.validatorHotkey)}
                    />
                  </Show>
                  <Show when={view.validatorUid != null}>
                    <span class="chain-vector-uid">UID {view.validatorUid}</span>
                  </Show>
                </span>
                <span class="chain-vector-weights">
                  <For each={view.entries}>
                    {(entry) => (
                      <span
                        class={"chain-vector-chip " + (entry.top ? "top-choice" : "support")}
                        title={entryTitle(entry, byHotkey().get(entry.hotkey))}
                      >
                        <span class="chain-vector-chip-uid">UID {entry.uid}</span>
                        <Show when={byHotkey().get(entry.hotkey)}>
                          {(boardEntry) => (
                            <span class="chain-vector-chip-name">
                              {agentName(boardEntry().agent_name)}
                            </span>
                          )}
                        </Show>
                        <span class="chain-vector-chip-share">{pct(entry.share)}</span>
                      </span>
                    )}
                  </For>
                </span>
              </div>
            )}
          </For>
        </div>
      </div>
    </Show>
  );
}
