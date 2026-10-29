// Generator competition (/generators). Miners submit case generators; each
// round runs every counted generator against a panel of top agents and pays
// generators whose cases separate those agents. The page reads only public
// endpoints: the generator board, rounds, round detail and upload terms, one
// generator's public detail (the ?generator= panel), the shared chain epoch,
// and the treasury allocation for the emission split.
//
// Every section loads, fails and retries on its own: a slow or failed read
// shows a skeleton or an error with a retry in its own place, and a failed
// refresh keeps the last good data on screen instead of blanking it.
import { For, Show, createMemo, createResource, createSignal, onCleanup } from "solid-js";
import type { Accessor, JSX } from "solid-js";

import type { ResourceState } from "../data/useEndpoint";
import { useEndpoint } from "../data/useEndpoint";
import { weightsResource } from "../data/weights";
import { getJSON } from "../lib/api";
import { REFRESH_MS } from "../lib/config";
import { fx, monoDisplay, relTime, relTimeUntil } from "../lib/format";
import { spaQuery } from "../lib/router";
import { EntityButton } from "../components/ui/EntityButton";
import { MinerAvatar } from "../components/ui/MinerAvatar";
import { Sparkline } from "../components/ui/Sparkline";
import { StatusChip } from "../components/ui/StatusChip";
import type {
  EmissionAllocation,
  EmissionShare,
  GeneratorBoard,
  GeneratorMode,
  GeneratorRoundDetail,
  GeneratorRoundList,
  GeneratorRoundStatus,
  GeneratorRoundSummary,
  GeneratorUploadTerms,
  ProjectedPayout,
} from "../components/generators/generator-data";
import {
  GENERATOR_RANK_SHARES,
  columnCases,
  cooldownText,
  generatorEmissionShare,
  isSubmissionId,
  newestScoredIndex,
  nextRoundEta,
  nextRoundIndex,
  payoutFor,
  projectGeneratorPayouts,
  taoAmount,
} from "../components/generators/generator-data";
import { GeneratorIdentityCell, showGenerator } from "../components/generators/GeneratorIdentity";
import { GeneratorPanel } from "../components/generators/GeneratorPanel";
import {
  CaseBar,
  CaseLegend,
  GradingBadge,
  SectionError,
  SkeletonRows,
  hasGradingVersions,
} from "../components/generators/GeneratorParts";

const GUIDE_URL =
  "https://github.com/ditto-assistant/ditto-subnet/blob/main/research/dittobench-datagen/docs/generator-competition.md";

const share = (value: number) =>
  new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 }).format(value);

const MODE_CHIP: Record<GeneratorMode, readonly [string, string]> = {
  enforce: ["Paying", "good"],
  shadow: ["Shadow · unpaid", "paused"],
  off: ["Off", "unknown"],
};

const ROUND_STATUS: Record<GeneratorRoundStatus, readonly [string, string]> = {
  committed: ["Committed", ""],
  generating: ["Generating", "progress"],
  running: ["Running", "progress"],
  scoring: ["Scoring", "progress"],
  scored: ["Scored", "good"],
  void: ["Void", "warn"],
};

const UNPAID_LABEL: Record<NonNullable<ProjectedPayout["unpaidReason"]>, string> = {
  ineligible: "Not weight-eligible",
  zero_score: "Score is zero",
  duplicate_hotkey: "Hotkey already paid",
  beyond_paid_ranks: "Below the paid ranks",
};

/** The newest successful value, kept through a failed refresh. */
function lastGood<T>(
  data: Accessor<T | undefined>,
  error: Accessor<unknown>,
): Accessor<T | undefined> {
  let last: T | undefined;
  return createMemo(() => {
    if (!error()) {
      const value = data();
      if (value !== undefined) last = value;
    }
    return last;
  });
}

function fromEndpoint<T>(resource: ResourceState<T>): Accessor<T | undefined> {
  return lastGood(resource.data, resource.error);
}

function chainEpoch() {
  const weights = weightsResource();
  return () => {
    if (weights.error()) return null;
    try {
      return weights.data()?.epoch ?? null;
    } catch {
      return null;
    }
  };
}

/** The emission split's segment colour, keyed by recipient. */
function splitTone(row: EmissionShare): string {
  if (row.kind === "burn") return "burn";
  if (row.kind === "gamma") return "gamma";
  if (row.id === "memory") return "agent";
  if (row.id === "generator") return "generator";
  return "other";
}

