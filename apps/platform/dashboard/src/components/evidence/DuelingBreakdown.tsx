// Bench v16+ dueling breakdown for one validator run. A v16 composite is
// `gate x ((1 - w x |U|) x datagen + w x sum col)`: the datagen half plus five
// pinned generator columns, the agent's own owner's columns masked, all times
// the run's composite gate (which carries the priced-cost factor). Platform
// projects these numbers from the stored aggregates; this panel only lays them
// out so a miner can see each column and the cost factor behind the score.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import type { DuelingBreakdown, DuelingColumn } from "../../types/leaderboard";

const f4 = (value: number): string => value.toFixed(4);

export function columnSource(column: DuelingColumn): string {
  if (column.substituted) return "public default (substituted)";
  if (column.public_default) return "public default";
  return "miner generator";
}

export function DuelingBreakdownPanel(props: {
  breakdown: DuelingBreakdown | null | undefined;
}): JSX.Element {
  return (
    <Show when={props.breakdown}>
      {(b) => (
        <section
          class="v9-gate-evidence gate-evidence dueling-breakdown"
          data-dueling-breakdown
          aria-label="Bench v16 dueling breakdown"
        >
          <div class="v9-gate-heading">
            <strong>Dueling composite</strong>
            <span>{"season " + b().season_index}</span>
          </div>
          <div class="v9-gate-row" data-dueling-row="formula">
            <code>{b().formula}</code>
            <div class="v9-gate-metrics">
              <span>
                datagen <b data-dueling-metric="datagen_raw">{f4(b().datagen_raw)}</b> ×{" "}
                {f4(b().datagen_share)}
              </span>
              <span>
                columns <b data-dueling-metric="generator_sum">{f4(b().generator_sum)}</b> (
                {b().counted_columns} counted, w {b().column_weight})
              </span>
              <span>
                gate <b data-dueling-metric="datagen_gate">{f4(b().datagen_gate)}</b>
              </span>
              <span>
                = <b data-dueling-metric="composite">{f4(b().composite)}</b>
              </span>
            </div>
          </div>
          <div class="v9-gate-row" data-dueling-row="priced-cost">
            <div class="v9-gate-metrics">
              <Show
                when={typeof b().priced_cost_applied_factor_bps === "number"}
                fallback={<span>No priced cost recorded for this run.</span>}
              >
                <span>
                  priced-cost factor{" "}
                  <b data-dueling-metric="priced_cost_factor">
                    {f4((b().priced_cost_applied_factor_bps as number) / 10_000)}
                  </b>
                  <Show when={b().priced_cost_window}>{(w) => " (" + w() + ")"}</Show>
                  <Show
                    when={
                      typeof b().priced_cost_factor_bps === "number" &&
                      b().priced_cost_factor_bps !== b().priced_cost_applied_factor_bps
                    }
                  >
                    {" · curve " +
                      f4((b().priced_cost_factor_bps as number) / 10_000) +
                      " not applied"}
                  </Show>
                </span>
                <Show when={typeof b().other_gate_factor === "number"}>
                  <span>
                    other gates <b>{f4(b().other_gate_factor as number)}</b>
                  </span>
                </Show>
              </Show>
            </div>
          </div>
          <div class="v9-gate-row" data-dueling-row="columns">
            <table class="dueling-columns">
              <thead>
                <tr>
                  <th scope="col">Column</th>
                  <th scope="col">Source</th>
                  <th scope="col">Grading</th>
                  <th scope="col">n</th>
                  <th scope="col">Sum</th>
                  <th scope="col">Mean</th>
                  <th scope="col">Adds</th>
                </tr>
              </thead>
              <tbody>
                <For each={b().columns}>
                  {(column) => (
                    <tr
                      data-dueling-column={column.column_id}
                      data-self-masked={column.self_masked ? "true" : "false"}
                    >
                      <td>{column.column_id}</td>
                      <td>{columnSource(column)}</td>
                      <td>
                        {column.grading_bench_version != null
                          ? "v" + column.grading_bench_version
                          : "—"}
                      </td>
                      <td>{column.n}</td>
                      <td>{column.sum.toFixed(2)}</td>
                      <td>{f4(column.mean)}</td>
                      <td>{column.self_masked ? "masked (own)" : f4(column.contribution)}</td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </div>
          <p>
            A column built by this agent's own owner is masked and its weight returns to the datagen
            half. The priced-cost factor is already inside the gate.
          </p>
        </section>
      )}
    </Show>
  );
}
