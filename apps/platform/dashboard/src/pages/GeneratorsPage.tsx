// Generator competition (/generators). Miners submit case generators; each
// round runs every counted generator against a panel of top agents and pays
// generators whose cases separate those agents. The page reads only public
// endpoints: the generator board, rounds and upload terms, the shared chain
// epoch, and the treasury allocation for the emission split.
import { For, Show, createMemo, createResource } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../data/useEndpoint";
import { weightsResource } from "../data/weights";
import { getJSON } from "../lib/api";
import { REFRESH_MS } from "../lib/config";
import { fx, monoDisplay, relTime, relTimeUntil, shortKey } from "../lib/format";
import { EntityButton } from "../components/ui/EntityButton";
import { StatusChip } from "../components/ui/StatusChip";
import type {
  EmissionAllocation,
  EmissionShare,
  GeneratorBoard,
  GeneratorMode,
  GeneratorRoundColumn,
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
  latestScoredRound,
  nextRoundEta,
  projectGeneratorPayouts,
  taoAmount,
} from "../components/generators/generator-data";

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

function safe<T>(resource: { data: () => T | undefined; error: () => unknown }): T | undefined {
  return resource.error() ? undefined : resource.data();
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

function CaseBar(props: { column: GeneratorRoundColumn }): JSX.Element {
  const total = () => columnCases(props.column) || 1;
  const parts = () =>
    [
      ["discriminating", props.column.discriminating],
      ["trivial", props.column.trivial],
      ["unsolved", props.column.unsolved],
      ["duplicate", props.column.duplicate],
    ] as const;
  return (
    <span
      class="gen-casebar"
      role="img"
      aria-label={parts()
        .map(([name, count]) => count + " " + name)
        .join(", ")}
    >
      <For each={parts().filter(([, count]) => count > 0)}>
        {([name, count]) => (
          <span class={"gen-case " + name} style={{ width: (count / total()) * 100 + "%" }} />
        )}
      </For>
    </span>
  );
}

function PayoutTable(props: { board: GeneratorBoard; generatorShare: number | null }): JSX.Element {
  const rows = createMemo(() => projectGeneratorPayouts(props.board.entries));
  const anyPaid = () => rows().some((row) => row.position !== null);
  return (
    <section class="gen-section" aria-labelledby="gen-board-title">
      <div class="gen-section-title">
        <h3 id="gen-board-title">Projected payouts</h3>
        <span>
          Latest scored run per hotkey · {props.board.window_days}-day window · updated{" "}
          {relTime(props.board.generated_at)}
        </span>
      </div>
      <Show when={props.board.mode !== "enforce"}>
        <p class="gen-note">
          The competition is in {props.board.mode} mode, so validators pay generators nothing yet.
          The positions below show what they would earn once it pays.
        </p>
      </Show>
      <Show when={!anyPaid()}>
        <p class="gen-note">
          No generator is eligible right now, so the generator share pays agent miners.
        </p>
      </Show>
      <div class="gen-table-scroll">
        <table class="gen-table" aria-label="Projected generator payouts">
          <thead>
            <tr>
              <th scope="col">Position</th>
              <th scope="col">Miner</th>
              <th scope="col" class="num">
                Score
              </th>
              <th scope="col" class="num">
                Discriminating cases
              </th>
              <th scope="col" class="num">
                Share of generator track
              </th>
              <th scope="col" class="num">
                Share of miner emission
              </th>
              <th scope="col">First seen</th>
            </tr>
          </thead>
          <tbody>
            <Show
              when={rows().length}
              fallback={
                <tr>
                  <td colspan={7} class="empty">
                    <div class="empty-msg">No generator has a scored run in the window.</div>
                  </td>
                </tr>
              }
            >
              <For each={rows()}>
                {(row) => (
                  <tr classList={{ unpaid: row.position === null }}>
                    <td>
                      {row.position !== null
                        ? "#" + row.position
                        : UNPAID_LABEL[row.unpaidReason ?? "zero_score"]}
                    </td>
                    <td>
                      <EntityButton
                        kind="miner"
                        id={row.entry.miner_hotkey}
                        label={shortKey(row.entry.miner_hotkey)}
                        title="Open the miner profile"
                      />
                    </td>
                    <td class="num">{fx(row.entry.score)}</td>
                    <td class="num">{row.entry.discriminating_cases}</td>
                    <td class="num">{row.position !== null ? share(row.trackShare) : "–"}</td>
                    <td class="num">
                      {row.position === null
                        ? "–"
                        : props.generatorShare === null
                          ? "Split not published"
                          : share(row.trackShare * props.generatorShare)}
                    </td>
                    <td>{relTime(row.entry.first_seen)}</td>
                  </tr>
                )}
              </For>
            </Show>
          </tbody>
        </table>
      </div>
      <p class="gen-note">
        Validators rank eligible generators with a score above zero by score, then first seen, keep
        one per hotkey, and pay positions{" "}
        {GENERATOR_RANK_SHARES.map((value) => share(value)).join(" / ")}. With fewer than{" "}
        {GENERATOR_RANK_SHARES.length} paid generators those shares are rescaled to fill the
        generator track.
      </p>
    </section>
  );
}

function RoundDetail(props: { detail: GeneratorRoundDetail }): JSX.Element {
  const cases = () => {
    const counts = props.detail.columns.map(columnCases);
    return counts.length && counts.every((count) => count === counts[0]) ? counts[0] : null;
  };
  return (
    <section class="gen-section" aria-labelledby="gen-round-title">
      <div class="gen-section-title">
        <h3 id="gen-round-title">Round {props.detail.summary.round_index} results</h3>
        <span>
          Scored {relTime(props.detail.summary.scored_at)} · {props.detail.summary.panel_size} panel
          agents
          <Show when={cases() !== null}> · {cases()} cases per generator</Show>
        </span>
      </div>
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
              <th scope="col" class="num">
                Trivial
              </th>
              <th scope="col" class="num">
                Unsolved
              </th>
              <th scope="col" class="num">
                Duplicate
              </th>
            </tr>
          </thead>
          <tbody>
            <For each={[...props.detail.columns].sort((a, b) => b.score - a.score)}>
              {(column) => (
                <tr>
                  <td>
                    <Show
                      when={column.miner_hotkey}
                      fallback={<span class="gen-default">Public default generator</span>}
                    >
                      {(hotkey) => (
                        <EntityButton
                          kind="miner"
                          id={hotkey()}
                          label={shortKey(hotkey())}
                          title="Open the miner profile"
                        />
                      )}
                    </Show>
                  </td>
                  <td class="num">{fx(column.score)}</td>
                  <td>
                    <CaseBar column={column} />
                  </td>
                  <td class="num">{column.discriminating}</td>
                  <td class="num">{column.trivial}</td>
                  <td class="num">{column.unsolved}</td>
                  <td class="num">{column.duplicate}</td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </div>
      <ul class="gen-case-legend" aria-label="Case outcomes">
        <li>
          <span class="gen-case discriminating" aria-hidden="true" /> Discriminating: some panel
          agents solve it and others miss it (earns credit)
        </li>
        <li>
          <span class="gen-case trivial" aria-hidden="true" /> Trivial: every eligible agent solves
          it
        </li>
        <li>
          <span class="gen-case unsolved" aria-hidden="true" /> Unsolved: too few agent owners solve
          it
        </li>
        <li>
          <span class="gen-case duplicate" aria-hidden="true" /> Duplicate: repeats an earlier
          column's or round's case
        </li>
      </ul>
      <Show when={props.detail.standings.length}>
        <div class="gen-table-scroll">
          <table class="gen-table">
            <caption>Panel agents on this round's cases</caption>
            <thead>
              <tr>
                <th scope="col">Rank</th>
                <th scope="col">Agent</th>
                <th scope="col" class="num">
                  Pairwise score
                </th>
                <th scope="col" class="num">
                  Wins / ties / losses
                </th>
                <th scope="col" class="num">
                  Mean on shared cases
                </th>
              </tr>
            </thead>
            <tbody>
              <For each={props.detail.standings}>
                {(standing) => (
                  <tr>
                    <td>#{standing.rank}</td>
                    <td>
                      <EntityButton
                        kind="agent"
                        id={standing.agent_id}
                        label={monoDisplay(standing.agent_id)}
                        title="Open the agent"
                      />
                    </td>
                    <td class="num">{fx(standing.pairwise_score)}</td>
                    <td class="num">
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
              <th scope="col">Commitment</th>
              <th scope="col">Opened</th>
              <th scope="col" class="num">
                Generators
              </th>
              <th scope="col">Revealed seed</th>
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
                  <td class="mono" title={round.commitment}>
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
                  <td class="mono" title={round.seed ?? undefined}>
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

export function GeneratorsPage(): JSX.Element {
  const board = useEndpoint<GeneratorBoard>("/generator/board", { pollMs: REFRESH_MS });
  const rounds = useEndpoint<GeneratorRoundList>("/generator/rounds", { pollMs: REFRESH_MS });
  const terms = useEndpoint<GeneratorUploadTerms>("/generator/upload-terms");
  const allocation = useEndpoint<EmissionAllocation>("/public/treasury-allocation");
  const epoch = chainEpoch();

  const roundList = () => safe(rounds);
  const scoredRound = () => latestScoredRound(roundList())?.round_index;
  // Keyed on the round index so a new scored round refetches its detail
  // while unchanged polls reuse the one already loaded.
  const [detail] = createResource(scoredRound, (index) =>
    getJSON<GeneratorRoundDetail>("/generator/rounds/" + index),
  );
  const detailData = () => (detail.error ? undefined : detail());
  const schedule = () => roundList()?.schedule;
  const nextRound = () => {
    const next = schedule()?.next_round_epoch;
    return roundList()?.rounds.find((round) => round.round_index === next) ?? null;
  };
  const currentRound = () => {
    const current = schedule()?.current_round;
    return roundList()?.rounds.find((round) => round.round_index === current) ?? null;
  };
  const eta = () => nextRoundEta(schedule(), epoch());
  const mode = () => safe(board)?.mode ?? schedule()?.mode;
  const generatorShare = () => generatorEmissionShare(safe(allocation));

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
        <Show when={rounds.loading() && !roundList()}>
          <p role="status">Loading rounds…</p>
        </Show>
        <Show when={rounds.error()}>
          <p role="alert">
            Round schedule could not be loaded.{" "}
            <button class="btn" onClick={() => rounds.refresh()}>
              Try again
            </button>
          </p>
        </Show>
        <Show when={roundList()}>
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
                {schedule()?.next_round_epoch ?? "–"}
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

      <EmissionSplit allocation={safe(allocation)} />

      <Show when={board.error()}>
        <p role="alert">
          Generator board could not be loaded.{" "}
          <button class="btn" onClick={() => board.refresh()}>
            Try again
          </button>
        </p>
      </Show>
      <Show when={safe(board)}>
        {(value) => <PayoutTable board={value()} generatorShare={generatorShare()} />}
      </Show>

      <Show when={detailData()}>{(value) => <RoundDetail detail={value()} />}</Show>

      <Show when={roundList()?.rounds.length}>
        <RoundHistory rounds={roundList()?.rounds ?? []} />
      </Show>

      <Show when={safe(terms)}>
        {(value) => (
          <section class="gen-section" aria-labelledby="gen-terms-title">
            <div class="gen-section-title">
              <h3 id="gen-terms-title">Submitting a generator</h3>
            </div>
            <dl class="gen-stats">
              <div>
                <dt>Uploads</dt>
                <dd>{value().accepting ? "Open" : "Paused"}</dd>
              </div>
              <div>
                <dt>Upload fee</dt>
                <dd>{value().payment_required ? taoAmount(value().payment_amount_rao) : "None"}</dd>
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
            <p class="gen-note">
              Upload with <code>ditto generator upload</code>. It checks these terms, refuses to pay
              while anything blocks the upload, then pays the fee and attaches the proof.
            </p>
          </section>
        )}
      </Show>
    </section>
  );
}
