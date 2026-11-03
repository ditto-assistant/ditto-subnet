// Paper figures for the pipeline theater: the Paper Ditto (submitted agents)
// and the Paper person (data generators).
//
// Ported from ditto-app's Paper family design draft (ditto-assistant/
// ditto-app#3482; DITTO-1527): body contours and accessories from
// src/mockups/sets/agent-avatars/PaperCharacter.tsx, the floating mitten hand
// from PaperWorkScene.tsx, and the face rig geometry from
// src/assets/paperditto-live.svg. Same organisation; the shapes are copied,
// the 39 KB live rig is not — the theater draws the static features and
// poses them with CSS, so a stage of a dozen characters stays cheap.
//
// Every figure draws in its own 100-unit box (the person is 100 × 140) and
// is placed by the caller's transform. Colour comes from the brand-kit tint
// scoped on the figure (`data-ditto-theme` + light mode, as ditto-app does),
// so a figure keeps its paper colours on a dark dashboard.
import { For, Match, Show, Switch } from "solid-js";
import type { JSX } from "solid-js";

import { BUBBLE_PATH } from "./bubble-path";
import type { DittoPersona, HumanPersona } from "./persona";
import { HAIR_INKS, SKINS } from "./persona";

export type Mood = "idle" | "focus" | "cheer" | "bonk" | "worried" | "sleepy";

const BUBBLE_ID = "theater-bubble";

/** Shared definitions; render once per theater. */
export function PaperDefs(): JSX.Element {
  return (
    <svg class="theater-defs" width="0" height="0" aria-hidden="true">
      <defs>
        <path id={BUBBLE_ID} d={BUBBLE_PATH} />
      </defs>
    </svg>
  );
}

function BodyContour(props: { body: number }): JSX.Element {
  return (
    <Switch>
      <Match when={props.body === 1}>
        <path d="M11 7h58l20 20v64H11Z" />
        <path d="M69 7v20h20" fill="var(--ditto-paper-raised)" />
      </Match>
      <Match when={props.body === 2}>
        <path d="M16 6h68v88L50 79 16 94Z" />
        <path d="M76 15v63" fill="none" opacity=".22" />
      </Match>
      <Match when={props.body === 3}>
        <path d="m10 12 18-5 17 6 18-6 26 8v73l-20 5-19-6-18 6-22-7Z" />
        <path d="M28 8v83m35-82v80" fill="none" opacity=".18" />
      </Match>
      <Match when={props.body === 4}>
        <path d="M17 16h66v68H17Z" />
        <rect x="8" y="6" width="84" height="19" rx="9.5" fill="var(--ditto-paper-raised)" />
        <rect x="8" y="75" width="84" height="19" rx="9.5" fill="var(--ditto-paper-raised)" />
        <path d="M20 10q-9 0-7 9m67 60q9 0 7 9" fill="none" />
      </Match>
      <Match when={props.body === 5}>
        <path d="M10 9h80v28c-10 0-10 22 0 22v32H10V59c10 0 10-22 0-22Z" />
        <path d="M23 16h54M23 83h54" stroke-dasharray="2 3" opacity=".4" />
      </Match>
      <Match when={props.body === 6}>
        <path d="m24 4 69 15-16 76L7 79Z" fill="var(--ditto-paper-raised)" />
        <path d="m11 15 67-7 11 77-70 9Z" />
      </Match>
    </Switch>
  );
}

/** The Paper Ditto face features in their native 96-unit rig. */
function DittoFace(props: { eyewear: number }): JSX.Element {
  return (
    <g class="pf-face" transform="scale(1.0417)">
      <path class="pf-brow pf-brow-l" d="M25.32 27.88Q33.72 22.42 42.12 25.78" />
      <path class="pf-brow pf-brow-r" d="M70.68 27.88Q62.28 22.42 53.88 25.78" />
      <g class="pf-eyes" data-eyewear={props.eyewear}>
        <g class="pf-eye">
          <circle cx="33.09" cy="45.94" r="12.18" />
          <circle class="pf-glint" cx="28.71" cy="41.56" r="3.65" />
        </g>
        <g class="pf-eye">
          <circle cx="62.91" cy="45.94" r="12.18" />
          <circle class="pf-glint" cx="58.53" cy="41.56" r="3.65" />
        </g>
      </g>
      <g class="pf-mouth">
        <path class="pf-smile" d="M40.02 66.1C43.17 70.86 52.83 70.86 55.98 66.1" />
        <path
          class="pf-grin"
          d="M40.02 66.1C43.17 70.86 52.83 70.86 55.98 66.1C58 79.5 38 79.5 40.02 66.1Z"
        />
        <ellipse class="pf-gasp" cx="48" cy="69" rx="5.5" ry="6.5" />
      </g>
    </g>
  );
}

