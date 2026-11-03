// Act three, Scoring: the arena. The newest scored generator round replays
// as a friendly battle: Paper people (counted generators) throw cases at
// Paper Dittos (the round's panel), and every agent is competing to be the
// most helpful. Easy cases are caught by everyone, stumpers bonk, a
// labyrinth case bounces off one agent into another's hands, and a duplicate
// is stamped mid-air. The boards beside the stage carry the round's real
// totals. Motion runs only while the theater is on screen and motion is
// allowed; otherwise the stage freezes on the round's defining moment.
import { For, Show, createEffect, createMemo, createSignal, onCleanup, untrack } from "solid-js";
import type { JSX } from "solid-js";

import { fx, relTime } from "../../lib/format";
import { EntityButton } from "../ui/EntityButton";
import type { ArenaThrow, RoundScript } from "./round-script";
import { roundSummary } from "./round-script";
import type { ArenaLayout, Flight, ThrowPhase } from "./arena-motion";
import {
  BEAT_MS,
  agentMood,
  arenaLayout,
  flightFor,
  restingPoint,
  tableauThrow,
  throwPhases,
} from "./arena-motion";
import { MiniDitto, MiniHuman, PaperDitto, PaperHand, PaperHuman } from "./PaperFigures";
import { CaseCard, DupeStamp, NameTag } from "./SceneBits";

/** Drives the replay: one throw at a time while `playing` holds. */
function createReplay(
  script: () => RoundScript | null,
  playing: () => boolean,
): { index: () => number; phase: () => ThrowPhase; loop: () => number } {
  const [index, setIndex] = createSignal(0);
  const [phase, setPhase] = createSignal<ThrowPhase>("rest");
  const [loop, setLoop] = createSignal(0);
  createEffect(() => {
    const current = script();
    if (!playing() || !current || current.throws.length === 0) return;
    const timers: ReturnType<typeof setTimeout>[] = [];
    let cancelled = false;
    const run = (at: number): void => {
      if (cancelled) return;
      const shot = current.throws[at % current.throws.length]!;
      setIndex(at % current.throws.length);
      for (const step of throwPhases(shot.outcome)) {
        timers.push(setTimeout(() => !cancelled && setPhase(step.phase), step.at));
      }
      const last = at % current.throws.length === current.throws.length - 1;
      timers.push(
        setTimeout(
          () => {
            if (last) setLoop((n) => n + 1);
            run(at + 1);
          },
          BEAT_MS.next + (last ? BEAT_MS.loopHold : 0),
        ),
      );
    };
    run(untrack(index));
    onCleanup(() => {
      cancelled = true;
      timers.forEach(clearTimeout);
    });
  });
  return { index, phase, loop };
}

function FlyingCase(props: { flight: Flight; shot: ArenaThrow }): JSX.Element {
  const vars = (): JSX.CSSProperties => {
    const f = props.flight;
    return {
      "--x0": `${f.from.x}px`,
      "--y0": `${f.from.y}px`,
      "--x1": `${f.to.x}px`,
      "--y1": `${f.to.y}px`,
      "--x2": `${(f.bounce ?? f.to).x}px`,
      "--y2": `${(f.bounce ?? f.to).y}px`,
      "--p1": `${f.peaks[0] ?? f.from.y}px`,
      "--p2": `${f.peaks[1] ?? f.to.y}px`,
      "--dur": `${f.durationMs}ms`,
    };
  };
  return (
    <g class="flight" data-kind={props.flight.kind} style={vars()}>
      <g class="flight-y">
        <g class="flight-spin">
          <CaseCard prop={props.shot.prop} />
        </g>
        <Show when={props.flight.kind === "dupe"}>
          <DupeStamp />
        </Show>
      </g>
    </g>
  );
}

function RestingCase(props: { flight: Flight; shot: ArenaThrow }): JSX.Element {
  const at = (): { x: number; y: number } => restingPoint(props.flight);
  return (
    <g transform={`translate(${at().x} ${at().y})`}>
      <CaseCard prop={props.shot.prop} />
      <Show when={props.flight.kind === "dupe"}>
        <DupeStamp />
      </Show>
    </g>
  );
}