function splitLabel(row: EmissionShare): string {
  if (row.kind === "burn") return "Burn";
  if (row.kind === "gamma") return "Gamma · " + (row.id ?? "service");
  if (row.id === "memory") return "Agent miners";
  if (row.id === "generator") return "Generator miners";
  return "Track · " + (row.id ?? "unknown");
}

export function EmissionSplit(props: { allocation: EmissionAllocation | undefined }): JSX.Element {
  const rows = () => (props.allocation?.emission_split ?? []).filter((row) => row.share > 0);
  return (
    <section class="gen-section gen-split" aria-labelledby="gen-split-title">
      <div class="gen-section-title">
        <h3 id="gen-split-title">Emission split</h3>
        <Show when={props.allocation?.emission_split_epoch != null}>
          <span>Epoch pin {props.allocation?.emission_split_epoch}</span>
        </Show>
      </div>
      <Show
        when={rows().length}
        fallback={
          <>
            <Show when={props.allocation}>
              {(allocation) => (
                <dl class="gen-stats">
                  <div>
                    <dt>Burn, forecast</dt>
                    <dd>{share(allocation().forecast_burn_share)}</dd>
                  </div>
                  <div>
                    <dt>Miners, forecast</dt>
                    <dd>{share(allocation().forecast_miner_share)}</dd>
                  </div>
                </dl>
              )}
            </Show>
            <p class="gen-note">
              The split between agent and generator miners is not yet published. Platform will serve
              it with the emission allocation; until then only burn versus miners is shown.
            </p>
          </>
        }
      >
        <div class="gen-split-bar" role="img" aria-label="Share of miner emission per recipient">
          <For each={rows()}>
            {(row) => (
              <span
                class={"gen-split-seg " + splitTone(row)}
                style={{ "flex-grow": String(row.share) }}
                title={splitLabel(row) + ": " + share(row.share)}
              />
            )}
          </For>
        </div>
        <ul class="gen-split-legend">
          <For each={rows()}>
            {(row) => (
              <li>
                <span class={"gen-swatch " + splitTone(row)} aria-hidden="true" />
                <span>{splitLabel(row)}</span>
                <strong>{share(row.share)}</strong>
              </li>
            )}
          </For>
        </ul>
        <p class="gen-note">
          Shares of total miner emission validators fold now. When no generator is eligible, the
          generator share pays agent miners instead of burning.
          <Show when={props.allocation?.emission_split_source === "settings"}>
            {" "}
            Read from live settings; no epoch pin has frozen it yet.
          </Show>
        </p>
      </Show>
    </section>
  );
}

function RoundStatus(props: { round: GeneratorRoundSummary }): JSX.Element {
  const chip = () => ROUND_STATUS[props.round.status] ?? [props.round.status, ""];
  return <StatusChip label={chip()[0]} tone={chip()[1]} />;
}

