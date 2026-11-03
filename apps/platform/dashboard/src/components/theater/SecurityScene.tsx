// Act two, Screening: airport security. A Ditto's bags (its submission
// archive) ride the x-ray belt while automated source review runs, and the
// Ditto walks through the detector. The light it walks out under is its real
// screening outcome: green on to the arena, amber to secondary inspection
// (held for review, or screening needs a manual retry), red turned away.
// With nobody in review, the newest submission's own trip replays.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { reviewStages } from "../operations/progress";
import { PaperDitto, PaperHuman } from "./PaperFigures";
import type { Mood } from "./PaperFigures";
import { humanPersona } from "./persona";
import { NameTag } from "./SceneBits";
import type { CastMember, SecurityAct, SecurityLight } from "./theater-model";
import { securityLight } from "./theater-model";

const FLOOR = 270;
const FIG = 54;
const OFFICER = humanPersona("sn118-security-officer");

/** Where a replayed Ditto ends up, and how it looks there. */
export const LIGHT_FINISH: Record<SecurityLight, { x: number; mood: Mood }> = {
  green: { x: 372, mood: "cheer" },
  amber: { x: 404, mood: "worried" },
  red: { x: 176, mood: "worried" },
  pending: { x: 237, mood: "focus" },
};

function Suitcase(props: { x: number; y: number; class?: string }): JSX.Element {
  return (
    <g class={"sec-bag " + (props.class ?? "")} transform={`translate(${props.x} ${props.y})`}>
      <path class="sec-bag-handle" d="M8-24v-5h12v5" />
      <rect class="sec-bag-body" x="0" y="-24" width="28" height="22" rx="3" />
      <path class="sec-bag-strap" d="M9-24v22M19-24v22" />
    </g>
  );
}

function Monitor(props: { stage: number | null; scanning: boolean }): JSX.Element {
  const stages = reviewStages();
  return (
    <g class="sec-monitor" data-scanning={props.scanning ? "true" : undefined}>
      <rect class="sec-monitor-frame" x="92" y="104" width="68" height="46" rx="4" />
      <rect class="sec-monitor-screen" x="98" y="110" width="56" height="34" rx="2" />
      <path class="sec-monitor-bag" d="M112 124h28v14h-28Zm9-4h10v4" />
      <rect class="sec-scanline" x="100" y="111" width="3" height="32" />
      <path class="sec-monitor-stand" d="M126 150v10" />
      <For each={stages}>
        {(stage, index) => (
          <g
            class="sec-ladder"
            data-state={
              props.stage == null
                ? "idle"
                : index() < props.stage
                  ? "done"
                  : index() === props.stage
                    ? "current"
                    : "todo"
            }
          >
            <rect x={98 + index() * 14.5} y="94" width="12" height="6" rx="2" />
            <text x={104 + index() * 14.5} y="89" text-anchor="middle" class="sec-ladder-label">
              {stage.level}
            </text>
          </g>
        )}
      </For>
    </g>
  );
}

function Detector(props: { light: SecurityLight; replay: boolean }): JSX.Element {
  return (
    <g
      class="sec-detector"
      data-light={props.light}
      data-replay={props.replay ? "true" : undefined}
    >
      <path class="sec-arch-outline" d="M237 270V128q0-5 5-5h40q5 0 5 5v142" />
      <path class="sec-arch" d="M237 270V128q0-5 5-5h40q5 0 5 5v142" />
      <path class="sec-arch-panel" d="M232 196h10M282 196h10" />
      <circle class="sec-lamp-base" cx="262" cy="108" r="8" />
      <circle class="sec-lamp" cx="262" cy="108" r="8" />
      <circle class="sec-lamp-glow" cx="262" cy="108" r="15" />
    </g>
  );
}

