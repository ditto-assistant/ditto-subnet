// The single #leaderboard-block. The monolith kept ONE DOM node and
// re-parented it between the overview column and the dedicated Leaderboard
// page (route() 9793–9797); here the block is one component rendered by both
// routes over the same module-scope view state (board-state) and data store
// (leaderboard-data), so the two mounts stay the same instrument. Contents,
// in monolith order (markup 2655–2732): section head, version switch with the
// history pills + Archive disclosure, the board, then the post-table context
// — emissions strip (dethrone floor, tail recipients, chain observation),
// rollout strip, standing notices. The overview mount hides the head, the
// switch and the strips with page-scoped CSS; nothing here branches on the
// mount beyond the router's enter-the-page reset.
import { For, Show, createEffect, createMemo, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { rolloutStripState } from "../../lib/bench-state";
import {
  agentName,
  esc,
  fx,
  fxScore,
  marginText,
  num,
  pct,
  relDuration,
  shortKey,
} from "../../lib/format";
import {
  crownContest,
  crownSeedDiffsText,
  crownWhyHigh,
  dethroneBandScale,
  dethroneFloor,
  displayComposite,
  efficiencyBoardStatus,
  isEligible,
  isFinalized,
  rolloutQuorum,
  signedScore,
} from "../../lib/scoring";
import type { BandDecayParams } from "../../lib/scoring";
import { Tip } from "../ui/Tooltip";
import { EntityButton } from "../ui/EntityButton";
import { BoardTable } from "./BoardTable";
import {
  leaderboardVersionView,
  restoreBoardPage,
  setBoardPage,
  setLeaderboardVersionView,
} from "./board-state";
import { leaderboardStore } from "./leaderboard-data";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

/** How many historical versions stay on the row beside "Current rollout".
 * Everything older files under Archive, so the row's width stops growing
 * with the benchmark's age instead of gaining a pill per release forever. */
const VISIBLE_VERSION_PILLS = 2;

// ── Version switch (renderVersionPills + wiring, 5358–5533) ──

function VersionSwitch(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  let group: HTMLDivElement | undefined;

  // History pills track every version that has ever been scored. Fall back
  // to the versions visible in the payload for an older API that does not
  // list them, so the pills never regress below what the board shows.
  const available = createMemo<number[]>((prev) => {
    const d = store.payload();
    if (!d) return prev;
    let versions = (d.available_bench_versions || []).slice();
    if (!versions.length) {
      (d.entries || []).forEach((e) => {
        if (e.bench_version != null && versions.indexOf(e.bench_version) < 0)
          versions.push(e.bench_version);
      });
      [d.active_bench_version, d.desired_bench_version].forEach((v) => {
        if (v && versions.indexOf(v) < 0) versions.push(v);
      });
    }
    if (!versions.length) return prev;
    return versions.sort((a, b) => b - a);
  }, [] as number[]);

  // The rollout view, the newest VISIBLE_VERSION_PILLS versions, and an
  // Archive disclosure for anything older. A selected archive version joins
  // the visible row instead of staying behind the trigger: the row always
  // shows what the board is showing, so there is exactly one pressed pill
  // and it is never off-screen.
  const split = createMemo(() => {
    const visible = available().slice(0, VISIBLE_VERSION_PILLS);
    const archived = available().slice(VISIBLE_VERSION_PILLS);
    const selected = archived.indexOf(Number(leaderboardVersionView()));
    if (selected >= 0) visible.push(archived.splice(selected, 1)[0] as number);
    return { visible, archived };
  });

  const contextText = (): string => {
    const d = store.payload();
    if (!d) return "Current rollout drives validator weights.";
    if (d.selection_mode === "historical") {
      return (
        "Historical Bench v" +
        d.current_bench_version +
        " standings. Current emissions remain on the rollout view."
      );
    }
    if (store.settledView()) {
      const active = store.bench().active;
      const desired = d.desired_bench_version;
      return (
        "Rows rank by their settled v" +
        active +
        " score while v" +
        desired +
        " collects. Validator weights stay on v" +
        active +
        " until the v" +
        desired +
        " rollout fully activates."
      );
    }
    return "This pool drives validator weights.";
  };

  function setArchiveOpen(open: boolean): void {
    if (!group) return;
    const trigger = group.querySelector<HTMLButtonElement>(".version-archive-trigger");
    const menu = group.querySelector<HTMLElement>(".version-archive-menu");
    const next = Boolean(open && trigger && menu);
    if (!trigger || !menu) return;
    trigger.setAttribute("aria-expanded", String(next));
    menu.hidden = !next;
  }
  const archiveOpen = (): boolean => {
    const menu = group?.querySelector<HTMLElement>(".version-archive-menu");
    return Boolean(menu && !menu.hidden);
  };

  function choose(view: string, fromArchive: boolean): void {
    if (!view || view === leaderboardVersionView()) return;
    setLeaderboardVersionView(view);
    setBoardPage(1);
    setArchiveOpen(false);
    // Choosing from the archive destroys the button that had focus (the
    // version is now a promoted pill), so hand focus to that pill rather
    // than dropping the keyboard user back at the top of the document.
    if (fromArchive) {
      queueMicrotask(() => {
        group
          ?.querySelector<HTMLButtonElement>('[data-leaderboard-version="' + view + '"]')
          ?.focus();
      });
    }
  }

  // Same dismissal contract as the global search popover: a pointer down
  // outside closes, and so does focus leaving the disclosure entirely. The
  // pointerdown flag covers Safari/iOS, which blur on a tap without focusing
  // the tapped button, so focusout would otherwise close before the click.
  let archivePointerDown = false;
  onMount(() => {
    const onDocPointerDown = (event: PointerEvent): void => {
      const target = event.target as Element | null;
      if (archiveOpen() && !target?.closest(".version-archive")) setArchiveOpen(false);
    };
    const onResize = (): void => {
      // Resizing invalidates the layout, and the pills may re-wrap under it.
      if (archiveOpen()) setArchiveOpen(false);
    };
    document.addEventListener("pointerdown", onDocPointerDown);
    window.addEventListener("resize", onResize);
    onCleanup(() => {
      document.removeEventListener("pointerdown", onDocPointerDown);
      window.removeEventListener("resize", onResize);
    });
  });

  return (
    <div class="leaderboard-version-switch">
      <span class="context" id="leaderboard-version-context">
        {contextText()}
      </span>
      <div
        class="activity-filter-list"
        id="leaderboard-version-pills"
        role="group"
        aria-label="Leaderboard benchmark view"
        ref={(el) => {
          group = el;
        }}
        onPointerDown={(event) => {
          if ((event.target as Element).closest(".version-archive")) {
            archivePointerDown = true;
            setTimeout(() => {
              archivePointerDown = false;
            }, 0);
          }
        }}
        onFocusOut={(event) => {
          if (!archiveOpen() || archivePointerDown) return;
          const next = event.relatedTarget as Element | null;
          if (!next || !next.closest(".version-archive")) setArchiveOpen(false);
        }}
        onKeyDown={(event) => {
          // Disclosure, not a menu widget: Tab already walks the options, so
          // this only adds Escape (close, focus back to the trigger) and
          // arrow stepping.
          if (!archiveOpen()) return;
          if (event.key === "Escape") {
            event.stopPropagation();
            setArchiveOpen(false);
            group?.querySelector<HTMLButtonElement>(".version-archive-trigger")?.focus();
            return;
          }
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          const options = Array.from(
            group?.querySelectorAll<HTMLButtonElement>(".version-archive-option") ?? [],
          );
          if (!options.length) return;
          event.preventDefault();
          const at = options.indexOf(document.activeElement as HTMLButtonElement);
          const next =
            at < 0
              ? event.key === "ArrowDown"
                ? 0
                : options.length - 1
              : (at + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
          options[next]?.focus();
        }}
      >
        <button
          class="activity-filter"
          type="button"
          data-leaderboard-version="current"
          aria-pressed={leaderboardVersionView() === "current" ? "true" : "false"}
          onClick={() => choose("current", false)}
        >
          Current rollout
        </button>
        <For each={split().visible}>
          {(version) => (
            <button
              class="activity-filter"
              type="button"
              data-leaderboard-version={String(version)}
              aria-pressed={String(version) === leaderboardVersionView() ? "true" : "false"}
              onClick={() => choose(String(version), false)}
            >
              {"Bench v" + version}
            </button>
          )}
        </For>
        <Show when={split().archived.length}>
          <div class="version-archive">
            <button
              class="activity-filter version-archive-trigger"
              type="button"
              aria-expanded="false"
              aria-controls="version-archive-menu"
              aria-label="Earlier benchmark versions"
              onClick={() => setArchiveOpen(!archiveOpen())}
            >
              Archive
              <span class="activity-filter-count">{split().archived.length}</span>
              <span class="version-archive-caret" aria-hidden="true">
                ▾
              </span>
            </button>
            <div class="version-archive-menu" id="version-archive-menu" hidden>
              <For each={split().archived}>
                {(version) => (
                  <button
                    class="version-archive-option"
                    type="button"
                    data-leaderboard-version={String(version)}
                    onClick={() => choose(String(version), true)}
                  >
                    {"Bench v" + version}
                  </button>
                )}
              </For>
            </div>
          </div>
        </Show>
      </div>
    </div>
  );
}

// ── KOTH standing callout ────────────────────────────────────
// Rendered between the version switch and the board whenever the reigning
// champion is NOT raw #1 — the one standing that reliably confuses readers
// ("why is #4 the champion?"). It states the first-seen KOTH rule inline,
// above the table, on both mounts (the overview hides the emissions strip,
// so this is the only explanation the compact board carries). Every number
// is read from the fold; the strip below keeps the full math.

function crownComparisonNote(method: string | undefined): string {
  if (method === "paired") {
    return "Rank is each agent's own-seed score. The crown is a head-to-head on shared confirmation seeds, so a lucky private dataset cannot take the title.";
  }
  if (method === "unpaired") {
    return "Not enough shared seeds yet for a paired comparison, so the fold uses an unpaired uncertainty band. More shared retests can change this threshold.";
  }
  return "The first-seen incumbent holds until a challenger clears the flat protection margin.";
}

function KothStandingCallout(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const floor = createMemo(() =>
    dethroneFloor(store.emissions(), store.champion(), store.settledView()),
  );
  const contest = createMemo(() => {
    const emissions = store.emissions();
    const champion = store.champion();
    const decision = emissions?.raw_leader_decision;
    const leader = store
      .entries()
      .find((entry) => String(entry.agent_id) === String(emissions?.raw_leader_agent_id));
    return emissions && champion && decision && leader
      ? { emissions, champion, decision, leader }
      : null;
  });
  const shown = createMemo(() => {
    if (store.unavailable()) return false;
    if (store.emissions()?.allocation_mode === "score_ceiling_pool") return true;
    const champ = store.champion();
    return Boolean(champ && typeof champ.rank === "number" && champ.rank > 1);
  });
  const aboveCount = (): number => ((store.champion()?.rank as number) || 1) - 1;
  const aboveText = (): string => {
    const n = aboveCount();
    return n === 1 ? "1 agent scores higher than it" : n + " agents score higher than it";
  };
  const rawMedianDiffers = (entry: BoardEntry): boolean =>
    Number.isFinite(entry.composite) &&
    Math.abs(entry.composite - displayComposite(entry, store.settledView())) >= 0.0000005;
  return (
    <div
      class="koth-standing"
      id="koth-standing"
      classList={{ show: shown() }}
      role="note"
      aria-label="How the current KOTH emissions are allocated"
    >
      <Show when={shown() ? store.champion() : null}>
        {(champ) => (
          <>
            <span class="koth-standing-crown" aria-hidden="true">
              ♛
            </span>
            <div class="koth-standing-copy" id="koth-standing-copy">
              <Show
                when={store.emissions()?.allocation_mode === "score_ceiling_pool"}
                fallback={
                  <Show
                    when={contest()}
                    fallback={
                      <>
                        <b>
                          <EntityButton
                            kind="agent"
                            id={champ().agent_id}
                            label={agentName(champ().agent_name)}
                          />
                          {" is the reigning champion from raw #" + champ().rank + "."}
                        </b>{" "}
                        {aboveText() +
                          ", but the crown only moves when a challenger beats the first-seen incumbent " +
                          "by more than the dethrone band"}
                        <Show when={floor()}>
                          {(f) => (
                            <>
                              {" — "}
                              <b class="beat">{"current floor " + fx(f().floor)}</b>
                            </>
                          )}
                        </Show>
                        {
                          ". The exact decision is unavailable from this older leaderboard response."
                        }
                      </>
                    }
                  >
                    {(current) => {
                      const contest = () => crownContest(current().decision, current().emissions);
                      return (
                        <>
                          <div class="koth-standing-title">
                            <b>
                              Rank is not the crown.{" "}
                              <EntityButton
                                kind="agent"
                                id={current().leader.agent_id}
                                label={agentName(current().leader.agent_name)}
                              />
                              {" is #1 on score. "}
                              <EntityButton
                                kind="agent"
                                id={current().champion.agent_id}
                                label={agentName(current().champion.agent_name)}
                              />
                              {" is still champion."}
                            </b>
                          </div>
                          <Show when={contest()}>
                            {(held) => (
                              <>
                                <dl class="koth-standing-metrics">
                                  <div>
                                    <dt>
                                      {held().method === "paired"
                                        ? "Head-to-head lead"
                                        : "Current lead"}
                                    </dt>
                                    <dd>{signedScore(held().challengerLead)}</dd>
                                  </div>
                                  <div>
                                    <dt>Needed to take crown</dt>
                                    <dd>{signedScore(held().requiredLead)}</dd>
                                  </div>
                                  <div>
                                    <dt>Still short</dt>
                                    <dd>{signedScore(held().shortfall)}</dd>
                                  </div>
                                  <Show when={held().pairedStandardError}>
                                    {(se) => (
                                      <div>
                                        <dt>Paired SE</dt>
                                        <dd>{fxScore(se())}</dd>
                                      </div>
                                    )}
                                  </Show>
                                  <Show when={current().decision.required_score}>
                                    {(score) => (
                                      <div>
                                        <dt>Dethrone score</dt>
                                        <dd>{">" + fxScore(score())}</dd>
                                      </div>
                                    )}
                                  </Show>
                                </dl>
                                <div class="koth-standing-detail">{crownWhyHigh(held())}</div>
                                <Show when={held().seedDifferences}>
                                  {(diffs) => (
                                    <div class="koth-standing-diffs">
                                      {crownSeedDiffsText(diffs())}
                                    </div>
                                  )}
                                </Show>
                              </>
                            )}
                          </Show>
                          <div class="koth-standing-detail">
                            <Show when={rawMedianDiffers(current().leader)}>
                              {"The " +
                                fxScore(current().leader.composite) +
                                " own-seed median is the rank score, not the crown test. "}
                            </Show>
                            {crownComparisonNote(current().decision.method)}
                          </div>
                        </>
                      );
                    }}
                  </Show>
                }
              >
                <b>Score-ceiling joint crown.</b>{" "}
                {(store.emissions()?.score_ceiling_pool_size || 0) +
                  " highest evidence-tied agents split the full miner pool equally because the incumbent's required dethrone score cannot be exceeded within the score range. The historical incumbent no longer reserves a separate champion share."}
              </Show>
            </div>
          </>
        )}
      </Show>
    </div>
  );
}

// ── Emissions strip (render() 4934–5009 + renderDethroneFloor) ──

function sharedSeedNote(recipient: { shared_seed_confirmations?: number | null } | null): string {
  const n =
    recipient && recipient.shared_seed_confirmations ? recipient.shared_seed_confirmations : 0;
  return n > 0 ? n + " shared-seed confirmation" + (n === 1 ? "" : "s") : "";
}

function EmissionsStrip(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const emissions = store.emissions;
  const entriesByAgent = createMemo(() => {
    const out = new Map<string, BoardEntry>();
    store.entries().forEach((entry) => out.set(String(entry.agent_id), entry));
    return out;
  });
  const entriesByHotkey = createMemo(() => {
    const out = new Map<string, BoardEntry>();
    store.entries().forEach((entry) => out.set(entry.miner_hotkey, entry));
    return out;
  });
  const championEntry = createMemo<BoardEntry | null>(() => {
    const e = emissions();
    if (!e) return null;
    return entriesByAgent().get(String(e.champion_agent_id)) ?? null;
  });
  const rawLeaderEntry = createMemo<BoardEntry | null>(() => {
    const e = emissions();
    if (!e) return null;
    return entriesByAgent().get(String(e.raw_leader_agent_id)) ?? null;
  });
  const championRecipient = createMemo(
    () => (emissions()?.recipients || []).find((r) => r.role === "champion") ?? null,
  );
  const jointChampions = createMemo(() =>
    (emissions()?.recipients || []).filter((r) => r.role === "joint_champion"),
  );
  const tails = createMemo(() => (emissions()?.recipients || []).filter((r) => r.role === "tail"));
  const scoreCeilingPool = (): boolean => emissions()?.allocation_mode === "score_ceiling_pool";

  const championName = (): string => {
    const entry = championEntry();
    return (
      (entry ? agentName(entry.agent_name) : shortKey(emissions()?.champion_miner_hotkey)) ||
      "Unidentified champion"
    );
  };
  const championRank = (): string => {
    const entry = championEntry();
    return entry && entry.rank ? "raw #" + entry.rank : "current score pool";
  };
  const championShare = (): number | undefined => {
    const recipient = championRecipient();
    return recipient && Number.isFinite(recipient.share_of_miner_pool)
      ? recipient.share_of_miner_pool
      : emissions()?.champion_share;
  };

  // Every consensus constant in this strip comes from the fold the API
  // reports (margin, dethrone_z, champion_share, rank_shares, tail_size).
  // Hardcoding any of them lets the copy drift from the rule being applied.
  const reasonText = (): string => {
    const e = emissions();
    if (!e) return "";
    if (e.allocation_mode === "score_ceiling_pool") {
      const n = e.score_ceiling_pool_size || jointChampions().length;
      return (
        "The incumbent's required dethrone score is at or above the best challenger's attainable score ceiling. " +
        "A single-winner crown would therefore be impossible to win. The fold instead pays the highest evidence-tied cohort, anchored on raw #1, and includes every tied destination beyond the normal tail cutoff. " +
        n +
        " joint champions split the full miner pool equally; missing paired evidence cannot widen the cohort."
      );
    }
    const champion = championEntry();
    const rawLeader = rawLeaderEntry();
    const marginLabel = marginText(e.margin);
    const bandScale = dethroneBandScale(
      // The band-decay fields ride on the fold but predate the shared wire
      // type; the read stays structural exactly like the original's.
      e as BandDecayParams,
      champion,
      champion ? displayComposite(champion, store.settledView()) : NaN,
      rawLeader,
    );
    const effectiveBandNote =
      bandScale < 1
        ? " Bench v" +
          Number((champion && champion.bench_version) || 0) +
          " applies the high-score curve, scaling the whole band to " +
          pct(bandScale) +
          " of its base width."
        : "";
    const tiePoolingNote = e.tie_weighting_active
      ? " Recipients whose exact effective scores tie, or whose paired shared-seed evidence remains statistically indistinguishable, pool only the ranked shares of the slots they occupy. Missing paired evidence cannot widen a group."
      : "";
    const decision = e.raw_leader_decision;
    if (decision && rawLeader) {
      const method =
        decision.method === "paired"
          ? "paired-seed uncertainty band"
          : decision.method === "unpaired"
            ? "statistical uncertainty band"
            : marginLabel + " fixed hysteresis";
      const contestNote =
        decision.method === "paired"
          ? " Both sides were scored on shared confirmation seeds, so this decision is a paired comparison: per-dataset difficulty cancels and cannot decide the crown."
          : " A lead inside the band is never left to dataset luck: validators re-score both agents on shared confirmation seeds and the next fold decides the contest on the paired comparison.";
      return (
        "Raw #1 " +
        agentName(rawLeader.agent_name) +
        " leads by " +
        fx(decision.challenger_lead) +
        " but must lead by more than " +
        fx(decision.required_lead) +
        " to dethrone the older incumbent. " +
        "The fold starts with the larger of the " +
        marginLabel +
        " protection margin and the " +
        method +
        ", then applies the versioned band curve." +
        effectiveBandNote +
        contestNote +
        tiePoolingNote
      );
    }
    return (
      "The raw score leader also wins the first-seen KOTH fold after the " +
      marginLabel +
      " protection margin, statistical band, and versioned high-score curve are applied." +
      tiePoolingNote
    );
  };

  // "Beat this to contend." Published as a floor, explicitly, and never as a
  // sufficient number: only the margin term is knowable before a challenger
  // is scored (lib/scoring.dethroneFloor — never inline math).
  const floor = createMemo(() =>
    scoreCeilingPool() ? null : dethroneFloor(emissions(), championEntry(), store.settledView()),
  );
  const floorText = (): string => {
    const f = floor();
    if (!f) return "";
    const band =
      f.z != null
        ? " The band is " +
          (f.scale < 1 ? num(f.scale) + "× the larger" : "the larger") +
          " of that base margin and " +
          num(f.z) +
          " × √(challenger SE² + champion SE²), so a challenger with noisier scores may have to clear more than " +
          fx(f.floor) +
          ", this is a floor, not a guarantee."
        : " This is a floor: the fold takes the larger of the margin and the statistical uncertainty band.";
    return (
      "The champion holds " +
      fx(f.champComposite) +
      ", and a challenger must exceed it by more than the " +
      marginText(f.effectiveMargin) +
      (f.scale < 1 ? " effective v6+ hysteresis." : " fixed hysteresis.") +
      band +
      " A lead inside the band is not rejected: both agents are re-scored on shared confirmation seeds and the next fold decides on the paired comparison."
    );
  };

  // A leaderboard failure hides the whole strip even when a previous chain
  // matrix is still held (renderLeaderboardUnavailable 5535–5554 removes
  // .show outright): stated absence, never stale-as-fresh context.
  const chainShown = createMemo(() => {
    if (store.unavailable()) return false;
    const fold = store.chainFold();
    return Boolean(fold && fold.minerVectors);
  });
  const chainCopy = (): string => {
    const fold = store.chainFold();
    const snapshot = store.chainWeights();
    if (!fold || !fold.minerVectors || !snapshot) return "";
    const revealedLeader = fold.leaders[0];
    const revealedEntry = revealedLeader ? entriesByHotkey().get(revealedLeader) : undefined;
    const revealedLabel = revealedEntry
      ? agentName(revealedEntry.agent_name)
      : shortKey(revealedLeader);
    const projectedHotkey = scoreCeilingPool() ? null : emissions()?.champion_miner_hotkey;
    const projectedEntry = projectedHotkey ? entriesByHotkey().get(projectedHotkey) : undefined;
    const projectedLabel = projectedEntry
      ? agentName(projectedEntry.agent_name)
      : projectedHotkey
        ? shortKey(projectedHotkey)
        : "The projected champion";
    let summary = projectedHotkey
      ? projectedLabel +
        " is the validator top choice in " +
        (fold.championCounts[projectedHotkey] || 0) +
        " of " +
        fold.minerVectors +
        " revealed miner-bearing vectors. "
      : "The chain exposes " + fold.minerVectors + " revealed miner-bearing validator vectors. ";
    if (revealedLeader && revealedLeader !== projectedHotkey) {
      summary +=
        revealedLabel +
        " is the most common validator top choice at " +
        fold.championCounts[revealedLeader] +
        " of " +
        fold.minerVectors +
        ". ";
    }
    summary +=
      "Block " +
      Number(snapshot.block).toLocaleString() +
      ". Commit-reveal can make this lag active commitments; Yuma combines validator inputs stake-weightedly.";
    // The API serves the last good matrix rather than nothing when a chain
    // re-read fails, so say so instead of silently presenting old data as
    // current. The panel stays up either way: dropping it for a tick and
    // restoring it on the next one is the "flicker" this replaced.
    if (snapshot.stale) {
      summary +=
        " Chain re-read is currently failing; this is the last matrix successfully read" +
        (snapshot.age_seconds ? ", " + relDuration(snapshot.age_seconds) + " ago" : "") +
        ".";
    }
    return summary;
  };

  return (
    <div
      class="emissions-strip"
      id="emissions-strip"
      classList={{ show: (Boolean(emissions()) || chainShown()) && !store.unavailable() }}
      role="status"
      aria-live="polite"
      aria-label="KOTH emissions"
    >
      <div class="emissions-summary">
        <div class="emissions-title" id="emissions-title">
          <Show when={emissions()}>
            <Show
              when={scoreCeilingPool()}
              fallback={
                <>
                  <span class="emission-badge champion">
                    <span aria-hidden="true">●</span> KOTH champion
                  </span>
                  {" · "}
                  <Show when={championEntry()} fallback={championName()}>
                    {(entry) => (
                      <EntityButton kind="agent" id={entry().agent_id} label={championName()} />
                    )}
                  </Show>
                  {" (" + championRank() + ") receives "}
                  {pct(championShare() as number)}
                  {" of the miner pool."}
                  <Show when={sharedSeedNote(championRecipient())}>
                    {(note) => (
                      <>
                        {" "}
                        <Tip text="Distinct champion-anchored seeds scored by the continual top-five lane.">
                          {"· " + note()}
                        </Tip>
                      </>
                    )}
                  </Show>
                </>
              }
            >
              <span class="emission-badge joint_champion">
                <span aria-hidden="true">♛</span> Score-ceiling joint crown
              </span>
              {" · " + jointChampions().length + " evidence-tied agents each receive "}
              {pct(jointChampions()[0]?.share_of_miner_pool as number)}
              {" of the miner pool."}
            </Show>
          </Show>
        </div>
        {/* The dethrone-math explainer collapses behind a disclosure so the
            table stays near the top; it only exists when there is a fold to
            explain. */}
        <details class="emissions-why" id="emissions-why" hidden={!emissions()}>
          <summary>How the crown is decided</summary>
          <div class="emissions-reason" id="emissions-reason">
            {emissions() ? reasonText() : ""}
          </div>
          <div
            class="emissions-threshold"
            id="emissions-threshold"
            classList={{ show: Boolean(floor()) }}
          >
            <Show when={floor()}>
              {(f) => (
                <>
                  <b>Beat this to contend:</b> <span class="beat">{fx(f().floor)}</span> composite.{" "}
                  {floorText()}
                </>
              )}
            </Show>
          </div>
        </details>
      </div>
      <div class="emissions-recipients" id="emissions-recipients">
        <Show when={emissions()}>
          <Show
            when={scoreCeilingPool() ? jointChampions().length : tails().length}
            fallback={<>No participation-tail recipients in the current pool.</>}
          >
            <span class={"emission-badge " + (scoreCeilingPool() ? "joint_champion" : "tail")}>
              {scoreCeilingPool() ? "Joint crown" : "Participation tail"}
            </span>
            {" · "}
            <For each={scoreCeilingPool() ? jointChampions() : tails()}>
              {(recipient, index) => {
                const entry = (): BoardEntry | undefined =>
                  entriesByAgent().get(String(recipient.agent_id));
                const label = (): string =>
                  entry() ? agentName(entry()?.agent_name) : shortKey(recipient.miner_hotkey);
                const note = (): string => sharedSeedNote(recipient);
                return (
                  <>
                    {index() > 0 ? " · " : ""}
                    <Show when={entry()} fallback={label()}>
                      {(e) => <EntityButton kind="agent" id={e().agent_id} label={label()} />}
                    </Show>{" "}
                    {pct(recipient.share_of_miner_pool as number)}
                    <Show when={note()}>
                      {(n) => (
                        <>
                          {" "}
                          <span class="text-faint">({n()})</span>
                        </>
                      )}
                    </Show>
                  </>
                );
              }}
            </For>
          </Show>
        </Show>
      </div>
      <div class="chain-observation" id="chain-observation" classList={{ show: chainShown() }}>
        <span class="chain-badge">On chain</span>
        <span id="chain-observation-copy">{chainCopy()}</span>
      </div>
    </div>
  );
}

// ── Rollout strip (renderRollout 3707–3794) ──────────────────
// Weights follow ONE version at a time. The whole ledger flips only once
// ranked_quorum_agents reaches min_ranked_quorum_agents at the desired
// version. Both numbers come from /public/bench/rollout so the threshold is
// never hardcoded here.

// ── Efficiency strip ──────────────────────────────────────────
//
// The per-row efficiency chip can only render on a row that HAS a factor, so
// while the cohort is empty the entire adjustment is invisible: an operator
// cannot tell a switched-off adjustment from an active one that qualified
// nobody, and a miner reading a blank column concludes the feature is broken.
// This strip states the board-level answer once, next to the numbers, and
// names the parameters the factor is computed from so the arithmetic can be
// checked against the published policy rather than taken on trust.

function clampText(s: NonNullable<ReturnType<typeof efficiencyBoardStatus>>): string {
  return s.minimumFactor == null || s.maximumFactor == null
    ? ""
    : "bounded ×" + fx(s.minimumFactor) + " to ×" + fx(s.maximumFactor);
}

function EfficiencyStrip(props: { store: LeaderboardStore }): JSX.Element {
  const status = createMemo(() => efficiencyBoardStatus(props.store.efficiency()));
  const shown = createMemo(() => !props.store.unavailable() && status() != null);
  return (
    <div
      class="efficiency-strip"
      id="efficiency-strip"
      classList={{ show: shown() }}
      role="note"
      aria-label="Relative token-efficiency state"
    >
      <Show when={shown() ? status() : null}>
        {(s) => (
          <div class="efficiency-summary" data-tone={s().tone}>
            <div class="efficiency-title">
              <span class="efficiency-state" data-tone={s().tone}>
                {s().tone === "applied"
                  ? "applied"
                  : s().tone === "projected"
                    ? "projection"
                    : "dormant"}
              </span>
              <b>{s().headline}</b>
            </div>
            <div class="efficiency-detail">{s().detail}</div>
            <div class="efficiency-params">
              <span class="efficiency-param">
                {"qualified cohort " + s().cohortSize + " / " + s().nMin + " required"}
              </span>
              {/* `when` must carry the number itself: Show hands the callback
                  the value it tested, so a boolean guard would render `1`. */}
              <Show when={s().referenceTokens}>
                {(tokens) => (
                  <span class="efficiency-param">
                    {"neutral reference " + num(Math.round(tokens())) + " tokens"}
                  </span>
                )}
              </Show>
              <Show when={clampText(s())}>
                {(text) => <span class="efficiency-param">{text()}</span>}
              </Show>
            </div>
          </div>
        )}
      </Show>
    </div>
  );
}

function RolloutStrip(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const strip = createMemo(() => rolloutStripState(store.rollout()));
  const quorum = createMemo(() => rolloutQuorum(store.rollout()));

  const headHint = (): string => {
    const s = strip();
    if (!s) return "";
    return s.rolling
      ? "v" + s.active + " drives validator weights · v" + s.desired + " is rolling out"
      : s.collecting
        ? "v" + s.active + " drives validator weights · inherited cohort scoring continues"
        : "v" + s.active + " drives validator weights";
  };

  type Progress = { count: [number, number] | null; text: string; note: string };
  const progress = createMemo<Progress | null>(() => {
    const s = strip();
    if (!s) return null;
    const state = store.rollout();
    const q = quorum();
    if (s.collecting && !state?.priority_complete) {
      return {
        count: [q.priorityReady, q.prioritySize],
        text: " inherited leaders have complete v" + s.desired + " quorums.",
        note:
          "The first five from the prior benchmark are a fleet-wide barrier. Validators that have already scored every " +
          "eligible leader intentionally idle until the other validators finish them; rank 6 and later cannot skip ahead.",
      };
    }
    if (s.collecting && state?.priority_complete) {
      return {
        count: [q.cohortReadyCount, q.cohortSize],
        text: " inherited top-cohort agents have complete v" + s.desired + " quorums.",
        note:
          "The first-five barrier is complete. This rollout's bounded inherited cohort has " +
          q.cohortSize +
          " miners, filled from the two previous benchmark iterations when needed; it remains ahead of ordinary work.",
      };
    }
    if (s.rolling && Number.isFinite(q.ready) && Number.isFinite(q.needed)) {
      return {
        count: [q.ready as number, q.needed as number],
        text:
          " agents ready at v" +
          s.desired +
          ", finalized on the full benchmark with a complete ranked quorum.",
        note:
          "Weights stay on v" +
          s.active +
          " until that threshold is met, then the whole ledger flips to v" +
          s.desired +
          " at once. The gate exists so the emission set is never short: the champion and the full participation tail " +
          "must all have comparable v" +
          s.desired +
          " scores before any of them are ranked on it.",
      };
    }
    if (s.status === "superseded") {
      return {
        count: null,
        text:
          "The v" +
          s.desired +
          " rollout was superseded before activation; a rollout to a later version opens in its place.",
        note:
          "Weights remain on v" +
          s.active +
          " throughout. A superseded rollout never moves emissions.",
      };
    }
    if (s.status === "activated") {
      return {
        count: null,
        text: "v" + s.desired + " is activated and drives validator weights.",
        note:
          "The whole ledger ranks on v" +
          s.desired +
          ". Scores from earlier versions are kept but are not comparable to it.",
      };
    }
    return {
      count: null,
      text: "No rollout is open. v" + s.active + " drives validator weights.",
      note: "",
    };
  });

  return (
    <div
      class="rollout-strip"
      id="rollout-strip"
      classList={{ show: Boolean(strip()) }}
      role="status"
      aria-live="polite"
      aria-label="Benchmark rollout"
    >
      <div class="rollout-head" id="rollout-head">
        <Show when={strip()}>
          {(s) => (
            <>
              <span>Benchmark rollout</span>
              <span class={"rollout-status " + s().status}>{s().status}</span>
              <span class="hint">{headHint()}</span>
            </>
          )}
        </Show>
      </div>
      <div class="rollout-progress" id="rollout-progress">
        <Show when={progress()}>
          {(p) => (
            <>
              <Show when={p().count}>
                {(count) => (
                  <>
                    <span class="count">{count()[0] + " of " + count()[1]}</span>
                  </>
                )}
              </Show>
              {p().text}
            </>
          )}
        </Show>
      </div>
      <div class="rollout-note" id="rollout-note">
        {progress()?.note ?? ""}
      </div>
    </div>
  );
}

// ── Standing notices (render() 4888–4931) ────────────────────

/** Fields the notice logic reads that predate the shared payload type. */
type NoticePayload = {
  registration_stale?: boolean;
  continual_aggregate_active?: boolean;
  continual_aggregate_required_protocol?: number | string;
};

function LeaderboardNotice(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const noticesHtml = createMemo<string>(() => {
    const d = store.payload() as (ReturnType<LeaderboardStore["payload"]> & NoticePayload) | null;
    if (!d || store.unavailable()) return "";
    const entries = store.entries();
    const provisional = entries.filter((e) => !isFinalized(e) && isEligible(e));
    const inactive = entries.filter((e) => e.registered === false);
    const registrationUnknown = entries.filter((e) => e.registered == null);
    const collectedContinualWaves = entries.some((e) => Number(e.completed_wave_count) > 0);
    const historicalView = d.selection_mode === "historical";
    const continualPending =
      !historicalView && collectedContinualWaves && d.continual_aggregate_active !== true;
    // Registration values that are real but were read a moment ago rather
    // than just now. Distinct from `registrationUnknown` (no usable read at
    // all): the rows keep their badges and only gain this one-line caveat.
    const registrationStale = d.registration_stale === true;
    const notices: string[] = [];
    if (!historicalView && d.v9_confirmation_mode === "shadow")
      notices.push(
        "<strong>LongMemEval shadow is active.</strong> Independent LongMemEval and ablation evidence is being collected and shown per agent. Shadow results do not change rankings or emissions. Qualified completed evidence is retained if Enforce later begins with the exact same frozen profile.",
      );
    else if (!historicalView && d.v9_confirmation_mode === "enforce")
      notices.push(
        "<strong>LongMemEval enforcement is active.</strong> Only agents with qualified full confirmation evidence can rank or receive emissions.",
      );
    if (provisional.length)
      notices.push(
        "<strong>Provisional standings.</strong> " +
          provisional.length +
          " miner" +
          (provisional.length === 1 ? " has" : "s have") +
          " accepted validator feedback but not the required 3 of 3 scores. Rankings and composites can change until quorum; only final results drive emissions, and current SN118 registration is also required.",
      );
    if (inactive.length)
      notices.push(
        "<strong>Registration inactive.</strong> " +
          inactive.length +
          " scored hotkey" +
          (inactive.length === 1 ? " is" : "s are") +
          " not currently registered on SN118. Their submissions and scores are retained, but they cannot hold the KOTH crown or receive active weights and emissions until the same hotkey registers again.",
      );
    if (registrationUnknown.length)
      notices.push(
        "<strong>Registration unavailable.</strong> Current SN118 registration could not be confirmed. The deterministic KOTH fold remains visible, but actual chain eligibility and active-rank styling stay unknown until registration is confirmed.",
      );
    else if (registrationStale)
      notices.push(
        "<strong>Registration not confirmed just now.</strong> The latest SN118 registration read failed, so the registration column below is the last one successfully read rather than a live confirmation. The values are real; they are simply not re-confirmed as of this refresh.",
      );
    if (continualPending)
      notices.push(
        "<strong>Continual score activation pending.</strong> Completed retest waves are preserved as observations, but rankings and weights remain on the canonical three-score median until every recently-live validator supports protocol " +
          esc(d.continual_aggregate_required_protocol || 14) +
          ". The contract activates globally, never per validator.",
      );
    return notices.join(" ");
  });
  return (
    <div
      class="leaderboard-notice"
      id="leaderboard-notice"
      classList={{ show: Boolean(noticesHtml()) }}
      role="status"
      aria-live="polite"
      aria-label="Standing notices"
      innerHTML={noticesHtml()}
    />
  );
}

// ── The block ────────────────────────────────────────────────

export function LeaderboardBlock(props: { mode: "overview" | "page" }): JSX.Element {
  const store = leaderboardStore();

  onMount(() => {
    // Entering the dedicated page resets the view to the current rollout
    // (route() 9793–9797): an archive can never render there — archive
    // browsing lives on the overview.
    if (props.mode === "page") setLeaderboardVersionView("current");
    // The pager is hash-owned state on both mounts.
    restoreBoardPage();
    store.ensureFresh();
  });

  createEffect(() => {
    // A mode change without a remount (route flip) applies the same reset.
    if (props.mode === "page") setLeaderboardVersionView("current");
  });

  const title = (): string => {
    if (store.unavailable()) return "Leaderboard";
    const d = store.payload();
    if (!d) return "Leaderboard";
    if (d.selection_mode === "historical") return "Bench v" + d.current_bench_version + " history";
    const entries = store.entries();
    const finalizedEntries = entries.filter((e) => isFinalized(e));
    const provisional = entries.filter((e) => !isFinalized(e) && isEligible(e));
    return finalizedEntries.length
      ? "Current rollout leaderboard"
      : provisional.length
        ? "Provisional leaderboard"
        : "Current rollout leaderboard";
  };

  const hint = (): string => {
    if (store.unavailable()) return "Live standings are temporarily unavailable";
    const d = store.payload();
    if (!d)
      return "one rank per payment-owner family · open an agent for full submission details · KOTH emissions shown separately";
    if (d.selection_mode === "historical")
      return "historical scores only · does not drive current validator weights";
    if (store.settledView())
      return (
        "ranked by settled v" +
        store.bench().active +
        " composite during the v" +
        d.desired_bench_version +
        " rollout · v" +
        d.desired_bench_version +
        " progress shown per row"
      );
    const finalizedEntries = store.entries().filter((e) => isFinalized(e));
    return finalizedEntries.length
      ? "authoritative per-agent score · open a row for tool & memory detail"
      : "early accepted scores · ranked provisionally by composite · open a row for detail";
  };

  return (
    <div id="leaderboard-block">
      <div class="section-head">
        <h2 id="leaderboard-title">{title()}</h2>
        <span class="hint" id="leaderboard-hint">
          {hint()}
        </span>
      </div>
      <VersionSwitch store={store} />
      <KothStandingCallout store={store} />
      <BoardTable store={store} />
      {/* Post-table context: emissions, rollout, and standing notices sit
          below the board so the table starts at the top of the Leaderboard
          page. Hidden entirely on the overview (see .overview-main rules). */}
      <h2 class="visually-hidden lb-context-heading">Emissions, rollout, and standing notices</h2>
      <EmissionsStrip store={store} />
      <EfficiencyStrip store={store} />
      <RolloutStrip store={store} />
      <LeaderboardNotice store={store} />
    </div>
  );
}