function PayoutTable(props: {
  board: GeneratorBoard | undefined;
  rows: ProjectedPayout[];
  generatorShare: number | null;
  loading: boolean;
  failed: boolean;
  onRetry: () => void;
}): JSX.Element {
  const anyPaid = () => props.rows.some((row) => row.position !== null);
  return (
    <section class="gen-section" aria-labelledby="gen-board-title">
      <div class="gen-section-title">
        <h3 id="gen-board-title">Generator board</h3>
        <Show when={props.board}>
          {(board) => (
            <span>
              Latest scored round per hotkey · {board().window_days}-day window · updated{" "}
              {relTime(board().generated_at)}
            </span>
          )}
        </Show>
      </div>
      <Show when={props.failed}>
        <SectionError what="Generator board" stale={!!props.board} onRetry={props.onRetry} />
      </Show>
      <Show when={props.board && props.board.mode !== "enforce"}>
        <p class="gen-note">
          The competition is in {props.board?.mode} mode, so validators pay generators nothing yet.
          The positions below show what they would earn once it pays.
        </p>
      </Show>
      <Show when={props.board && !anyPaid()}>
        <p class="gen-note">
          No generator is eligible right now, so the generator share pays agent miners.
        </p>
      </Show>
      <Show when={props.board || props.loading}>
        <div class="gen-table-scroll">
          <table class="gen-table gen-board" aria-label="Projected generator payouts">
            <thead>
              <tr>
                <th scope="col">Position</th>
                <th scope="col">Generator</th>
                <th scope="col" class="num">
                  Score
                </th>
                <th scope="col" class="hide-sm">
                  <span title="Column score in each recent scored round, oldest to newest">
                    Recent rounds
                  </span>
                </th>
                <th scope="col" class="num hide-md">
                  Discriminating
                </th>
                <th scope="col" class="num">
                  Generator track
                </th>
                <th scope="col" class="num hide-sm">
                  Miner emission
                </th>
                <th scope="col" class="hide-md">
                  First seen
                </th>
              </tr>
            </thead>
            <tbody>
              <Show
                when={props.board}
                fallback={<SkeletonRows rows={4} cols={8} label="Loading the generator board" />}
              >
                <Show
                  when={props.rows.length}
                  fallback={
                    <tr>
                      <td colspan={8} class="empty">
                        <div class="empty-msg">
                          No generator has a scored round in the window. Qualified generators enter
                          rounds best-first; see Submitting a generator below.
                        </div>
                      </td>
                    </tr>
                  }
                >
                  <For each={props.rows}>
                    {(row) => (
                      <tr
                        classList={{
                          unpaid: row.position === null,
                          ["pos-" + row.position]: row.position !== null && row.position <= 3,
                        }}
                      >
                        <td class="gen-pos">
                          {row.position !== null ? (
                            "#" + row.position
                          ) : (
                            <small>{UNPAID_LABEL[row.unpaidReason ?? "zero_score"]}</small>
                          )}
                        </td>
                        <td>
                          <GeneratorIdentityCell
                            item={row.entry}
                            minerHotkey={row.entry.miner_hotkey}
                          />
                        </td>
                        <td class="num">{fx(row.entry.score)}</td>
                        <td class="hide-sm">
                          <Show
                            when={(row.entry.recent?.length ?? 0) > 1}
                            fallback={
                              <small>
                                {row.entry.rounds_in_window != null
                                  ? row.entry.rounds_in_window +
                                    " round" +
                                    (row.entry.rounds_in_window === 1 ? "" : "s")
                                  : "–"}
                              </small>
                            }
                          >
                            <span class="gen-form">
                              <Sparkline
                                history={row.entry.recent?.map((point) => point.score)}
                                width={72}
                                height={18}
                                measure="Round score"
                                unit="scored rounds"
                              />
                              <Show when={row.entry.rounds_in_window != null}>
                                <small
                                  title={
                                    row.entry.rounds_in_window + " scored rounds in the window"
                                  }
                                >
                                  {row.entry.rounds_in_window}
                                </small>
                              </Show>
                            </span>
                          </Show>
                        </td>
                        <td class="num hide-md">{row.entry.discriminating_cases}</td>
                        <td class="num">{row.position !== null ? share(row.trackShare) : "–"}</td>
                        <td class="num hide-sm">
                          {row.position === null
                            ? "–"
                            : props.generatorShare === null
                              ? "Split not published"
                              : share(row.trackShare * props.generatorShare)}
                        </td>
                        <td class="hide-md">{relTime(row.entry.first_seen)}</td>
                      </tr>
                    )}
                  </For>
                </Show>
              </Show>
            </tbody>
          </table>
        </div>
      </Show>
      <p class="gen-note">
        Validators rank eligible generators with a score above zero by score, then first seen, keep
        one per hotkey, and pay positions{" "}
        {GENERATOR_RANK_SHARES.map((value) => share(value)).join(" / ")}. With fewer than{" "}
        {GENERATOR_RANK_SHARES.length} paid generators those shares are rescaled to fill the
        generator track. Open a generator for its round-by-round results.
      </p>
    </section>
  );
}