function Stage(props: {
  script: RoundScript;
  layout: ArenaLayout;
  shot: ArenaThrow | null;
  phase: ThrowPhase;
  loop: number;
  motion: boolean;
}): JSX.Element {
  const flight = createMemo<Flight | null>(() =>
    props.shot ? flightFor(props.layout, props.shot) : null,
  );
  const inFlight = (): boolean => props.phase !== "windup";
  const l = (): ArenaLayout => props.layout;
  return (
    <svg
      class="theater-scene scene-arena"
      data-layout={l().kind}
      viewBox={`0 0 ${l().width} ${l().height}`}
      aria-hidden="true"
    >
      <Show
        when={l().kind === "wide"}
        fallback={
          <>
            <ellipse class="arena-mat" cx="210" cy="270" rx="200" ry="232" />
            <path class="arena-line" d="M20 268h380" />
          </>
        }
      >
        <ellipse class="arena-mat" cx="480" cy="250" rx="468" ry="72" />
        <path class="arena-line" d="M480 70v240" />
        <path class="arena-hazard" d="M30 300h120M810 300h120" />
      </Show>
      <g class="arena-banner" transform={`translate(${l().banner.x} ${l().banner.y})`}>
        <rect x="-74" y="-20" width="148" height="40" rx="5" />
        <text class="arena-banner-round" y="-2" text-anchor="middle">
          Round {props.script.round}
        </text>
        <text class="arena-banner-sub" y="13" text-anchor="middle">
          generators vs agents
        </text>
      </g>
      <For each={props.script.generators}>
        {(generator, index) => {
          const p = (): { x: number; y: number } => l().generators[index()]!;
          const s = (): number => l().generatorSize;
          const throwing = (): boolean =>
            props.motion && props.shot?.generator === index() && props.phase === "windup";
          return (
            <>
              <PaperHuman
                persona={generator.persona}
                x={p().x}
                y={p().y}
                size={s()}
                alive
                throwing={throwing()}
                mood={throwing() ? "focus" : "idle"}
                class={props.shot?.generator === index() ? "is-thrower" : ""}
              >
                <PaperHand
                  x={88}
                  y={78}
                  size={26}
                  rotate={-20}
                  fill="var(--skin)"
                  class="pf-throw-hand"
                />
                <PaperHand x={12} y={80} size={24} rotate={20} fill="var(--skin)" />
              </PaperHuman>
              <NameTag
                x={p().x + s() / 2}
                y={p().y + s() * 1.4 + 10}
                text={generator.name}
                max={11}
              />
            </>
          );
        }}
      </For>
      <For each={props.script.agents}>
        {(agent, index) => {
          const p = (): { x: number; y: number } => l().agents[index()]!;
          const s = (): number => l().agentSize;
          return (
            <>
              <PaperDitto
                persona={agent.persona}
                x={p().x}
                y={p().y}
                size={s()}
                mood={agentMood(props.shot, index(), props.phase)}
                alive
                flip={l().kind === "wide"}
              >
                <PaperHand x={2} y={70} size={30} rotate={-14} class="pf-hand-l" />
                <PaperHand x={98} y={70} size={30} rotate={14} class="pf-hand-r" />
              </PaperDitto>
              <NameTag x={p().x + s() / 2} y={p().y + s() + 16} text={agent.name} max={11} />
            </>
          );
        }}
      </For>
      <Show when={props.shot && flight()}>
        <Show when={props.motion} fallback={<RestingCase flight={flight()!} shot={props.shot!} />}>
          <Show when={inFlight()}>
            {/* Keyed on the throw so each case restarts its flight. */}
            <For each={[`${props.loop}:${props.shot!.index}`]}>
              {() => <FlyingCase flight={flight()!} shot={props.shot!} />}
            </For>
          </Show>
        </Show>
      </Show>
    </svg>
  );
}

const OUTCOME_COLUMNS: readonly {
  key: "discriminating" | "trivial" | "unsolved" | "duplicate";
  label: string;
  title: string;
}[] = [
  {
    key: "discriminating",
    label: "Split",
    title: "Discriminating: some agents solved it, others did not",
  },
  { key: "trivial", label: "Easy", title: "Trivial: every agent solved it" },
  { key: "unsolved", label: "Stumped", title: "Unsolved: no agent solved it" },
  { key: "duplicate", label: "Dupes", title: "Duplicate: discarded, never counted" },
];