function Accessories(props: { persona: DittoPersona }): JSX.Element {
  const look = (): DittoPersona => props.persona;
  return (
    <g class="pf-accessories">
      <Switch>
        <Match when={look().headwear === 1}>
          <path d="M50 24V13m0 5C38 18 37 9 37 7c9 0 13 3 13 11Zm0-2C50 8 56 5 63 5c0 9-7 11-13 11Z" />
        </Match>
        <Match when={look().headwear === 2}>
          <path d="M18 24C23 5 42 5 54 9c15 1 22 8 23 17Z" />
          <path d="M15 24c22-4 45-3 70 3-17 5-41 2-70-3Z" fill="var(--ditto-paper-raised)" />
          <path d="M53 10q-5 4-5 11" fill="none" />
        </Match>
        <Match when={look().headwear === 3}>
          <path d="M20 23C20 6 36 3 50 6c18-4 30 4 30 17Z" />
          <rect x="17" y="19" width="66" height="12" rx="5" fill="var(--ditto-paper-raised)" />
          <path d="M27 23v4m9-4v4m9-4v4m9-4v4m9-4v4m9-4v4" />
          <circle cx="50" cy="5" r="6" />
        </Match>
        <Match when={look().headwear === 4}>
          <path d="M68 18 55 8q-9 17 9 20l4-10 12-10q9 17-9 20Z" />
          <circle cx="68" cy="20" r="4" fill="var(--ditto-paper-raised)" />
        </Match>
        <Match when={look().headwear === 5}>
          <ellipse cx="50" cy="6" rx="23" ry="5" fill="none" stroke-width="2.5" />
        </Match>
        <Match when={look().headwear === 6}>
          <path d="m28 22-3-17 15 9L50 2l10 12 15-9-3 17Z" />
          <path d="M30 25h40" />
        </Match>
      </Switch>
      <Show when={look().eyewear > 0}>
        <g fill={look().eyewear === 3 ? "var(--ditto-paper-ink)" : "none"}>
          <For each={[34.469, 65.531]}>
            {(center) => (
              <Show
                when={look().eyewear === 2}
                fallback={
                  <rect
                    x={center - 14.03}
                    y="33.35"
                    width="28.06"
                    height="29"
                    rx={look().eyewear === 3 ? 5 : 10}
                  />
                }
              >
                <circle cx={center} cy="47.85" r="14.3" />
              </Show>
            )}
          </For>
          <path d="M48.5 44.85q1.5-2 3 0M16.44 42.85l4 2m59.12 0 4-2" fill="none" />
          <Show when={look().eyewear === 3}>
            <path d="m27 39-3 7m34-7-3 7" stroke="var(--ditto-paper)" opacity=".7" />
          </Show>
        </g>
      </Show>
      <Switch>
        <Match when={look().extra === 1}>
          <path d="M11 49C7 8 91 8 89 49" fill="none" stroke-width="4" />
          <rect x="6" y="40" width="10" height="24" rx="5" fill="var(--ditto-paper-ink)" />
          <rect x="84" y="40" width="10" height="24" rx="5" fill="var(--ditto-paper-ink)" />
        </Match>
        <Match when={look().extra === 2}>
          <g fill="var(--ditto-paper-ink)" stroke="none" opacity=".55">
            <For
              each={[
                [20, 61],
                [25, 58],
                [29, 62],
                [71, 62],
                [75, 58],
                [80, 61],
              ]}
            >
              {(point) => <circle cx={point[0]} cy={point[1]} r="1.1" />}
            </For>
          </g>
        </Match>
        <Match when={look().extra === 3}>
          <path
            d="m80 66 3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1Z"
            fill="var(--ditto-paper-raised)"
            stroke-width="1.4"
          />
        </Match>
      </Switch>
    </g>
  );
}

/** Little four-point stars that circle a bonked head. */
export function DizzyStars(): JSX.Element {
  return (
    <g class="pf-dizzy" aria-hidden="true">
      <For each={[0, 1, 2]}>
        {(index) => (
          <path
            class="pf-star"
            style={{ "--i": index }}
            d="M0-5 1.4-1.4 5 0 1.4 1.4 0 5-1.4 1.4-5 0-1.4-1.4Z"
          />
        )}
      </For>
    </g>
  );
}

export interface FigureProps {
  /** Top-left of the figure's box, in the scene's units. */
  x: number;
  y: number;
  /** Rendered width of the 100-unit box. */
  size: number;
  mood?: Mood;
  /** Plays the idle bob and blink (also gated by the theater's play state). */
  alive?: boolean;
  /** Mirror the figure horizontally (faces left). */
  flip?: boolean;
  class?: string;
  children?: JSX.Element;
}

/** A Paper Ditto. `children` render inside the figure's 100-unit box, above
 * the body, for props and hands that should move with it. */
