// Round-by-round score for one generator: one series (so no legend box; the
// heading names it), a 2px line with 8px markers on the generator hue, a
// recessive grid, and a hover/focus tooltip per round. The round table under
// it is the table view of the same data.
import { For, Show, createMemo, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { fx, relTime } from "../../lib/format";
import type { GeneratorSubmissionRound } from "./generator-data";

const W = 640;
const H = 168;
const PAD = { top: 12, right: 12, bottom: 24, left: 40 };

function niceMax(value: number): number {
  if (!(value > 0)) return 0.1;
  const steps = [0.05, 0.1, 0.2, 0.25, 0.5, 1];
  return steps.find((step) => step >= value * 1.08) ?? Math.ceil(value * 1.08 * 10) / 10;
}

export function GeneratorRoundChart(props: { rounds: GeneratorSubmissionRound[] }): JSX.Element {
  const ordered = createMemo(() => [...props.rounds].sort((a, b) => a.round_index - b.round_index));
  const [active, setActive] = createSignal<number | null>(null);
  const yMax = createMemo(() => niceMax(Math.max(0, ...ordered().map((r) => r.score))));
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const x = (i: number) =>
    PAD.left + (ordered().length === 1 ? plotW / 2 : (i / (ordered().length - 1)) * plotW);
  const y = (score: number) => PAD.top + plotH - (score / yMax()) * plotH;
  const ticks = createMemo(() => [0, yMax() / 2, yMax()]);
  const path = createMemo(() =>
    ordered()
      .map((r, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(r.score).toFixed(1))
      .join(" "),
  );
  const slot = () => (ordered().length > 1 ? plotW / (ordered().length - 1) : plotW);
  const activeRound = () => {
    const index = active();
    return index === null ? null : (ordered()[index] ?? null);
  };
  const label = () => {
    const rows = ordered();
    if (!rows.length) return "";
    const first = rows[0] as GeneratorSubmissionRound;
    const last = rows[rows.length - 1] as GeneratorSubmissionRound;
    return (
      "Score in " +
      rows.length +
      " scored round" +
      (rows.length === 1 ? "" : "s") +
      ", from round " +
      first.round_index +
      " to " +
      last.round_index +
      ", latest " +
      fx(last.score)
    );
  };
  return (
    <figure class="gen-round-chart">
      <div class="gen-round-chart-plot">
        <svg viewBox={"0 0 " + W + " " + H} role="img" aria-label={label()}>
          <For each={ticks()}>
            {(tick) => (
              <g class="gen-chart-grid">
                <line x1={PAD.left} x2={W - PAD.right} y1={y(tick)} y2={y(tick)} />
                <text x={PAD.left - 8} y={y(tick)} dy="0.32em" text-anchor="end">
                  {fx(tick)}
                </text>
              </g>
            )}
          </For>
          <Show when={ordered().length > 1}>
            <path class="gen-chart-line" d={path()} />
          </Show>
          <For each={ordered()}>
            {(round, i) => (
              <circle
                class="gen-chart-dot"
                classList={{ active: active() === i() }}
                cx={x(i())}
                cy={y(round.score)}
                r={active() === i() ? 5 : 4}
              />
            )}
          </For>
          <Show when={ordered().length > 0}>
            <text class="gen-chart-axis" x={PAD.left} y={H - 6}>
              Round {ordered()[0]?.round_index}
            </text>
            <Show when={ordered().length > 1}>
              <text class="gen-chart-axis" x={W - PAD.right} y={H - 6} text-anchor="end">
                Round {ordered()[ordered().length - 1]?.round_index}
              </text>
            </Show>
          </Show>
          {/* Hit targets: a full-height column per round, wider than the
              marker, so hover and touch land without precision. */}
          <For each={ordered()}>
            {(round, i) => (
              <rect
                class="gen-chart-hit"
                x={x(i()) - slot() / 2}
                y={PAD.top}
                width={slot()}
                height={plotH}
                tabindex="0"
                aria-label={
                  "Round " +
                  round.round_index +
                  ": score " +
                  fx(round.score) +
                  ", rank " +
                  round.rank
                }
                onPointerEnter={() => setActive(i())}
                onPointerLeave={() => setActive(null)}
                onFocus={() => setActive(i())}
                onBlur={() => setActive(null)}
              />
            )}
          </For>
        </svg>
        <Show when={activeRound()}>
          {(round) => (
            <div
              class="gen-chart-tip"
              role="status"
              style={{
                left: ((x(active() ?? 0) / W) * 100).toFixed(2) + "%",
                top: ((y(round().score) / H) * 100).toFixed(2) + "%",
              }}
            >
              <strong>Round {round().round_index}</strong>
              <span>
                Score {fx(round().score)} · rank {round().rank} of {round().columns}
              </span>
              <span>
                {round().discriminating} discriminating · scored {relTime(round().scored_at)}
              </span>
            </div>
          )}
        </Show>
      </div>
    </figure>
  );
}