function RoundDetail(props: {
  index: number | null;
  detail: GeneratorRoundDetail | undefined;
  failed: boolean;
  onRetry: () => void;
}): JSX.Element {
  const cases = () => {
    const counts = (props.detail?.columns ?? []).map(columnCases);
    return counts.length && counts.every((count) => count === counts[0]) ? counts[0] : null;
  };
  const columns = () => [...(props.detail?.columns ?? [])].sort((a, b) => b.score - a.score);
  const grading = () => hasGradingVersions(props.detail?.columns ?? []);
  return (
    <section class="gen-section" aria-labelledby="gen-round-title">
      <div class="gen-section-title">
        <h3 id="gen-round-title">
          {props.index != null ? "Round " + props.index + " results" : "Latest round results"}
        </h3>
        <Show when={props.detail}>
          {(detail) => (
            <span>
              Scored {relTime(detail().summary.scored_at)} · {detail().summary.panel_size} panel
              agents
              <Show when={cases() !== null}> · {cases()} cases per generator</Show>
            </span>
          )}
        </Show>
      </div>
      <Show when={props.failed}>
        <SectionError
          what={"Round " + (props.index ?? "") + " results"}
          stale={!!props.detail}
          onRetry={props.onRetry}
        />
      </Show>
      <Show when={props.detail || !props.failed}>
        <div class="gen-table-scroll">
          <table class="gen-table">
            <caption>Generators in this round</caption>
            <thead>
              <tr>
                <th scope="col">Generator</th>
                <th scope="col" class="num">
                  Score
                </th>
                <th scope="col">Cases</th>
                <th scope="col" class="num">
                  Discriminating
                </th>
                <th scope="col" class="num hide-sm">
                  Trivial
                </th>
                <th scope="col" class="num hide-sm">
                  Unsolved
                </th>
                <th scope="col" class="num hide-sm">
                  Duplicate
                </th>
                <Show when={grading()}>
                  <th
                    scope="col"
                    class="hide-sm"
                    title="The grading policy each generator's pack declared"
                  >
                    Grading
                  </th>
                </Show>
              </tr>
            </thead>
            <tbody>
              <Show
                when={props.detail}
                fallback={<SkeletonRows rows={5} cols={7} label="Loading round results" />}
              >
                <For each={columns()}>
                  {(column) => (
                    <tr>
                      <td>
                        <GeneratorIdentityCell
                          item={column}
                          minerHotkey={column.miner_hotkey}
                          publicDefault={column.public_default || !column.miner_hotkey}
                        />
                      </td>
                      <td class="num">{fx(column.score)}</td>
                      <td>
                        <CaseBar counts={column} />
                      </td>
                      <td class="num">{column.discriminating}</td>
                      <td class="num hide-sm">{column.trivial}</td>
                      <td class="num hide-sm">{column.unsolved}</td>
                      <td class="num hide-sm">{column.duplicate}</td>
                      <Show when={grading()}>
                        <td class="hide-sm">
                          <GradingBadge version={column.grading_bench_version} />
                        </td>
                      </Show>
                    </tr>
                  )}
                </For>
              </Show>
            </tbody>
          </table>
        </div>
      </Show>
      <CaseLegend />
      <Show when={props.detail?.standings.length}>
        <div class="gen-table-scroll">
          <table class="gen-table">
            <caption>Panel agents on this round&apos;s cases</caption>
            <thead>
              <tr>
                <th scope="col">Rank</th>
                <th scope="col">Agent</th>
                <th scope="col" class="num">
                  Pairwise score
                </th>
                <th scope="col" class="num hide-sm">
                  Wins / ties / losses
                </th>
                <th scope="col" class="num">
                  Mean on shared cases
                </th>
              </tr>
            </thead>
            <tbody>
              <For each={props.detail?.standings ?? []}>
                {(standing) => (
                  <tr>
                    <td>#{standing.rank}</td>
                    <td>
                      <span class="gen-identity">
                        <Show
                          when={standing.avatar_url}
                          fallback={<span class="gen-avatar-blank" aria-hidden="true" />}
                        >
                          <MinerAvatar url={standing.avatar_url} />
                        </Show>
                        <span class="gen-identity-text">
                          <span class="gen-identity-name">
                            <EntityButton
                              kind="agent"
                              id={standing.agent_id}
                              label={standing.agent_name || monoDisplay(standing.agent_id)}
                              class="gen-name"
                              title="Open the agent"
                            />
                            <Show when={standing.agent_version != null}>
                              <span class="gen-version">v{standing.agent_version}</span>
                            </Show>
                            <Show when={standing.reference}>
                              <small title="A reference agent the operator seats on every panel">
                                reference
                              </small>
                            </Show>
                          </span>
                        </span>
                      </span>
                    </td>
                    <td class="num">{fx(standing.pairwise_score)}</td>
                    <td class="num hide-sm">
                      {standing.wins} / {standing.ties} / {standing.losses}
                    </td>
                    <td class="num">
                      {fx(standing.common_mean)} <small>({standing.common_cases})</small>
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
      </Show>
    </section>
  );
}

function RoundHistory(props: { rounds: GeneratorRoundSummary[] }): JSX.Element {
  return (
    <section class="gen-section" aria-labelledby="gen-history-title">
      <div class="gen-section-title">
        <h3 id="gen-history-title">Recent rounds</h3>
        <span>Seeds are revealed after the privacy window</span>
      </div>
      <div class="gen-table-scroll">
        <table class="gen-table">
          <thead>
            <tr>
              <th scope="col">Round</th>
              <th scope="col">Status</th>
              <th scope="col" class="hide-sm">
                Commitment
              </th>
              <th scope="col">Opened</th>
              <th scope="col" class="num">
                Generators
              </th>
              <th scope="col" class="hide-sm">
                Revealed seed
              </th>
            </tr>
          </thead>
          <tbody>
            <For each={props.rounds}>
              {(round) => (
                <tr>
                  <td>{round.round_index}</td>
                  <td>
                    <RoundStatus round={round} />
                    <Show when={round.void_reason}>
                      <small> {round.void_reason}</small>
                    </Show>
                  </td>
                  <td class="mono hide-sm" title={round.commitment}>
                    {monoDisplay(round.commitment)}
                    <small>
                      {" "}
                      {round.commitment_publication === "published"
                        ? round.commitment_confirmations + " confirmations"
                        : round.commitment_publication}
                    </small>
                  </td>
                  <td>{round.opened_at ? relTime(round.opened_at) : "Not yet"}</td>
                  <td class="num">{round.column_count || "–"}</td>
                  <td class="mono hide-sm" title={round.seed ?? undefined}>
                    {round.seed ? monoDisplay(round.seed) : "Private"}
                  </td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function selectedGenerator(): string | null {
  const id = spaQuery().get("generator")?.toLowerCase() ?? null;
  return isSubmissionId(id) ? id : null;
}

export function GeneratorsPage(): JSX.Element {
  const board = useEndpoint<GeneratorBoard>("/generator/board", { pollMs: REFRESH_MS });
  const rounds = useEndpoint<GeneratorRoundList>("/generator/rounds", { pollMs: REFRESH_MS });
  const terms = useEndpoint<GeneratorUploadTerms>("/generator/upload-terms");
  const allocation = useEndpoint<EmissionAllocation>("/public/treasury-allocation");
  const epoch = chainEpoch();

  const boardData = fromEndpoint(board);
  const roundList = fromEndpoint(rounds);
  const termsData = fromEndpoint(terms);
  const allocationData = fromEndpoint(allocation);

  // Either the board or the round list names the newest scored round, so its
  // detail starts loading as soon as the first of the two answers. Keyed on
  // the index: unchanged polls reuse the detail already loaded.
  const scoredIndex = createMemo(() => newestScoredIndex(roundList(), boardData()));
  const [detail, { refetch: refetchDetail }] = createResource(scoredIndex, (index) =>
    getJSON<GeneratorRoundDetail>("/generator/rounds/" + index),
  );
  const detailData = lastGood(
    () => (detail.error ? undefined : detail()),
    () => detail.error,
  );

  const payouts = createMemo(() => projectGeneratorPayouts(boardData()?.entries ?? []));
  const schedule = () => roundList()?.schedule;
  const nextIndex = () => nextRoundIndex(schedule());
  const nextRound = () => {
    const next = nextIndex();
    if (next == null) return null;
    return roundList()?.rounds.find((round) => round.round_index === next) ?? null;
  };
  const currentRound = () => {
    const current = schedule()?.current_round;
    return roundList()?.rounds.find((round) => round.round_index === current) ?? null;
  };
  const eta = () => nextRoundEta(schedule(), epoch());
  const mode = () => boardData()?.mode ?? schedule()?.mode;
  const generatorShare = () => generatorEmissionShare(allocationData());

  const [selected, setSelected] = createSignal(selectedGenerator());
  const restore = () => setSelected(selectedGenerator());
  window.addEventListener("popstate", restore);
  onCleanup(() => window.removeEventListener("popstate", restore));

  return (
    <section class="page active generators-page" data-page="generators">
      <header class="gen-intro">
        <Show when={mode()}>
          {(value) => (
            <p class="gen-mode">
              Emissions{" "}
              <StatusChip
                label={(MODE_CHIP[value()] ?? [value(), ""])[0]}
                tone={(MODE_CHIP[value()] ?? [value(), ""])[1]}
              />
            </p>
          )}
        </Show>
        <p>
          Miners submit case generators. Each round, every counted generator writes cases from a
          committed secret seed and a panel of top agents answers them. Generators earn for
          discriminating cases: ones that some panel agents solve and others miss.{" "}
          <a href={GUIDE_URL} target="_blank" rel="noopener">
            How the competition works
          </a>
        </p>
      </header>

      <section class="gen-section gen-rounds-now" aria-label="Round schedule">
        <Show when={rounds.error()}>
          <SectionError
            what="Round schedule"
            stale={!!roundList()}
            onRetry={() => rounds.refresh()}
          />
        </Show>
        <Show
          when={roundList()}
          fallback={
            <Show when={!rounds.error()}>
              <dl class="gen-stats" role="status" aria-label="Loading rounds">
                <For each={["Current round", "Next round", "Next commitment", "Round length"]}>
                  {(label) => (
                    <div>
                      <dt>{label}</dt>
                      <dd>
                        <span class="gen-skel gen-skel-stat" />
                      </dd>
                    </div>
                  )}
                </For>
              </dl>
            </Show>
          }
        >
          <dl class="gen-stats">
            <div>
              <dt>Current round</dt>
              <dd>
                <Show when={currentRound()} fallback={schedule()?.current_round ?? "–"}>
                  {(round) => (
                    <>
                      {round().round_index} <RoundStatus round={round()} />
                    </>
                  )}
                </Show>
              </dd>
            </div>
            <div>
              <dt>Next round</dt>
              <dd>
                {nextIndex() ?? "–"}
                <Show when={eta()}>
                  {(at) => (
                    <small title={at().toISOString()}>
                      {" "}
                      opens {relTimeUntil(at().toISOString())}
                    </small>
                  )}
                </Show>
              </dd>
            </div>
            <div>
              <dt>Next commitment</dt>
              <dd>
                <Show when={nextRound()} fallback="Not committed yet">
                  {(round) => (
                    <span title={round().commitment}>
                      {round().commitment_publication === "published"
                        ? "Published · " + round().commitment_confirmations + " confirmations"
                        : "Publication " + round().commitment_publication}
                    </span>
                  )}
                </Show>
              </dd>
            </div>
            <div>
              <dt>Round length</dt>
              <dd>
                {schedule()?.epochs_per_round} epoch
                {schedule()?.epochs_per_round === 1 ? "" : "s"}
              </dd>
            </div>
          </dl>
        </Show>
      </section>

      <PayoutTable
        board={boardData()}
        rows={payouts()}
        generatorShare={generatorShare()}
        loading={board.loading()}
        failed={!!board.error()}
        onRetry={() => board.refresh()}
      />

      <EmissionSplit allocation={allocationData()} />

      <Show when={scoredIndex() !== null || detail.error || (!boardData() && !roundList())}>
        <RoundDetail
          // The heading follows the data on screen: while a newly scored
          // round loads, the previous round stays labelled as itself.
          index={detailData()?.summary.round_index ?? scoredIndex()}
          detail={detailData()}
          failed={!!detail.error}
          onRetry={() => void refetchDetail()}
        />
      </Show>

      <Show when={roundList()?.rounds.length}>
        <RoundHistory rounds={roundList()?.rounds ?? []} />
      </Show>

      <section class="gen-section" aria-labelledby="gen-terms-title">
        <div class="gen-section-title">
          <h3 id="gen-terms-title">Submitting a generator</h3>
        </div>
        <Show when={terms.error() && !termsData()}>
          <SectionError what="Upload terms" onRetry={() => terms.refresh()} />
        </Show>
        <Show when={termsData()}>
          {(value) => (
            <>
              <dl class="gen-stats">
                <div>
                  <dt>Uploads</dt>
                  <dd>{value().accepting ? "Open" : "Paused"}</dd>
                </div>
                <div>
                  <dt>Upload fee</dt>
                  <dd>
                    {value().payment_required ? taoAmount(value().payment_amount_rao) : "None"}
                  </dd>
                </div>
                <div>
                  <dt>Cooldown per hotkey</dt>
                  <dd>{cooldownText(value().upload_cooldown_seconds)}</dd>
                </div>
              </dl>
              <Show when={value().blockers.length}>
                <ul class="gen-note">
                  <For each={value().blockers}>{(blocker) => <li>{blocker}</li>}</For>
                </ul>
              </Show>
            </>
          )}
        </Show>
        <p class="gen-note">
          Upload with <code>ditto generator upload --name &quot;your generator&quot;</code>. It
          checks these terms and the name, refuses to pay while anything blocks the upload, then
          pays the fee and attaches the proof. The board shows the name next to your miner profile
          picture (<code>ditto avatar set</code>).
        </p>
      </section>

      <Show when={selected()} keyed>
        {(id) => (
          <GeneratorPanel
            submissionId={id}
            payout={payoutFor(payouts(), id)}
            generatorShare={generatorShare()}
            onClose={() => showGenerator(null)}
          />
        )}
      </Show>
    </section>
  );
}
