// Metagraph: SN118 miners with stake, Yuma signals, KOTH score, and τ/day.
import { For, Show, createEffect, createMemo, createSignal, type JSX } from "solid-js";

import { ChainEconomicsStrip } from "../components/overview/ChainEconomicsStrip";
import { CopyButton } from "../components/shell/CopyButton";
import { MinerAvatar } from "../components/ui/MinerAvatar";
import { Pager } from "../components/ui/Pager";
import type { ResourceState } from "../data/useEndpoint";
import { useEndpoint } from "../data/useEndpoint";
import { publicDisplayName, shortKey } from "../lib/format";
import { pushEntityRoute } from "../stores/routeStore";
import type { PublicChainEpoch, PublicChainResponse, PublicNeuron } from "../types/chain";
import type { LeaderboardEntry, LeaderboardPayload } from "../types/leaderboard";

const PAGE_SIZE = 25;

type SortKey =
  | "uid"
  | "agent"
  | "score"
  | "stake"
  | "incentive"
  | "consensus"
  | "emission"
  | "tao_day";

type RowEnrichment = {
  agentName: string | null;
  agentId: string | null;
  avatarUrl: string | null;
  score: number | null;
};

/** A validator permit does not make a hotkey miner-ineligible. A permitted UID
 * with positive incentive is participating in the miner allocation and must
 * remain visible beside ordinary non-permit miner registrations. */
export function isMiningUid(neuron: PublicNeuron): boolean {
  return !neuron.validator_permit || neuron.incentive > 0;
}