export function ArenaScene(props: {
  script: RoundScript | null;
  scoredAt: string | null;
  motion: boolean;
  playing: boolean;
  compact: boolean;
}): JSX.Element {
  const replay = createReplay(
    () => props.script,
    () => props.playing && props.motion,
  );
  const layout = createMemo<ArenaLayout | null>(() => {
    const script = props.script;
    if (!script) return null;
    return arenaLayout(
      props.compact ? "tall" : "wide",
      script.generators.length,
      script.agents.length,
    );
  });
  const shot = (): ArenaThrow | null => {
    const script = props.script;
    if (!script) return null;
    if (!props.motion) return tableauThrow(script.throws);
    return script.throws[replay.index()] ?? null;
  };
  const phase = (): ThrowPhase => (props.motion ? replay.phase() : "rest");
  return (
    <Show
      when={props.script && layout()}
      fallback={
        <p class="arena-empty">
          No scored generator round to replay yet. The arena opens once a round finishes scoring.
        </p>
      }
    >
      <div class="arena-stage">
        <Stage
          script={props.script!}
          layout={layout()!}
          shot={shot()}
          phase={phase()}
          loop={replay.loop()}
          motion={props.motion}
        />
        <p class="arena-caption" aria-hidden="true">
          <span class="arena-caption-count">
            {props.script!.throws.length
              ? `Case ${(shot()?.index ?? 0) + 1} of ${props.script!.throws.length} shown`
              : "No cases"}
          </span>
          {shot()?.caption ?? ""}
        </p>
        <p class="sr-only">{roundSummary(props.script!)}</p>
      </div>
      <div class="arena-boards">
        <div class="arena-table-scroll">
          <table class="arena-table" aria-label={`Generators in round ${props.script!.round}`}>
            <caption>
              Generators · round {props.script!.round}
              <Show when={props.scoredAt}>
                <span> · scored {relTime(props.scoredAt)}</span>
              </Show>
            </caption>
            <thead>
              <tr>
                <th scope="col">Generator</th>
                <For each={OUTCOME_COLUMNS}>
                  {(column) => (
                    <th scope="col" class="num" title={column.title}>
                      {column.label}
                    </th>
                  )}
                </For>
                <th scope="col" class="num">
                  Score
                </th>
              </tr>
            </thead>
            <tbody>
              <For each={props.script!.generators}>
                {(generator, index) => (
                  <tr classList={{ "is-current": props.motion && shot()?.generator === index() }}>
                    <th scope="row">
                      <span class="arena-who">
                        <MiniHuman persona={generator.persona} />
                        <Show when={generator.hotkey} fallback={<span>{generator.name}</span>}>
                          <EntityButton
                            kind="miner"
                            id={generator.hotkey}
                            label={generator.name}
                            title="Open the miner profile"
                          />
                        </Show>
                      </span>
                    </th>
                    <For each={OUTCOME_COLUMNS}>
                      {(column) => <td class="num">{generator.counts[column.key]}</td>}
                    </For>
                    <td class="num">{fx(generator.score)}</td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
        <div class="arena-table-scroll">
          <table class="arena-table" aria-label={`Agent panel in round ${props.script!.round}`}>
            <caption>Panel · pairwise standings</caption>
            <thead>
              <tr>
                <th scope="col" class="num">
                  #
                </th>
                <th scope="col">Agent</th>
                <th
                  scope="col"
                  class="num"
                  title="Wins, ties and losses against the other panel agents"
                >
                  W–T–L
                </th>
              </tr>
            </thead>
            <tbody>
              <For each={props.script!.agents}>
                {(agent) => (
                  <tr>
                    <td class="num">{agent.rank}</td>
                    <th scope="row">
                      <span class="arena-who">
                        <MiniDitto persona={agent.persona} />
                        <EntityButton
                          kind="agent"
                          id={agent.agentId}
                          label={agent.name}
                          title="Open the agent card"
                        />
                        <Show when={agent.reference}>
                          <span class="arena-ref">reference</span>
                        </Show>
                      </span>
                    </th>
                    <td class="num">
                      {agent.wins}–{agent.ties}–{agent.losses}
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
      </div>
    </Show>
  );
}