export function PaperDitto(
  props: FigureProps & {
    persona: DittoPersona;
    /** Assembly progress on the factory line: a blank sheet, a painted body
     * with its face, or the finished Ditto with accessories (the default). */
    build?: "blank" | "painted" | "done";
  },
): JSX.Element {
  const scale = (): number => props.size / 100;
  return (
    <g
      class={"pf pf-ditto " + (props.class ?? "")}
      transform={`translate(${props.x} ${props.y}) scale(${scale()})`}
      data-ditto-theme={props.persona.tone}
      data-ditto-mode="light"
      data-mood={props.mood ?? "idle"}
      data-build={props.build ?? "done"}
      data-alive={props.alive ? "true" : undefined}
      style={{ "--seed-delay": `${-(props.persona.seed % 2400)}ms` }}
    >
      <g class="pf-bob">
        <g transform={props.flip ? "translate(100 0) scale(-1 1)" : undefined}>
          <Show
            when={props.persona.body > 0}
            fallback={
              <use
                href={"#" + BUBBLE_ID}
                class="pf-bubble"
                transform="scale(1.0417) translate(48 48) scale(4.2) translate(-12 -11.69)"
              />
            }
          >
            <g class="pf-body pf-body-depth" transform="translate(3 3)">
              <BodyContour body={props.persona.body} />
            </g>
            <g class="pf-body">
              <BodyContour body={props.persona.body} />
            </g>
          </Show>
          <DittoFace eyewear={props.persona.eyewear} />
          <Accessories persona={props.persona} />
        </g>
        <Show when={props.mood === "bonk"}>
          <g transform="translate(50 -4)">
            <DizzyStars />
          </g>
        </Show>
        {props.children}
      </g>
    </g>
  );
}

/** The Paper floating mitten hand (ditto-app PaperWorkScene `Hand`), drawn in
 * a 48-unit box centred on (x, y). */
export function PaperHand(props: {
  x: number;
  y: number;
  size?: number;
  rotate?: number;
  point?: boolean;
  fill?: string;
  class?: string;
}): JSX.Element {
  const s = (): number => (props.size ?? 30) / 48;
  // The outer group carries no transform attribute, so CSS can move the
  // hand for a pose without clobbering its placement.
  return (
    <g
      class={"pf-hand " + (props.class ?? "")}
      style={props.fill ? { "--hand-fill": props.fill } : undefined}
    >
      <g
        transform={`translate(${props.x} ${props.y}) rotate(${props.rotate ?? 0}) scale(${s()}) translate(-24 -26)`}
      >
        <path
          d={
            props.point
              ? "M16 29V7a3.5 3.5 0 0 1 7 0v14c0-5 7-5 7 0 1-4 7-3 7 1v4c7-2 7 4 4 8l-8 10H20L9 33c-5-6 1-10 7-4Z"
              : "M14 28V15a3.5 3.5 0 0 1 7 0V8a3.5 3.5 0 0 1 7 0v7a3.5 3.5 0 0 1 7 0v5a3.5 3.5 0 0 1 7 0v12c0 8-6 13-14 13h-4c-5 0-8-2-12-7l-6-7c-4-5 1-10 5-6Z"
          }
        />
        <path d="M21 28v5m7-8v7m7-5v5" fill="none" opacity=".4" />
      </g>
    </g>
  );
}

function Hair(props: { style: number }): JSX.Element {
  return (
    <Switch>
      <Match when={props.style === 0}>
        <path d="M27 30c0-15 10-21 23-21s23 6 23 21c-6-6-14-9-23-9s-17 3-23 9Z" />
      </Match>
      <Match when={props.style === 1}>
        <path d="M24 46C21 22 33 9 50 9s29 13 26 37c-3-2-5-6-5-15-8 1-21-2-28-9-3 6-9 9-14 9 0 9-2 13-5 15Z" />
      </Match>
      <Match when={props.style === 2}>
        <circle cx="50" cy="6" r="8" />
        <path d="M27 31c0-14 10-21 23-21s23 7 23 21c-7-7-15-10-23-10s-16 3-23 10Z" />
      </Match>
      <Match when={props.style === 3}>
        <path d="M24 34c-6-6-2-15 5-15-1-8 8-13 14-9 4-6 14-6 17 0 7-3 15 2 13 9 7 1 9 10 3 15-3-6-8-9-12-9-6 3-21 3-28 0-5 0-9 3-12 9Z" />
      </Match>
      <Match when={props.style === 4}>
        <path d="M26 30l3-14 6 6 4-12 6 9 6-11 4 11 7-8 2 11 7-4-2 12c-6-5-15-8-23-8s-16 3-20 8Z" />
      </Match>
      <Match when={props.style === 5}>
        <path d="M26 30C26 15 36 8 50 8s24 7 24 22Z" fill="var(--ditto-selection)" />
        <rect x="23" y="25" width="54" height="9" rx="4.5" fill="var(--ditto-paper-raised)" />
        <circle cx="50" cy="6" r="5" fill="var(--ditto-paper-raised)" />
      </Match>
    </Switch>
  );
}

