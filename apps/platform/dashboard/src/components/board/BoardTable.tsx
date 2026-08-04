// The board itself: toolbar (view tabs + the in-place filter), the
// compact table with its sortable headers, family child rows, and the
// pager. Ports render()'s row half (renderBoardRows 5037–5188), the view
// controls (5267–5352), boardMatches (5027–5035) and boardCompare
// (3876–3887). Solid's keyed <For> replaces the sectionChanged innerHTML
// gate: an unchanged slice never rebuilds, so focus and expanded state
// survive background refreshes by construction.
import { For, Show, createEffect, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import {
  agentLabel,
  agentName,
  agentVersionLabel,
  fmtMs,
  fx,
  pct,
  relTime,
  shortKey,
} from "../../lib/format";
import {
  displayComposite,
  chainWeightLabel,
  errBandBounds,
  isEligible,
  isFinalized,
  isRegistered,
  showsCompositeErrBand,
  unrankedKind,
} from "../../lib/scoring";
import { pushEntityRoute } from "../../stores/routeStore";
import { CopyButton } from "../shell/CopyButton";
import { EntityButton } from "../ui/EntityButton";
import { Pager } from "../ui/Pager";
import { Sparkline } from "../ui/Sparkline";
import { EmptyRow } from "../ui/States";
import {
  boardDir,
  boardPage,
  boardPageSize,
  boardQuery,
  boardSort,
  boardTab,
  expandedFamilies,
  persistRanks,
  setBoardDir,
  setBoardPage,
  setBoardQuery,
  setBoardSort,
  setBoardTab,
  navigateBoardPage,
  toggleFamily,
  writeBoardPage,
} from "./board-state";
import type { BoardSortKey, BoardTab as BoardTabName } from "./board-state";
import {
  ChipTip,
  ContinualScoreChip,
  EfficiencyBonusChip,
  QualityGateChip,
  RankMove,
  RolloutChip,
  TokenPenaltyChip,
} from "./chips";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

// Matching agent name, UID, and hotkey covers how people actually look a
// miner up: by what they called it, by the number on the board, or by
// pasting a key from the chain. Family members match their parent row too.
export function boardMatches(entry: BoardEntry, needle: string): boolean {
  if (!needle) return true;
  const fields: unknown[] = [
    entry.agent_name,
    entry.miner_hotkey,
    entry.miner_uid == null ? "" : "uid " + entry.miner_uid,
    entry.miner_uid,
  ];
  (entry.submission_family?.members ?? []).forEach((member) => {
    fields.push(member.agent_name, member.miner_hotkey);
  });
  return fields.some(
    (field) =>
      String(field == null ? "" : field)
        .toLowerCase()
        .indexOf(needle) >= 0,
  );
}

// Sort accessors. Null/undefined always sorts last regardless of direction,
// so an unranked or legacy row never displaces real data at the top.
const BOARD_SORTS: Record<BoardSortKey, (e: BoardEntry, settledView: boolean) => unknown> = {
  rank: (e) => e.rank,
  composite: (e, settledView) => displayComposite(e, settledView),
  cost: (e) => e.average_run_cost_microusd,
  latency: (e) => e.median_ms,
  first_seen: (e) => (e.first_seen ? Date.parse(e.first_seen) : null),
};

function boardCompare(a: BoardEntry, b: BoardEntry, settledView: boolean): number {
  const get = BOARD_SORTS[boardSort()] || BOARD_SORTS.rank;
  const av = get(a, settledView) as number | string | null | undefined;
  const bv = get(b, settledView) as number | string | null | undefined;
  const an = av === null || av === undefined || av !== av;
  const bn = bv === null || bv === undefined || bv !== bv;
  if (an || bn) return an && bn ? 0 : an ? 1 : -1;
  if (av < bv) return -1 * boardDir();
  if (av > bv) return 1 * boardDir();
  // Composite breaks ties so equal sort keys stay in a stable, meaningful
  // order rather than whatever the source array happened to hold.
  return (displayComposite(b, settledView) || 0) - (displayComposite(a, settledView) || 0);
}

interface HeaderSpec {
  key: BoardSortKey | null;
  label: string;
  tip: string;
  class?: string;
  width?: string;
}

const HEADERS: HeaderSpec[] = [
  {
    key: "rank",
    label: "Ranked agent",
    width: "300px",
    tip: "Score rank and the miner's best-scoring agent. Open either identity for details, or copy the hotkey. Provisional runs remain unranked (–).",
  },
  {
    key: null,
    label: "Emissions",
    width: "128px",
    tip: "Current KOTH role. The incumbent champion takes a fixed share of the miner pool; a participation tail splits the remainder.",
  },
  {
    key: "composite",
    label: "Scores",
    width: "280px",
    tip: "Current ranking score with tool and memory subscores stacked beneath it. Sort uses the current ranking score.",
  },
  {
    key: "cost",
    label: "Avg run cost",
    class: "num",
    width: "120px",
    tip: "Average platform-metered chat plus embedding spend across settled, non-empty validator runs on this score's benchmark version.",
  },
  {
    key: "latency",
    label: "Latency",
    class: "num hide-sm",
    width: "96px",
    tip: "Median per-case response time for the run, and how many cases were scored (a full benchmark is ~114 cases).",
  },
  {
    key: "first_seen",
    label: "First seen",
    class: "num hide-sm",
    width: "92px",
    tip: "When this miner's winning agent was first uploaded to the platform.",
  },
];

/** The emissions column tooltip, rewritten from the fold once it arrives
 * (applyEmissionsCopy 3666–3686) — consensus constants are read from the
 * fold the API reports, never written into the copy. */
function emissionsColTip(store: LeaderboardStore): string {
  const emissions = store.emissions();
  const share = emissions?.champion_share;
  if (!emissions || typeof share !== "number" || !Number.isFinite(share)) {
    return "Current KOTH role. The incumbent champion takes a fixed share of the miner pool; a participation tail splits the remainder.";
  }
  const tailSize =
    typeof emissions.tail_size === "number" && Number.isFinite(emissions.tail_size)
      ? emissions.tail_size
      : null;
  const rankShares = Array.isArray(emissions.rank_shares)
    ? emissions.rank_shares.filter((value) => Number.isFinite(value))
    : [];
  const rankedCopy = rankShares.length
    ? " Ranked shares: " + rankShares.map(pct).join(" / ") + "."
    : "";
  return (
    "Current KOTH role. The base ranked schedule gives the incumbent " +
    pct(share) +
    " of the miner pool" +
    (tailSize === 0
      ? "; there is no participation tail."
      : "; up to " +
        (tailSize == null ? "the configured number of" : tailSize) +
        " participation-tail recipients receive descending shares of the remaining " +
        pct(1 - share) +
        ".") +
    rankedCopy
  );
}

function Bar(props: { kind: "tool" | "memory"; value: number }): JSX.Element {
  return (
    <div class="metric">
      <div class="barwrap">
        <div
          class={"bar " + props.kind}
          style={{ width: (Math.max(0, Math.min(1, props.value)) * 100).toFixed(1) + "%" }}
        />
      </div>
      <span class="mval">{fx(props.value)}</span>
    </div>
  );
}

// Composite cell = bar (with a ±1-SE uncertainty band) + value, then a
// second line carrying the trend sparkline and the chip vocabulary
// (compositeCell 5582–5601).
function ScoreStackCell(props: { entry: BoardEntry; store: LeaderboardStore }): JSX.Element {
  const value = (): number => displayComposite(props.entry, props.store.settledView());
  const band = (): { lo: number; hi: number; width: number } | null =>
    showsCompositeErrBand(props.entry, props.store.settledView())
      ? errBandBounds(value(), props.entry.composite_stderr)
      : null;
  return (
    <td class="scores-cell">
      <div class="score-stack">
        <div class="score-stack-row current">
          <span class="score-stack-label">Current</span>
          <div class="metric">
            <div class="barwrap">
              <Show when={band()}>
                {(b) => (
                  <div
                    class="err"
                    title={
                      "±" +
                      fx(props.entry.composite_stderr as number) +
                      " (1 SE), measurement uncertainty"
                    }
                    style={{
                      left: b().lo.toFixed(1) + "%",
                      width: b().width.toFixed(1) + "%",
                    }}
                  />
                )}
              </Show>
              <div
                class="bar composite"
                style={{ width: (Math.max(0, Math.min(1, value())) * 100).toFixed(1) + "%" }}
              />
            </div>
            <span class="mval">{fx(value())}</span>
          </div>
        </div>
        <div class="score-stack-row">
          <span class="score-stack-label">Tool</span>
          <Bar kind="tool" value={props.entry.tool_mean} />
        </div>
        <div class="score-stack-row">
          <span class="score-stack-label">Memory</span>
          <Bar kind="memory" value={props.entry.memory_mean} />
        </div>
        <div class="cline2 score-stack-context">
          <Sparkline history={props.entry.history} />
          <RolloutChip
            entry={props.entry}
            settledView={props.store.settledView()}
            desiredVersion={props.store.bench().desired ?? props.store.bench().active}
          />
          <ContinualScoreChip entry={props.entry} />
          <EfficiencyBonusChip entry={props.entry} />
          <QualityGateChip entry={props.entry} />
          <TokenPenaltyChip entry={props.entry} />
        </div>
      </div>
    </td>
  );
}

function formatRunCost(value: number | null | undefined): string {
  const dollars = Math.max(0, Number(value) || 0) / 1_000_000;
  return "$" + dollars.toFixed(dollars >= 1 ? 2 : 3);
}

function BoardRow(props: {
  entry: BoardEntry;
  index: number;
  store: LeaderboardStore;
  chainRegistrationUnknown: boolean;
}): JSX.Element {
  const e = (): BoardEntry => props.entry;
  const elig = (): boolean => isEligible(e());
  const finalizedEntry = (): boolean => isFinalized(e());
  const registered = (): boolean => isRegistered(e());
  const emission = (): ReturnType<LeaderboardStore["emissionFor"]> =>
    props.store.emissionFor(e().agent_id);
  const chainWeight = (): ReturnType<LeaderboardStore["chainFold"]> extends infer _
    ? unknown
    : never => undefined as never;
  void chainWeight;
  const chainInfo = (): { weighted: number; champion: number; vectors: number } | null =>
    props.store.chainFold()?.byHotkey[e().miner_hotkey] ?? null;
  const isChamp = (): boolean => emission()?.role === "champion";
  // Rank medals (r1–r3) require live emission eligibility:
  // e.emission_eligible === true, never a looser truthiness.
  const rankCls = (): string =>
    finalizedEntry() && elig() && e().emission_eligible === true && (e().rank as number) <= 3
      ? " r" + e().rank
      : "";
  const kind = (): "zero" | "provisional" | null => unrankedKind(e());
  const displayName = (): string => agentName(e().agent_name);
  const rowLabel = (): string =>
    (elig()
      ? (finalizedEntry() ? "Rank " : "Provisional rank ") + e().rank
      : kind() === "zero"
        ? "Unranked, scored zero"
        : "Provisional, unranked") +
    ", agent " +
    agentLabel(e().agent_name, e().agent_version) +
    ", composite " +
    fx(displayComposite(e(), props.store.settledView())) +
    (emission() ? ", KOTH " + emission()?.role : "") +
    ". Activate for run detail.";
  const familyKey = (): string => String(e().agent_id);
  const familyMembers = (): NonNullable<NonNullable<BoardEntry["submission_family"]>["members"]> =>
    (e().submission_family?.members ?? []).filter(
      (member) => String(member.agent_id) !== String(e().agent_id),
    );
  const familyExpanded = (): boolean => expandedFamilies().has(familyKey());

  function activate(ev: Event): void {
    const target = ev.target as Element;
    if (
      target.closest(".copy") ||
      target.closest("[data-entity-link]") ||
      target.closest("[data-family-toggle]")
    ) {
      return;
    }
    pushEntityRoute("miner", e().miner_hotkey);
  }

  return (
    <>
      <tr
        data-i={props.index}
        class={isChamp() ? "champion" : undefined}
        tabindex="0"
        aria-label={rowLabel()}
        onClick={activate}
        onKeyDown={(ev) => {
          // Keyboard activation of a focused row (Enter or Space). The row is
          // a plain tabbable row (no role="button", which would forbid the
          // nested links/copy buttons).
          if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
          const target = ev.target as Element;
          if (
            target.closest(".copy") ||
            target.closest("[data-entity-link]") ||
            target.closest("[data-family-toggle]")
          ) {
            return;
          }
          ev.preventDefault();
          pushEntityRoute("miner", e().miner_hotkey);
        }}
      >
        <td class="ranked-agent-cell">
          <div class="ranked-agent">
            <div class="ranked-agent-rank">
              <Show
                when={elig()}
                fallback={
                  <ChipTip
                    class="rank prov-rank tip-chip"
                    text={
                      "Not ranked (" +
                      (kind() === "zero" ? "scored 0.000" : "provisional run") +
                      ")."
                    }
                  >
                    –
                  </ChipTip>
                }
              >
                <span class={"rank" + rankCls()}>
                  {finalizedEntry() ? e().rank : "P" + e().rank}
                </span>
                <Show when={finalizedEntry()}>
                  <RankMove hotkey={e().miner_hotkey} rank={e().rank as number} />
                </Show>
              </Show>
            </div>
            <span class="winner-identity">
              <span class="winner-name">
                <EntityButton kind="agent" id={e().agent_id} label={displayName()} />
                <Show when={kind() === "zero"}>
                  <ChipTip
                    class="prov tip-chip"
                    text="This full run scored a composite of 0.000, so it is shown for transparency but is not ranked and not emission-eligible."
                  >
                    no score
                  </ChipTip>
                </Show>
                <Show when={kind() === "provisional"}>
                  <ChipTip
                    class="prov tip-chip"
                    text="Provisional. It ran a smaller profile that did not administer the full benchmark, so it is shown for transparency but is not ranked and not emission-eligible."
                  >
                    provisional
                  </ChipTip>
                </Show>
                <Show when={!finalizedEntry()}>
                  <ChipTip
                    class="quorum-badge tip-chip"
                    text={
                      "Accepted score feedback; final at " +
                      (e().score_quorum || 3) +
                      " independent validators."
                    }
                  >
                    {(e().score_count || 0) + " of " + (e().score_quorum || 3) + " · provisional"}
                  </ChipTip>
                </Show>
                <Show when={!registered() && e().registered === false}>
                  <ChipTip
                    class="prov tip-chip"
                    text="This hotkey is not currently registered on SN118. Its score is retained, but it is excluded from active weights and emissions until the same hotkey registers again."
                  >
                    not registered
                  </ChipTip>
                </Show>
                <Show
                  when={
                    !registered() &&
                    e().registered !== false &&
                    // Chain unreachable for the whole snapshot: the notice above
                    // the board explains it once, so the identical per-row badge
                    // is suppressed.
                    !props.chainRegistrationUnknown
                  }
                >
                  <ChipTip
                    class="prov tip-chip"
                    text="Current SN118 registration could not be confirmed. The score is retained, but active weight and emission eligibility are unknown."
                  >
                    unconfirmed
                  </ChipTip>
                </Show>
              </span>
              <span class="submission-version">{agentVersionLabel(e().agent_version)}</span>
              <Show when={familyMembers().length}>
                <button
                  type="button"
                  class="family-toggle"
                  data-family-toggle={familyKey()}
                  aria-expanded={familyExpanded() ? "true" : "false"}
                  aria-controls={"family-" + familyKey()}
                  onClick={(ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    toggleFamily(familyKey());
                  }}
                >
                  {familyMembers().length +
                    (familyMembers().length === 1 ? " other submission" : " other submissions")}
                </button>
              </Show>
              <span class="winner-miner" title={e().miner_hotkey}>
                <span class="winner-miner-label" aria-hidden="true">
                  Miner
                </span>
                <span class="miner-uid" title="Current SN118 UID">
                  UID {e().miner_uid == null ? "–" : e().miner_uid}
                </span>
                <span class="hotkey">
                  <EntityButton
                    kind="miner"
                    id={e().miner_hotkey}
                    label={shortKey(e().miner_hotkey)}
                  />
                </span>
                <CopyButton value={e().miner_hotkey} label="full hotkey" />
              </span>
            </span>
          </div>
        </td>
        <td>
          <Show when={emission()} fallback={<span class="muted">–</span>}>
            {(recipient) => (
              <span class={"emission-badge " + recipient().role}>
                <span aria-hidden="true">●</span>{" "}
                {(recipient().role === "champion" ? "Champion" : "Tail") +
                  " · " +
                  pct(recipient().share_of_miner_pool as number)}
              </span>
            )}
          </Show>
          <Show when={chainInfo()}>
            {(info) => (
              <ChipTip
                class="chain-weight-note tip-chip"
                text={
                  "How many validators most recently revealed this miner as their top choice, or assigned it any weight. Block " +
                  (props.store.chainWeights()?.block ?? "") +
                  "; this can lag active commitments and is not final Yuma emissions."
                }
              >
                {chainWeightLabel(info())}
              </ChipTip>
            )}
          </Show>
        </td>
        <ScoreStackCell entry={e()} store={props.store} />
        <td class="num run-cost-cell">
          <Show
            when={e().average_run_cost_microusd != null && (e().inference_run_count || 0) > 0}
            fallback={<span class="muted">–</span>}
          >
            <strong>{formatRunCost(e().average_run_cost_microusd)}</strong>
            <span>{e().inference_run_count} settled</span>
          </Show>
        </td>
        <Show when={e().median_ms != null} fallback={<td class="num hide-sm lat muted">–</td>}>
          <td class="num hide-sm lat">
            {fmtMs(e().median_ms as number)}
            <Show when={e().n != null}>
              <div class="cases">{e().n} cases</div>
            </Show>
          </td>
        </Show>
        <td class="num hide-sm muted">{relTime(e().first_seen)}</td>
      </tr>
      <For each={familyMembers()}>
        {(member, familyIndex) => (
          <tr
            class="family-child"
            data-family-parent={familyKey()}
            hidden={!familyExpanded()}
            id={familyIndex() === 0 ? "family-" + familyKey() : undefined}
          >
            <td class="family-branch" aria-hidden="true">
              ↳
            </td>
            <td colspan="5">
              <div class="family-member">
                <span class="family-member-name">
                  <EntityButton
                    kind="agent"
                    id={member.agent_id}
                    label={agentName(member.agent_name)}
                  />
                  <span class="submission-version">{agentVersionLabel(member.agent_version)}</span>
                </span>
                <span class="family-member-score" title="Canonical three-validator median">
                  {fx(member.canonical_composite)}
                </span>
                <span class="family-member-state">
                  Scored · not independently ranked · represented by {displayName()}
                </span>
              </div>
            </td>
          </tr>
        )}
      </For>
    </>
  );
}

export function BoardTable(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const needle = (): string => boardQuery().trim().toLowerCase();
  const all = createMemo(() => store.entries().filter((e) => boardMatches(e, needle())));
  const scored = createMemo(() => all().filter((e) => isFinalized(e)));
  const provisional = createMemo(() => all().filter((e) => !isFinalized(e)));
  const counts = (): Record<BoardTabName, number> => ({
    all: all().length,
    scored: scored().length,
    provisional: provisional().length,
  });
  const rows = createMemo(() => {
    const source =
      boardTab() === "all" ? all() : boardTab() === "provisional" ? provisional() : scored();
    return source.slice().sort((a, b) => boardCompare(a, b, store.settledView()));
  });
  const pageCount = (): number => Math.max(1, Math.ceil(rows().length / boardPageSize));
  createEffect(() => {
    // A deep-linked page past the end (or a page emptied by newly-loaded
    // data) clamps to the last real page; keep the URL honest about landing.
    if (boardPage() > pageCount()) {
      setBoardPage(pageCount());
      writeBoardPage(false);
    }
  });
  const pageRows = createMemo(() => {
    const start = (Math.min(boardPage(), pageCount()) - 1) * boardPageSize;
    return rows().slice(start, start + boardPageSize);
  });
  // openModal() indexed lastEntries, so a row carries its index there, not
  // its position in the current page (the documented data-i contract).
  const entryIndex = createMemo(() => {
    const map = new Map<BoardEntry, number>();
    store.entries().forEach((entry, index) => map.set(entry, index));
    return map;
  });
  const chainRegistrationUnknown = createMemo(() => {
    const entries = store.entries();
    return entries.length > 0 && entries.every((e) => e.registered == null);
  });
  createEffect(() => {
    // Rank-movement baseline: rewritten after every successful render so the
    // arrows read "since your last visit".
    if (store.entries().length) persistRanks(store.entries());
  });

  const pinfo = (): string =>
    rows().length
      ? "Page " +
        Math.min(boardPage(), pageCount()) +
        " of " +
        pageCount() +
        " · " +
        rows().length +
        (boardTab() === "all" ? " runs" : boardTab() === "provisional" ? " provisional" : " scored")
      : "Page 1 of 1";

  function chooseTab(tabName: BoardTabName): void {
    if (tabName === boardTab()) return;
    setBoardTab(tabName);
    // Tab/sort aren't URL state, so a reset-to-1 only drops the pager param
    // (replaceState, not push; there is no meaningful entry to go back to).
    setBoardPage(1);
    writeBoardPage(false);
  }

  function chooseSort(key: BoardSortKey): void {
    if (boardSort() === key) {
      setBoardDir(boardDir() === 1 ? -1 : 1);
    } else {
      setBoardSort(key);
      // Rank reads naturally ascending; every measured column is most useful
      // highest-first.
      setBoardDir(key === "rank" ? 1 : -1);
    }
    setBoardPage(1);
    writeBoardPage(false);
  }

  let filterInput: HTMLInputElement | undefined;
  let filterDebounce: ReturnType<typeof setTimeout> | undefined;
  function applyFilter(): void {
    const next = filterInput ? filterInput.value : "";
    if (next === boardQuery()) return;
    setBoardQuery(next);
    // A narrowed list almost never has the page you were on.
    setBoardPage(1);
    writeBoardPage(false);
  }

  return (
    <div
      class="board"
      tabindex="0"
      role="region"
      aria-label="Leaderboard table, horizontally scrollable on small screens"
    >
      <div class="board-toolbar">
        <div class="board-tabs activity-filter-list" role="group" aria-label="Leaderboard view">
          <For each={["all", "scored", "provisional"] as BoardTabName[]}>
            {(tabName) => (
              <button
                class="activity-filter"
                type="button"
                data-board-tab={tabName}
                aria-pressed={boardTab() === tabName ? "true" : "false"}
                onClick={() => chooseTab(tabName)}
              >
                {tabName === "all" ? "All " : tabName === "scored" ? "Scored " : "Provisional "}
                <span class="activity-filter-count" data-board-count={tabName}>
                  {store.payload() || store.entries().length ? counts()[tabName] : "–"}
                </span>
              </button>
            )}
          </For>
        </div>
        <div class="board-search" id="board-search" classList={{ "has-query": !!boardQuery() }}>
          <label class="visually-hidden" for="board-filter">
            Filter the leaderboard
          </label>
          <div class="search-field">
            <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-4-4" />
            </svg>
            <input
              class="search-input"
              id="board-filter"
              type="search"
              autocomplete="off"
              placeholder="Filter by agent, UID, or hotkey"
              ref={(el) => {
                filterInput = el;
              }}
              onInput={() => {
                clearTimeout(filterDebounce);
                filterDebounce = setTimeout(applyFilter, 120);
              }}
              onKeyDown={(event) => {
                // Escape clears rather than just blurring; the filter is the
                // only thing standing between the reader and the full board.
                if (event.key === "Escape" && filterInput?.value) {
                  event.stopPropagation();
                  filterInput.value = "";
                  applyFilter();
                }
              }}
            />
            <button
              class="search-clear"
              id="board-filter-clear"
              type="button"
              aria-label="Clear the leaderboard filter"
              title="Clear filter"
              onClick={() => {
                if (filterInput) filterInput.value = "";
                applyFilter();
                filterInput?.focus();
              }}
            >
              ×
            </button>
          </div>
        </div>
      </div>
      <table
        id="board"
        tabindex="-1"
        aria-label="Subnet 118 leaderboard: one ranked representative per payment-owner submission family"
      >
        <thead>
          <tr>
            <For each={HEADERS}>
              {(header) => (
                <th
                  scope="col"
                  style={header.width ? { width: header.width } : undefined}
                  class={
                    (header.class ? header.class + " " : "") + (header.key ? "sortable" : "") ||
                    undefined
                  }
                  data-sort={header.key ?? undefined}
                  aria-sort={
                    header.key && boardSort() === header.key
                      ? boardDir() === 1
                        ? "ascending"
                        : "descending"
                      : undefined
                  }
                  onClick={header.key ? () => chooseSort(header.key as BoardSortKey) : undefined}
                  onKeyDown={
                    header.key
                      ? (ev) => {
                          // The focusable tooltip term inside the header
                          // doubles as the keyboard sort control.
                          if (ev.key !== "Enter" && ev.key !== " ") return;
                          ev.preventDefault();
                          chooseSort(header.key as BoardSortKey);
                        }
                      : undefined
                  }
                >
                  <ChipTip
                    class="tip"
                    id={header.key ? undefined : "emissions-col-tip"}
                    tabindex={0}
                    role={header.key ? "button" : undefined}
                    text={header.key ? header.tip : emissionsColTip(store)}
                  >
                    {header.label}
                  </ChipTip>
                  <Show when={header.key}>
                    <span class="sarrow" aria-hidden="true">
                      {boardSort() === header.key && boardDir() === -1 ? "▼" : "▲"}
                    </span>
                  </Show>
                </th>
              )}
            </For>
          </tr>
        </thead>
        <tbody id="rows">
          <Show
            when={!store.unavailable()}
            fallback={
              <EmptyRow colspan={6}>
                Could not load live leaderboard data. Try refreshing in a moment.
              </EmptyRow>
            }
          >
            <Show
              when={store.payload()}
              fallback={<EmptyRow colspan={6}>Loading leaderboard…</EmptyRow>}
            >
              <Show
                when={pageRows().length}
                fallback={
                  <EmptyRow colspan={6}>
                    {needle()
                      ? "No miner matches that filter."
                      : boardTab() === "provisional"
                        ? "No provisional runs right now. Pre-quorum scores appear here as validators report them."
                        : "No miners have been scored yet. As soon as a submission clears scoring it appears here."}
                  </EmptyRow>
                }
              >
                <For each={pageRows()}>
                  {(entry) => (
                    <BoardRow
                      entry={entry}
                      index={entryIndex().get(entry) ?? 0}
                      store={store}
                      chainRegistrationUnknown={chainRegistrationUnknown()}
                    />
                  )}
                </For>
              </Show>
            </Show>
          </Show>
        </tbody>
      </table>
      <Pager
        class="pager bottom"
        label="Leaderboard pages"
        info={pinfo()}
        prevId="board-prev"
        nextId="board-next"
        infoId="board-pinfo"
        prevDisabled={boardPage() <= 1}
        nextDisabled={boardPage() >= pageCount()}
        onPrev={() => {
          if (boardPage() > 1) navigateBoardPage(boardPage() - 1);
        }}
        onNext={() => {
          // "next" is disabled at the last page, so this never overshoots.
          navigateBoardPage(boardPage() + 1);
        }}
      />
    </div>
  );
}
