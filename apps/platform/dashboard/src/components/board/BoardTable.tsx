// The board itself: toolbar (view tabs + the in-place filter), the
// compact table with its sortable headers, family child rows, and pager. Ports render()'s row half
// (renderBoardRows 5037–5188), the view
// controls (5267–5352), boardMatches (5027–5035) and boardCompare
// (3876–3887). Solid's keyed <For> replaces the sectionChanged innerHTML
// gate: an unchanged slice never rebuilds, so focus survives background
// refreshes by construction.
import { For, Show, createEffect, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import {
  agentLabel,
  agentName,
  publicDisplayName,
  agentVersionLabel,
  fmtMs,
  fx,
  fxScore,
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
  toggleFamily,
  navigateBoardPage,
  writeBoardPage,
} from "./board-state";
import type { BoardSortKey, BoardTab as BoardTabName } from "./board-state";
import {
  ChipTip,
  ContinualScoreChip,
  EfficiencyBonusChip,
  QualityGateChip,
  RankMove,
  RetestSeedChip,
  RolloutChip,
  TokenPenaltyChip,
  V9ConfirmationChip,
} from "./chips";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";
import type { ChainWeightInfo } from "../../types/leaderboard";
import { HandleBadge } from "../ui/HandleBadge";
import { ChainWeightsPanel } from "./ChainWeightsPanel";

// Matching agent name, UID, and hotkey covers how people actually look a
// miner up: by what they called it, by the number on the board, or by
// pasting a key from the chain. Compact family children match by display name;
// their full identity and evidence remain click-loaded.
export function boardMatches(entry: BoardEntry, needle: string): boolean {
  if (!needle) return true;
  const fields: unknown[] = [
    publicDisplayName(entry.agent_name, entry.name_handle),
    entry.agent_name,
    entry.miner_hotkey,
    entry.miner_uid == null ? "" : "uid " + entry.miner_uid,
    entry.miner_uid,
  ];
  (entry.submission_family?.members ?? []).forEach((member) => {
    fields.push(member.agent_name);
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

const BOARD_SORT_LABELS: Record<BoardSortKey, string> = {
  rank: "Rank",
  composite: "Score",
  cost: "Average run cost",
  latency: "Latency",
  first_seen: "First seen",
};

// The card layout under 720px hides the sortable column headers, so the
// toolbar carries this select instead. Every key/direction pair the headers
// can produce is listed: a sort chosen on a wide window survives a resize
// with the select still reflecting it.
const MOBILE_SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "rank:1", label: "Rank · best first" },
  { value: "rank:-1", label: "Rank · lowest first" },
  { value: "composite:-1", label: "Score · high to low" },
  { value: "composite:1", label: "Score · low to high" },
  { value: "cost:1", label: "Avg run cost · low to high" },
  { value: "cost:-1", label: "Avg run cost · high to low" },
  { value: "latency:1", label: "Latency · fastest first" },
  { value: "latency:-1", label: "Latency · slowest first" },
  { value: "first_seen:-1", label: "First seen · newest first" },
  { value: "first_seen:1", label: "First seen · oldest first" },
];

function defaultBoardSort(): boolean {
  return boardSort() === "rank" && boardDir() === 1;
}

function boardSortDirection(): string {
  if (boardSort() === "rank") return boardDir() === 1 ? "best first" : "lowest first";
  if (boardSort() === "first_seen") return boardDir() === 1 ? "oldest first" : "newest first";
  return boardDir() === 1 ? "low to high" : "high to low";
}

function restoreBoardRankOrder(): void {
  setBoardSort("rank");
  setBoardDir(1);
  setBoardPage(1);
  writeBoardPage(false);
}

function boardCompare(a: BoardEntry, b: BoardEntry, settledView: boolean): number {
  // Alternate metric sorts never erase the board's authority boundaries.
  // Finalized entries stay above provisional feedback, and rank-eligible
  // entries stay above zero-score/smoke runs. Without these buckets, sorting
  // cost low-to-high promotes $0 unranked rows above the actual standings.
  const af = isFinalized(a);
  const bf = isFinalized(b);
  if (af !== bf) return af ? -1 : 1;
  const ae = isEligible(a);
  const be = isEligible(b);
  if (ae !== be) return ae ? -1 : 1;

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
    tip: "Current quality score with tool and memory subscores stacked beneath it. Quality is the primary rank key; when active, the efficiency chip shows the value used only to break exact-quality ties.",
  },
  {
    key: "cost",
    label: "Avg run cost",
    class: "num",
    width: "120px",
    tip: "Average platform-metered chat plus embedding spend across completed, non-empty validator runs on this score's benchmark version. A run counts only once its validator posted a score, so a lease abandoned mid-flight is excluded rather than averaged in as a cheap run.",
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
  const tieCopy = emissions.tie_weighting_active
    ? emissions.allocation_mode === "score_ceiling_pool"
      ? " The dethrone threshold is outside the attainable score range, so every best-score evidence tie shares the full miner pool equally, even beyond the normal tail cutoff."
      : " Evidence-tied recipients pool only the ranked shares of the slots they occupy."
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
    rankedCopy +
    tieCopy
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
  const showsEfficiencyTieBreak = (): boolean => props.entry.efficiency_factor != null;
  const band = (): { lo: number; hi: number; width: number } | null =>
    showsCompositeErrBand(props.entry, props.store.settledView())
      ? errBandBounds(value(), props.entry.composite_stderr)
      : null;
  return (
    <td class="scores-cell">
      <div class="score-stack">
        <div class="score-stack-row current">
          <span class="score-stack-label">{showsEfficiencyTieBreak() ? "Quality" : "KOTH"}</span>
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
            <span class="mval">{fxScore(value())}</span>
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
          <RolloutChip
            entry={props.entry}
            settledView={props.store.settledView()}
            desiredVersion={props.store.bench().desired ?? props.store.bench().active}
          />
          <V9ConfirmationChip
            entry={props.entry}
            mode={props.store.payload()?.v9_confirmation_mode}
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
  /** The reigning champion's display rank, or null when there is no ranked
   * champion. Rows ranked above it outscore the incumbent without having
   * dethroned it — they dim and carry the "crown held" note. */
  championRank: number | null;
}): JSX.Element {
  const e = (): BoardEntry => props.entry;
  const v9ConfirmationSuppressed = (): boolean =>
    props.store.payload()?.v9_confirmation_mode === "enforce" &&
    (e().v9_confirmation_status === "base_only" || e().v9_confirmation_status === "provisional");
  const elig = (): boolean => isEligible(e()) && !v9ConfirmationSuppressed();
  const finalizedEntry = (): boolean => isFinalized(e()) && !v9ConfirmationSuppressed();
  const registered = (): boolean => isRegistered(e());
  const emission = (): ReturnType<LeaderboardStore["emissionFor"]> =>
    v9ConfirmationSuppressed() ? null : props.store.emissionFor(e().agent_id);
  const chainInfo = (): ChainWeightInfo | null =>
    props.store.chainFold()?.byHotkey[e().miner_hotkey] ?? null;
  const isChamp = (): boolean => emission()?.role === "champion";
  const isJointChamp = (): boolean => emission()?.role === "joint_champion";
  // A row that outranks the reigning incumbent without having dethroned it:
  // the exact state that reads as "why isn't raw #1 the champion?". Only
  // meaningful for ranked, finalized rows while the champion itself sits
  // below raw #1.
  const aboveChampion = (): boolean =>
    finalizedEntry() &&
    elig() &&
    !isChamp() &&
    !isJointChamp() &&
    e().rank != null &&
    props.championRank != null &&
    props.championRank > 1 &&
    (e().rank as number) < props.championRank;
  const aboveChampionTip = (): string => {
    const emissions = props.store.emissions();
    const decision = emissions?.raw_leader_decision;
    const isRawLeader = String(emissions?.raw_leader_agent_id) === String(e().agent_id);
    if (decision && isRawLeader) {
      const threshold =
        typeof decision.required_score === "number" && Number.isFinite(decision.required_score)
          ? " The challenger score must exceed " + fxScore(decision.required_score) + "."
          : "";
      return (
        "Rank #1 challenger, not champion. Its KOTH lead is +" +
        fxScore(decision.challenger_lead) +
        "; the current fold requires more than +" +
        fxScore(decision.required_lead) +
        "." +
        threshold
      );
    }
    return (
      "Scores above the reigning champion, but the exact fold decision has not moved the crown. " +
      "Raw rank alone never replaces the first-seen incumbent."
    );
  };
  const aboveChampionLabel = (): string => {
    const emissions = props.store.emissions();
    return String(emissions?.raw_leader_agent_id) === String(e().agent_id)
      ? "#1 challenger · crown held"
      : "outscores · crown held";
  };
  // Rank medals (r1–r3) require live emission eligibility:
  // e.emission_eligible === true, never a looser truthiness.
  const rankCls = (): string =>
    finalizedEntry() && elig() && e().emission_eligible === true && (e().rank as number) <= 3
      ? " r" + e().rank
      : "";
  const kind = (): "zero" | "provisional" | null => unrankedKind(e());
  const displayName = (): string => publicDisplayName(e().agent_name, e().name_handle);
  const rowLabel = (): string =>
    (elig()
      ? (finalizedEntry() ? "Rank " : "Provisional rank ") + e().rank
      : kind() === "zero"
        ? "Unranked, scored zero"
        : "Provisional, unranked") +
    ", agent " +
    agentLabel(e().agent_name, e().agent_version) +
    ", composite " +
    fxScore(displayComposite(e(), props.store.settledView())) +
    (emission() ? ", KOTH " + emission()?.role : "") +
    (aboveChampion() ? ", outscores the champion but has not cleared the dethrone band" : "") +
    ". Activate for run detail.";
  // KOTH standing class first, then the chain-weight sync tint: gold when at
  // least one revealed vector crowns this miner its top choice, magenta when
  // vectors assign it weight without the crown. A ranked row with NEITHER is
  // the out-of-sync state the tints exist to expose — it stays untinted.
  const rowClass = (): string | undefined => {
    const standing = isChamp()
      ? "champion"
      : isJointChamp()
        ? "joint-champion"
        : aboveChampion()
          ? "above-champion"
          : "";
    const info = chainInfo();
    const chain = info ? (info.champion ? " chain-top" : " chain-weighted") : "";
    return (standing + chain).trim() || undefined;
  };
  const familyKey = (): string => String(e().agent_id);
  const familyMembers = () => e().submission_family?.members ?? [];
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
        class={rowClass()}
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
                  <Show when={isChamp()}>
                    <span class="rank-crown" aria-hidden="true">
                      ♛
                    </span>
                  </Show>
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
                <HandleBadge handle={e().name_handle} />
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
                <Show when={!finalizedEntry() && !v9ConfirmationSuppressed()}>
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
                    text="This hotkey is not currently registered on SN118. Its score is retained, but it cannot hold the KOTH crown or receive active weights and emissions until the same hotkey registers again."
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
        <td class="emissions-cell">
          <Show when={emission()} fallback={<span class="muted">–</span>}>
            {(recipient) => (
              <span class={"emission-badge " + recipient().role}>
                <span aria-hidden="true">
                  {recipient().role === "champion" || recipient().role === "joint_champion"
                    ? "♛"
                    : "●"}
                </span>{" "}
                {(recipient().role === "champion"
                  ? "Champion"
                  : recipient().role === "joint_champion"
                    ? "Joint crown"
                    : "Tail") +
                  " · " +
                  pct(recipient().share_of_miner_pool as number)}
              </span>
            )}
          </Show>
          <Show when={aboveChampion()}>
            <ChipTip class="above-champion-note tip-chip" text={aboveChampionTip()}>
              {aboveChampionLabel()}
            </ChipTip>
          </Show>
          <Show when={chainInfo()}>
            {(info) => (
              <>
                <ChipTip
                  class={
                    "chain-weight-note tip-chip " + (info().champion ? "top-choice" : "support")
                  }
                  text={
                    "On-chain weights are currently set on this miner. " +
                    (info().champion
                      ? info().champion + " of " + info().vectors + " revealed validator vectors"
                      : info().weighted + " of " + info().vectors + " revealed validator vectors") +
                    (info().champion
                      ? " crown it their top choice."
                      : " assign it weight without crowning it.") +
                    " Block " +
                    (props.store.chainWeights()?.block ?? "") +
                    "; this can lag active commitments and is not final Yuma emissions."
                  }
                >
                  <span class="chain-weight-dot" aria-hidden="true" />
                  {chainWeightLabel(info())}
                </ChipTip>
                {/* The ghost overlay of the weights themselves: this miner's
                    mean share of the revealed miner-weight mass. */}
                <div
                  class={"chain-weight-meter " + (info().champion ? "top-choice" : "support")}
                  title={
                    pct(info().share) +
                    " mean share of the revealed on-chain miner-weight mass across " +
                    info().vectors +
                    " validator vectors."
                  }
                >
                  <span class="chain-weight-meter-track" aria-hidden="true">
                    <i
                      style={{
                        width: (Math.max(0, Math.min(1, info().share)) * 100).toFixed(1) + "%",
                      }}
                    />
                  </span>
                  <span class="chain-weight-meter-value">{pct(info().share)}</span>
                </div>
              </>
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
            {/* "completed", not "settled": the average covers only leases whose
                validator posted a score, so this count is a handful of runs
                rather than every lease the agent was issued. */}
            <span>{e().inference_run_count} completed</span>
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
        <td class="num hide-sm first-seen-cell muted">{relTime(e().first_seen)}</td>
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
                  {fxScore(member.canonical_composite)}
                </span>
                <span class="family-member-state">
                  <RetestSeedChip count={Number(member.confirmation_seed_depth) || 0} />
                  <span>Scored · not independently ranked · represented by {displayName()}</span>
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
  // The champion's display rank drives the above-the-champion dimming; null
  // (no fold, suppressed champion, or unranked) turns the treatment off.
  const championRank = createMemo<number | null>(() => {
    const champ = store.champion();
    return champ && typeof champ.rank === "number" ? champ.rank : null;
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

  function chooseMobileSort(value: string): void {
    const [key, dir] = value.split(":");
    if (!key || !(key in BOARD_SORTS)) return;
    setBoardSort(key as BoardSortKey);
    setBoardDir(dir === "-1" ? -1 : 1);
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
      aria-label="Leaderboard table, shown as stacked cards on small screens"
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
        <label class="board-sort-mobile">
          <span class="board-sort-mobile-label" aria-hidden="true">
            Sort
          </span>
          <select
            class="board-sort-select"
            aria-label="Sort the leaderboard"
            onChange={(ev) => chooseMobileSort(ev.currentTarget.value)}
          >
            <For each={MOBILE_SORT_OPTIONS}>
              {(opt) => (
                <option value={opt.value} selected={opt.value === boardSort() + ":" + boardDir()}>
                  {opt.label}
                </option>
              )}
            </For>
          </select>
        </label>
        <Show when={!defaultBoardSort()}>
          <div class="board-sort-state" role="status" aria-live="polite">
            <span>
              Sorted by <strong>{BOARD_SORT_LABELS[boardSort()]}</strong> · {boardSortDirection()}.
              Canonical ranks stay fixed.
            </span>
            <button class="board-sort-reset" type="button" onClick={restoreBoardRankOrder}>
              Restore rank order
            </button>
          </div>
        </Show>
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
      <ChainWeightsPanel store={store} />
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
                      {boardSort() === header.key ? (boardDir() === -1 ? "▼" : "▲") : "↕"}
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
                      championRank={championRank()}
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