/** A Paper person: a data generator, drawn in the Paper style as a
 * cartoony human because generators write humanlike memory traces. */
export function PaperHuman(
  props: FigureProps & { persona: HumanPersona; throwing?: boolean },
): JSX.Element {
  const scale = (): number => props.size / 100;
  const skin = (): string => SKINS[props.persona.skin]!;
  return (
    <g
      class={"pf pf-human " + (props.class ?? "")}
      transform={`translate(${props.x} ${props.y}) scale(${scale()})`}
      data-ditto-theme={props.persona.shirt}
      data-ditto-mode="light"
      data-mood={props.mood ?? "idle"}
      data-alive={props.alive ? "true" : undefined}
      data-throwing={props.throwing ? "true" : undefined}
      style={{
        "--seed-delay": `${-(props.persona.seed % 2400)}ms`,
        "--skin": skin(),
        "--hair": HAIR_INKS[props.persona.hairInk]!,
      }}
    >
      <g class="pf-bob">
        <g transform={props.flip ? "translate(100 0) scale(-1 1)" : undefined}>
          {/* Legs and shoes. */}
          <rect class="pf-leg" x="36" y="100" width="11" height="26" rx="5" />
          <rect class="pf-leg" x="53" y="100" width="11" height="26" rx="5" />
          <path class="pf-shoe" d="M32 128c0-5 4-6 9-6h7v7H33Z" />
          <path class="pf-shoe" d="M52 122h7c5 0 9 1 9 6l-1 1H52Z" />
          {/* Shirt: the generator's paper tint, with a folded collar. */}
          <path class="pf-shirt" d="M26 70c4-10 14-14 24-14s20 4 24 14l5 34H21Z" />
          <path class="pf-collar" d="M41 57l9 10 9-10" />
          <Show when={props.persona.prop === 0}>
            <g class="pf-clipboard">
              <rect x="38" y="74" width="24" height="28" rx="2" />
              <rect x="45" y="71" width="10" height="6" rx="2" class="pf-clip" />
              <path d="M43 84h14M43 90h14M43 96h9" fill="none" opacity=".5" />
            </g>
          </Show>
          {/* Head. */}
          <circle class="pf-head" cx="50" cy="34" r="23" />
          <circle class="pf-ear" cx="27.5" cy="37" r="4.5" />
          <circle class="pf-ear" cx="72.5" cy="37" r="4.5" />
          <g transform="translate(29.8 15.5) scale(0.42)">
            <DittoFace eyewear={props.persona.prop === 2 ? 1 : 0} />
          </g>
          <g class="pf-hair">
            <Hair style={props.persona.hair} />
          </g>
          <Show when={props.persona.prop === 1}>
            <g class="pf-headset">
              <path d="M27 34C25 10 75 10 73 34" fill="none" stroke-width="3" />
              <rect x="22" y="30" width="7" height="13" rx="3.5" />
              <path d="M25 43q2 10 14 10" fill="none" />
            </g>
          </Show>
          <Show when={props.persona.prop === 2}>
            <g class="pf-specs" fill="none">
              <circle cx="43.6" cy="35.2" r="6.2" />
              <circle cx="56.4" cy="35.2" r="6.2" />
              <path d="M49.4 34.4q.6-.8 1.2 0" />
            </g>
          </Show>
          <Show when={props.persona.prop === 3}>
            <g class="pf-pencil" transform="rotate(-30 74 22)">
              <rect x="70" y="8" width="6" height="22" rx="1" />
              <path d="M70 30l3 6 3-6Z" />
            </g>
          </Show>
        </g>
        {props.children}
      </g>
    </g>
  );
}

/** A small inline figure for tables and labels (an HTML `<svg>`). */
export function MiniDitto(props: { persona: DittoPersona; size?: number }): JSX.Element {
  return (
    <svg
      class="theater-mini"
      viewBox="-6 -10 112 116"
      width={props.size ?? 22}
      height={props.size ?? 22}
      aria-hidden="true"
    >
      <PaperDitto persona={props.persona} x={0} y={0} size={100} />
    </svg>
  );
}

export function MiniHuman(props: { persona: HumanPersona; size?: number }): JSX.Element {
  return (
    <svg
      class="theater-mini"
      viewBox="14 0 72 72"
      width={props.size ?? 22}
      height={props.size ?? 22}
      aria-hidden="true"
    >
      <PaperHuman persona={props.persona} x={0} y={0} size={100} />
    </svg>
  );
}
