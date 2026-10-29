// Act one, Build: the factory. Each submission is assembled into its Paper
// Ditto on a conveyor. Queued submissions wait as blank sheets in the
// hopper; a screener's live admission step decides which machine a Ditto
// stands under (stamp the body while fetching and validating, paint it while
// the image builds, dress it while the service boots and is health-checked).
// With nobody on the line, the newest submission's own assembly replays.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { PaperDitto } from "./PaperFigures";
import { Conveyor, NameTag } from "./SceneBits";
import type { CastMember, FactoryAct, Station } from "./theater-model";

const BELT_Y = 232;
const FIG = 58;
const STATIONS: readonly { id: Station; x: number; label: string }[] = [
  { id: "stamp", x: 168, label: "Fetch & validate" },
  { id: "paint", x: 276, label: "Build image" },
  { id: "dress", x: 384, label: "Start & check" },
];

const BUILD_AT: Record<Station, "blank" | "painted" | "done"> = {
  stamp: "blank",
  paint: "painted",
  dress: "done",
};

interface Placed {
  member: CastMember;
  station: Station;
  x: number;
  build: "blank" | "painted" | "done";
}

/** Stand each live Ditto under its machine; an unreported stage takes the
 * first free station, and two at one machine stand side by side. */
export function placeStations(members: readonly CastMember[]): Placed[] {
  const used = new Map<Station, number>();
  return members.map((member) => {
    const free = STATIONS.find((station) => !used.has(station.id)) ?? STATIONS[0]!;
    const station = STATIONS.find((s) => s.id === member.station) ?? free;
    const crowd = used.get(station.id) ?? 0;
    used.set(station.id, crowd + 1);
    return {
      member,
      station: station.id,
      x: station.x - FIG / 2 + (crowd ? (crowd % 2 ? 22 : -22) * Math.ceil(crowd / 2) : 0),
      build: member.station ? BUILD_AT[member.station] : "painted",
    };
  });
}

function Machines(props: { active: Set<Station>; replay: boolean }): JSX.Element {
  const state = (id: Station): string =>
    props.replay ? "replay" : props.active.has(id) ? "working" : "idle";
  return (
    <g class="factory-machines">
      <path class="scene-girder" d="M118 62h330" />
      <path class="scene-girder-rivets" d="M130 62h306" />
      <For each={STATIONS}>
        {(station) => (
          <text class="scene-label" x={station.x} y="48" text-anchor="middle">
            {station.label}
          </text>
        )}
      </For>
      {/* Stamp press: the body is cut from a blank sheet. */}
      <g class="machine machine-stamp" data-state={state("stamp")}>
        <rect class="machine-housing" x="148" y="62" width="40" height="22" rx="3" />
        <g class="machine-stamp-head">
          <rect class="machine-rod" x="164" y="84" width="8" height="30" />
          <rect class="machine-plate" x="141" y="112" width="54" height="16" rx="3" />
          <path class="machine-ink" d="M150 128h36" />
        </g>
      </g>
      {/* Paint nozzle: the tint goes on. */}
      <g class="machine machine-paint" data-state={state("paint")}>
        <rect class="machine-housing" x="256" y="62" width="40" height="22" rx="3" />
        <rect class="machine-rod" x="272" y="84" width="8" height="18" />
        <path class="machine-plate" d="M262 102h28l-8 16h-12Z" />
        <g class="machine-spray">
          <circle cx="270" cy="128" r="3" />
          <circle cx="282" cy="132" r="2.5" />
          <circle cx="276" cy="140" r="3.2" />
          <circle cx="266" cy="146" r="2.2" />
          <circle cx="285" cy="148" r="2.6" />
        </g>
      </g>
      {/* Dressing arm: an accessory is set on its head. */}
      <g class="machine machine-dress" data-state={state("dress")}>
        <rect class="machine-housing" x="364" y="62" width="40" height="22" rx="3" />
        <g class="machine-arm">
          <rect class="machine-rod" x="380" y="84" width="8" height="40" />
          <path class="machine-claw" d="M370 124h28M372 124v10l5 4m19-14v10l-5 4" />
          <path class="machine-hat" d="M373 140c2-9 9-12 11-12s9 3 11 12Z" />
        </g>
      </g>
    </g>
  );
}

export function FactoryScene(props: {
  act: FactoryAct;
  featured: CastMember | null;
  motion: boolean;
}): JSX.Element {
  const placed = (): Placed[] => placeStations(props.act.stations);
  const replay = (): CastMember | null => (placed().length ? null : props.featured);
  const active = (): Set<Station> => new Set(placed().map((p) => p.station));
  const sheets = (): number[] => Array.from({ length: Math.min(props.act.queued, 4) }, (_, i) => i);
  return (
    <svg class="theater-scene scene-factory" viewBox="0 0 480 300" aria-hidden="true">
      <path class="scene-floor" d="M8 270h464" />
      {/* Hopper: queued submissions wait as blank sheets. */}
      <g class="factory-hopper">
        <For each={sheets()}>
          {(index) => (
            <rect
              class="hopper-sheet"
              x={30 + index * 9}
              y={136 - index * 3}
              width="34"
              height="44"
              rx="2"
              transform={`rotate(${index * 5 - 8} ${47 + index * 9} 158)`}
            />
          )}
        </For>
        <path class="hopper-bin" d="M16 150h84l-10 64H26Z" />
        <text class="hopper-count" x="58" y="190" text-anchor="middle">
          {props.act.queued}
        </text>
        <text class="scene-label" x="58" y="204" text-anchor="middle">
          in line
        </text>
        <path class="hopper-chute" d="M90 206l14 22" />
      </g>
      <Machines active={active()} replay={Boolean(replay()) && props.motion} />
      <Conveyor x1={66} x2={464} y={BELT_Y} />
      {/* Exit toward security. */}
      <path class="scene-arrow" d="M440 286h26m-6-5 6 5-6 5" />
      <For each={placed()}>
        {(p) => (
          <>
            <PaperDitto
              persona={p.member.persona}
              x={p.x}
              y={BELT_Y - FIG + 4}
              size={FIG}
              build={p.build}
              mood="focus"
              alive
            />
            <NameTag x={p.x + FIG / 2} y={BELT_Y + 50} text={p.member.name} max={11} />
          </>
        )}
      </For>
      <Show when={replay()}>
        {(member) => (
          <g class="factory-replay" data-motion={props.motion ? "on" : "off"}>
            <g class="replay-walk">
              <g class="replay-layer replay-blank">
                <PaperDitto
                  persona={member().persona}
                  x={355}
                  y={BELT_Y - FIG + 4}
                  size={FIG}
                  build="blank"
                />
              </g>
              <g class="replay-layer replay-painted">
                <PaperDitto
                  persona={member().persona}
                  x={355}
                  y={BELT_Y - FIG + 4}
                  size={FIG}
                  build="painted"
                  mood="focus"
                />
              </g>
              <g class="replay-layer replay-done">
                <PaperDitto
                  persona={member().persona}
                  x={355}
                  y={BELT_Y - FIG + 4}
                  size={FIG}
                  mood="cheer"
                  alive
                />
              </g>
              <NameTag x={384} y={BELT_Y + 50} text={member().name} max={12} />
            </g>
          </g>
        )}
      </Show>
    </svg>
  );
}