export function SecurityScene(props: {
  act: SecurityAct;
  featured: CastMember | null;
  motion: boolean;
}): JSX.Element {
  const walker = (): CastMember | null => props.act.scanning[0] ?? null;
  const queued = (): CastMember | null => props.act.scanning[1] ?? null;
  const replay = (): CastMember | null => (walker() ? null : props.featured);
  const replayLight = (): SecurityLight => securityLight(replay()?.status ?? "");
  const bench = (): CastMember[] =>
    props.act.secondary.filter((member) => member.key !== replay()?.key).slice(0, 3);
  const light = (): SecurityLight => (walker() ? "pending" : replayLight());
  return (
    <svg class="theater-scene scene-security" viewBox="0 0 480 300" aria-hidden="true">
      <path class="scene-floor" d={`M8 ${FLOOR}h464`} />
      {/* Secondary inspection: a roped-off bench. */}
      <g class="sec-secondary">
        <path class="sec-rope" d="M380 202h88M384 202v68M464 202v68" />
        <rect class="sec-sign" x="388" y="170" width="72" height="22" rx="3" />
        <text class="sec-sign-text" x="424" y="185" text-anchor="middle">
          Secondary
        </text>
        <rect class="sec-bench" x="388" y="252" width="76" height="8" rx="3" />
        <path class="sec-bench-legs" d="M394 260v10M458 260v10" />
      </g>
      <For each={bench()}>
        {(member, index) => (
          <>
            <PaperDitto
              persona={member.persona}
              x={389 + index() * 24}
              y={220}
              size={34}
              mood="worried"
              alive
            />
            <Show when={index() === 0}>
              <NameTag x={424} y={288} text={member.name} max={12} tone="warn" />
            </Show>
          </>
        )}
      </For>
      {/* X-ray belt and tunnel. */}
      <rect class="scene-belt" x="16" y="214" width="190" height="12" rx="6" />
      <path class="scene-legs" d="M30 226v44M192 226v44" />
      <Show when={queued()}>
        {(member) => (
          <>
            <Suitcase x={24} y={214} />
            <PaperDitto
              persona={member().persona}
              x={4}
              y={FLOOR - 40}
              size={40}
              mood="idle"
              alive
            />
          </>
        )}
      </Show>
      <Show when={walker()}>
        {(member) => (
          <>
            <Suitcase x={112} y={214} class="sec-bag-live" />
            <PaperDitto
              persona={member().persona}
              x={235}
              y={FLOOR - FIG}
              size={FIG}
              mood="focus"
              alive
            />
            <NameTag x={262} y={288} text={member().name} max={12} />
          </>
        )}
      </Show>
      <Show when={replay()}>
        {(member) => (
          <g
            class="sec-replay"
            data-light={replayLight()}
            data-motion={props.motion ? "on" : "off"}
          >
            {/* The animated wrapper carries no transform attribute of its own. */}
            <g class="sec-bag-replay">
              <Suitcase x={170} y={214} />
            </g>
            <g class="sec-walker" style={{ "--fx": `${LIGHT_FINISH[replayLight()].x}px` }}>
              <PaperDitto
                persona={member().persona}
                x={LIGHT_FINISH[replayLight()].x}
                y={FLOOR - FIG}
                size={FIG}
                mood={LIGHT_FINISH[replayLight()].mood}
                alive
              />
              <NameTag
                x={LIGHT_FINISH[replayLight()].x + FIG / 2}
                y={288}
                text={member().name}
                max={12}
                tone={
                  replayLight() === "amber" ? "warn" : replayLight() === "red" ? "danger" : "plain"
                }
              />
            </g>
          </g>
        )}
      </Show>
      <path class="sec-tunnel" d="M80 214v-44q0-10 10-10h72q10 0 10 10v44" />
      <path class="sec-curtain" d="M88 172v40M96 172v40M156 172v40M164 172v40" />
      <Monitor stage={walker()?.reviewStage ?? null} scanning={Boolean(walker() || replay())} />
      <Detector light={light()} replay={Boolean(replay()) && props.motion} />
      <PaperHuman persona={OFFICER} x={300} y={FLOOR - 74} size={53} alive />
      <path class="scene-arrow" d="M440 140h26m-6-5 6 5-6 5" />
    </svg>
  );
}