function latest<T>(resource: ResourceState<T> | undefined): T | undefined {
  if (!resource || resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

function entryScore(entry: LeaderboardEntry): number | null {
  const value = entry.official_composite ?? entry.composite;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Tempos in a day from chain epoch config (fallback: 360-block / 12s tempo). */
function temposPerDay(epoch: PublicChainEpoch | null | undefined): number {
  const tempoBlocks = epoch?.tempo_blocks || 360;
  const blockSeconds = epoch?.block_seconds || 12;
  const tempoSeconds = tempoBlocks * blockSeconds;
  if (tempoSeconds <= 0) return 0;
  return 86_400 / tempoSeconds;
}

/** Projected τ/day from last-tempo α emission × tempos/day × α/TAO. */
function taoPerDay(
  emissionAlpha: number,
  epoch: PublicChainEpoch | null | undefined,
  alphaTao: number | null | undefined,
): number | null {
  if (alphaTao == null || !Number.isFinite(alphaTao)) return null;
  const rate = temposPerDay(epoch);
  if (rate <= 0) return null;
  return emissionAlpha * rate * alphaTao;
}

export function MetagraphPage(
  props: {
    chain?: ResourceState<PublicChainResponse>;
    leaderboard?: ResourceState<LeaderboardPayload>;
  } = {},
): JSX.Element {
  const localChain = props.chain ? null : useEndpoint<PublicChainResponse>("/public/chain");
  const chain = () => props.chain ?? localChain!;
  const [query, setQuery] = createSignal("");
  const [sortKey, setSortKey] = createSignal<SortKey>("emission");
  const [sortDir, setSortDir] = createSignal<"desc" | "asc">("desc");
  const [page, setPage] = createSignal(1);

  /** Hotkey → leaderboard row. Agents are submitted under a hotkey. */
  const entryByHotkey = createMemo(() => {
    const map = new Map<string, LeaderboardEntry>();
    for (const entry of latest(props.leaderboard)?.entries ?? []) {
      if (entry.miner_hotkey) map.set(entry.miner_hotkey, entry);
    }
    return map;
  });

  function enrich(neuron: PublicNeuron): RowEnrichment {
    const entry = entryByHotkey().get(neuron.hotkey);
    if (!entry) {
      return { agentName: null, agentId: null, avatarUrl: null, score: null };
    }
    return {
      agentName: publicDisplayName(entry.agent_name, entry.name_handle),
      agentId: entry.agent_id ?? null,
      avatarUrl: entry.avatar_url ?? null,
      score: entryScore(entry),
    };
  }

  function displayName(neuron: PublicNeuron): string {
    return enrich(neuron).agentName || "—";
  }

  function rowTaoDay(neuron: PublicNeuron): number | null {
    const snap = latest(chain());
    return taoPerDay(neuron.emission, snap?.epoch, snap?.market.alpha_tao);
  }

  const filtered = createMemo(() => {
    const snap = latest(chain());
    // Hide validator-only rows, but retain permit holders that also receive a
    // miner incentive. Validator permit is a capability, not an exclusive role.
    let list = (snap?.metagraph ?? []).filter(isMiningUid);
    const q = query().trim().toLowerCase();
    if (q) {
      list = list.filter((n) => {
        const info = enrich(n);
        const agent = info.agentName ?? "";
        return (
          String(n.uid).includes(q) ||
          n.hotkey.toLowerCase().includes(q) ||
          n.coldkey.toLowerCase().includes(q) ||
          agent.toLowerCase().includes(q) ||
          (info.agentId ?? "").toLowerCase().includes(q)
        );
      });
    }
    const key = sortKey();
    const dir = sortDir() === "asc" ? 1 : -1;
    return [...list].sort((a, b) => {
      let av: string | number | null;
      let bv: string | number | null;
      if (key === "agent") {
        av = displayName(a).toLowerCase();
        bv = displayName(b).toLowerCase();
      } else if (key === "score") {
        av = enrich(a).score;
        bv = enrich(b).score;
      } else if (key === "tao_day") {
        av = rowTaoDay(a);
        bv = rowTaoDay(b);
      } else {
        av = a[key];
        bv = b[key];
      }
      if (av == null && bv == null) return a.uid - b.uid;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (av === bv) return a.uid - b.uid;
      return av < bv ? -1 * dir : 1 * dir;
    });
  });

  const pageCount = createMemo(() => Math.max(1, Math.ceil(filtered().length / PAGE_SIZE)));

  createEffect(() => {
    void query();
    void sortKey();
    void sortDir();
    setPage(1);
  });

  createEffect(() => {
    const max = pageCount();
    if (page() > max) setPage(max);
  });

  const pageRows = createMemo(() => {
    const start = (page() - 1) * PAGE_SIZE;
    return filtered().slice(start, start + PAGE_SIZE);
  });

  function toggleSort(next: SortKey): void {
    if (sortKey() === next) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(next);
    setSortDir(next === "uid" || next === "agent" ? "asc" : "desc");
  }

  function sortMark(key: SortKey): string {
    if (sortKey() !== key) return "";
    return sortDir() === "asc" ? " ↑" : " ↓";
  }

  function openMiner(neuron: PublicNeuron, ev: Event): void {
    const target = ev.target as Element;
    if (target.closest("button") || target.closest("a") || target.closest(".copy")) return;
    pushEntityRoute("miner", neuron.hotkey);
  }

  return (
    <section class="page active" data-page="metagraph">
      <ChainEconomicsStrip chain={chain()} hideLink />
      <div class="metagraph-board">
        <header class="metagraph-toolbar">
          <div class="metagraph-toolbar-lead">
            <p class="metagraph-toolbar-title">Miners</p>
            <p class="metagraph-count" aria-live="polite">
              {filtered().length.toLocaleString()}
              {query().trim() ? " match" : ""}
              {filtered().length === 1 ? " miner" : " miners"}
              <Show when={pageCount() > 1}>
                <span>
                  {" "}
                  · page {page()}/{pageCount()}
                </span>
              </Show>
            </p>
          </div>
          <label class="metagraph-search">
            <span class="sr-only">Filter miners</span>
            <input
              type="search"
              placeholder="Filter by agent, UID, hotkey, or coldkey"
              value={query()}
              onInput={(e) => setQuery(e.currentTarget.value)}
            />
          </label>
        </header>
        <div class="metagraph-table-wrap">
          <div class="metagraph-grid" role="table" aria-label="SN118 miners">
            <div class="metagraph-head" role="row">
              <div class="col-uid" role="columnheader">
                <button type="button" onClick={() => toggleSort("uid")}>
                  UID{sortMark("uid")}
                </button>
              </div>
              <div class="col-agent" role="columnheader">
                <button type="button" onClick={() => toggleSort("agent")}>
                  Agent{sortMark("agent")}
                </button>
              </div>
              <div class="col-key" role="columnheader">
                Hotkey
              </div>
              <div class="col-key" role="columnheader">
                Coldkey
              </div>
              <div class="col-num" role="columnheader">
                <button type="button" onClick={() => toggleSort("score")}>
                  Official score{sortMark("score")}
                </button>
              </div>
              <div class="col-num" role="columnheader">
                <button type="button" onClick={() => toggleSort("stake")}>
                  Stake α{sortMark("stake")}
                </button>
              </div>
              <div class="col-num" role="columnheader">
                <button type="button" onClick={() => toggleSort("incentive")}>
                  Incentive{sortMark("incentive")}
                </button>
              </div>
              <div class="col-num" role="columnheader">
                <button type="button" onClick={() => toggleSort("consensus")}>
                  Consensus{sortMark("consensus")}
                </button>
              </div>
              <div class="col-num" role="columnheader">
                <button type="button" onClick={() => toggleSort("emission")}>
                  Emission α{sortMark("emission")}
                </button>
              </div>
              <div class="col-num" role="columnheader">
                <button type="button" onClick={() => toggleSort("tao_day")}>
                  τ/day{sortMark("tao_day")}
                </button>
              </div>
              <div class="col-active" role="columnheader">
                Chain active
              </div>
            </div>
            <Show
              when={pageRows().length > 0}
              fallback={<p class="metagraph-empty">No miners match this filter.</p>}
            >
              <For each={pageRows()}>
                {(neuron, index) => {
                  const info = () => enrich(neuron);
                  const label = () => displayName(neuron);
                  const taoDay = () => rowTaoDay(neuron);
                  return (
                    <div
                      class="metagraph-row"
                      role="row"
                      classList={{
                        odd: index() % 2 === 0,
                        even: index() % 2 === 1,
                        inactive: !neuron.is_active,
                      }}
                      tabindex="0"
                      aria-label={`${label()}, UID ${neuron.uid}`}
                      onClick={(ev) => openMiner(neuron, ev)}
                      onKeyDown={(ev) => {
                        if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") {
                          return;
                        }
                        ev.preventDefault();
                        openMiner(neuron, ev);
                      }}
                    >
                      <div class="col-uid mono" role="cell">
                        {neuron.uid}
                      </div>
                      <div class="col-agent" role="cell">
                        <div class="metagraph-agent-cell">
                          <MinerAvatar url={info().avatarUrl} />
                          <span class="metagraph-agent" classList={{ empty: !info().agentName }}>
                            {label()}
                          </span>
                        </div>
                      </div>
                      <div class="col-key" role="cell">
                        <div class="metagraph-key" title={neuron.hotkey}>
                          <span class="mono">{shortKey(neuron.hotkey)}</span>
                          <CopyButton class="metagraph-copy" value={neuron.hotkey} label="hotkey" />
                        </div>
                      </div>
                      <div class="col-key" role="cell">
                        <div class="metagraph-key" title={neuron.coldkey}>
                          <span class="mono">{shortKey(neuron.coldkey)}</span>
                          <CopyButton
                            class="metagraph-copy"
                            value={neuron.coldkey}
                            label="coldkey"
                          />
                        </div>
                      </div>
                      <div class="col-num mono" role="cell">
                        {info().score == null ? "—" : info().score!.toFixed(4)}
                      </div>
                      <div class="col-num mono" role="cell">
                        {neuron.stake.toFixed(3)}
                      </div>
                      <div class="col-num mono" role="cell">
                        {neuron.incentive.toFixed(4)}
                      </div>
                      <div class="col-num mono" role="cell">
                        {neuron.consensus.toFixed(4)}
                      </div>
                      <div class="col-num mono metagraph-emphasis" role="cell">
                        {neuron.emission.toFixed(4)}
                      </div>
                      <div class="col-num mono" role="cell">
                        {taoDay() == null ? "—" : taoDay()!.toFixed(2)}
                      </div>
                      <div class="col-active" role="cell">
                        <span
                          class="metagraph-active"
                          classList={{ on: neuron.is_active, off: !neuron.is_active }}
                        >
                          {neuron.is_active ? "yes" : "no"}
                        </span>
                      </div>
                    </div>
                  );
                }}
              </For>
            </Show>
          </div>
        </div>
        <Pager
          class="pager bottom metagraph-pager"
          label="Metagraph pages"
          info={`Page ${page()} of ${pageCount()}`}
          prevDisabled={page() <= 1}
          nextDisabled={page() >= pageCount()}
          onPrev={() => {
            if (page() > 1) setPage(page() - 1);
          }}
          onNext={() => {
            if (page() < pageCount()) setPage(page() + 1);
          }}
        />
        <p class="metagraph-footnote">
          Mining UIDs only, including validator-permit hotkeys with positive miner incentive.
          Stake/emission are on-chain α. τ/day is a projection: last-tempo emission × tempos/day ×
          current α/TAO. Agent, official score, and avatar come from the public leaderboard and are
          matched by miner hotkey — click a row for miner history.
        </p>
      </div>
    </section>
  );
}
