// Composite-trend sparkline, a port of sparkSvg (monolith 5691–5708): line +
// area + last-point dot over a normalized [min,max] range, with the numeric
// history announced via aria-label and a <title>.
import { Show, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import { fx } from "../../lib/format";

export interface SparklineProps {
  history: number[] | null | undefined;
  width?: number;
  height?: number;
}

interface SparkGeometry {
  line: string;
  area: string;
  last: readonly [number, number];
  label: string;
  title: string;
}

export function Sparkline(props: SparklineProps): JSX.Element {
  const width = () => props.width || 62;
  const height = () => props.height || 16;
  const geometry = createMemo<SparkGeometry | null>(() => {
    const h = props.history;
    if (!h || h.length < 2) return null;
    const W = width();
    const H = height();
    const min = Math.min(...h);
    const max = Math.max(...h);
    const rng = max - min || 1;
    const pts = h.map(
      (v, i) => [(i / (h.length - 1)) * (W - 2) + 1, H - 1 - ((v - min) / rng) * (H - 3)] as const,
    );
    const line = pts
      .map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1))
      .join(" ");
    const area =
      "M" +
      (pts[0] as readonly [number, number])[0].toFixed(1) +
      " " +
      (H - 1) +
      " " +
      pts.map((p) => "L" + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ") +
      " L" +
      (pts[pts.length - 1] as readonly [number, number])[0].toFixed(1) +
      " " +
      (H - 1) +
      " Z";
    const last = pts[pts.length - 1] as readonly [number, number];
    return {
      line,
      area,
      last,
      label:
        "Composite trend over " +
        h.length +
        " scored runs, " +
        fx(h[0] as number) +
        " to " +
        fx(h[h.length - 1] as number),
      title: "composite over time: " + h.map(fx).join(", "),
    };
  });
  return (
    <Show when={geometry()}>
      {(g) => (
        <svg
          class="spark"
          role="img"
          aria-label={g().label}
          width={width()}
          height={height()}
          viewBox={"0 0 " + width() + " " + height()}
        >
          <title>{g().title}</title>
          <path class="area" d={g().area} />
          <path d={g().line} />
          <circle class="dot" cx={g().last[0].toFixed(1)} cy={g().last[1].toFixed(1)} r="1.7" />
        </svg>
      )}
    </Show>
  );
}
