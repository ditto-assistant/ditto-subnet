// Small shared props for the theater's scenes: name tags, the conveyor belt,
// and the case cards generators throw. Paper style throughout: light paper
// fills, one ink line weight, round joins.
import { For, Match, Switch } from "solid-js";
import type { JSX } from "solid-js";

/** Truncate a label so a tag never outgrows the figure it names. */
export function clip(text: string, max = 14): string {
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

/** A paper name tag centred on (x, y). */
export function NameTag(props: {
  x: number;
  y: number;
  text: string;
  max?: number;
  tone?: "plain" | "good" | "warn" | "danger";
}): JSX.Element {
  const label = (): string => clip(props.text, props.max ?? 14);
  const width = (): number => Math.max(34, label().length * 6.1 + 12);
  return (
    <g
      class="scene-tag"
      data-tone={props.tone ?? "plain"}
      transform={`translate(${props.x} ${props.y})`}
    >
      <rect x={-width() / 2} y="-8" width={width()} height="16" rx="3" />
      <text y="4" text-anchor="middle">
        {label()}
      </text>
    </g>
  );
}

/** A conveyor belt from x1 to x2 with its top at y. */
export function Conveyor(props: { x1: number; x2: number; y: number }): JSX.Element {
  const rollers = (): number[] => {
    const out: number[] = [];
    for (let x = props.x1 + 14; x < props.x2 - 8; x += 28) out.push(x);
    return out;
  };
  return (
    <g class="scene-conveyor">
      <rect
        class="scene-belt"
        x={props.x1}
        y={props.y}
        width={props.x2 - props.x1}
        height="16"
        rx="8"
      />
      <line
        class="scene-belt-run"
        x1={props.x1 + 8}
        x2={props.x2 - 8}
        y1={props.y + 3}
        y2={props.y + 3}
      />
      <For each={rollers()}>
        {(x) => (
          <g class="scene-roller" transform={`translate(${x} ${props.y + 8})`}>
            <g class="scene-roller-spin">
              <circle r="5" />
              <path d="M-5 0h10" />
            </g>
          </g>
        )}
      </For>
      <path
        class="scene-legs"
        d={`M${props.x1 + 18} ${props.y + 16}v22M${props.x2 - 18} ${props.y + 16}v22`}
      />
    </g>
  );
}

/** A case card, centred on the origin. */
export function CaseCard(props: { prop: "maze" | "sticky" | "knot" | "copy" }): JSX.Element {
  return (
    <g class="case-card" data-prop={props.prop}>
      <Switch>
        <Match when={props.prop === "maze"}>
          {/* A labyrinth tile: the case that separates agents. */}
          <rect x="-13" y="-13" width="26" height="26" rx="3" class="case-sheet" />
          <path class="case-maze" d="M-9-9h18v18h-14v-10h10v6h-5M-9-4v13M0-9v5" fill="none" />
        </Match>
        <Match when={props.prop === "sticky"}>
          <path class="case-sticky" d="M-11-11h22v16l-6 6h-16Z" />
          <path class="case-fold" d="M11 5h-6v6" />
          <path d="M-6-4h12M-6 1h8" class="case-lines" />
        </Match>
        <Match when={props.prop === "knot"}>
          {/* A crumpled ball: nobody can solve it. */}
          <path class="case-sheet" d="M-11-2-7-10 2-11 9-7 11 2 6 10-3 11-10 6Z" />
          <path class="case-lines" d="M-6-5 1-2 6-6M-7 3l6-1 5 5M0-2v9" />
        </Match>
        <Match when={props.prop === "copy"}>
          <rect x="-8" y="-14" width="22" height="24" rx="2" class="case-sheet case-back" />
          <rect x="-13" y="-10" width="22" height="24" rx="2" class="case-sheet" />
          <path d="M-9-4h14M-9 1h14M-9 6h9" class="case-lines" />
        </Match>
      </Switch>
    </g>
  );
}

/** The "DUPE" rubber stamp that lands on a duplicate case. */
export function DupeStamp(): JSX.Element {
  return (
    <g class="case-stamp">
      <rect x="-20" y="-9" width="40" height="18" rx="3" />
      <text y="4" text-anchor="middle">
        DUPE
      </text>
    </g>
  );
}
