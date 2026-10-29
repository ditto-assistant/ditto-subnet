// Arena choreography: the timing of one throw, who reacts how, and where
// everyone stands. Pure, so the data → motion mapping is testable without a
// browser; ArenaScene only plays it.
import type { Mood } from "./PaperFigures";
import type { ArenaThrow } from "./round-script";

export type ThrowPhase = "windup" | "flight" | "first" | "second" | "rest";

/** Milliseconds from the start of a throw. A split case bonks one agent at
 * `first` and lands in another's hands at `second`; every other case
 * resolves at `first`. */
export const BEAT_MS = {
  windup: 0,
  flight: 260,
  first: 860,
  second: 1340,
  rest: 1780,
  next: 2150,
  /** Pause on the final tally before the loop starts again. */
  loopHold: 2800,
} as const;

export function throwPhases(outcome: ArenaThrow["outcome"]): { phase: ThrowPhase; at: number }[] {
  const phases: { phase: ThrowPhase; at: number }[] = [
    { phase: "windup", at: BEAT_MS.windup },
    { phase: "flight", at: BEAT_MS.flight },
    { phase: "first", at: BEAT_MS.first },
  ];
  if (outcome === "discriminating") phases.push({ phase: "second", at: BEAT_MS.second });
  phases.push({ phase: "rest", at: BEAT_MS.rest });
  return phases;
}

/** Flight time of the case, in ms: until it lands for good. */
export function flightMs(outcome: ArenaThrow["outcome"]): number {
  return (outcome === "discriminating" ? BEAT_MS.second : BEAT_MS.first) - BEAT_MS.flight;
}

const after = (phase: ThrowPhase, mark: ThrowPhase): boolean => {
  const order: ThrowPhase[] = ["windup", "flight", "first", "second", "rest"];
  return order.indexOf(phase) >= order.indexOf(mark);
};

/** How agent `index` looks at `phase` of throw `shot`. */
export function agentMood(shot: ArenaThrow | null, index: number, phase: ThrowPhase): Mood {
  if (!shot) return "idle";
  const catches = shot.catchers.includes(index);
  const misses = shot.missers.includes(index);
  if (!catches && !misses) return "idle";
  if (!after(phase, "first")) return "focus";
  if (misses) return "bonk";
  // A split case reaches its catcher only after bouncing off the misser.
  if (shot.outcome === "discriminating" && !after(phase, "second")) return "focus";
  return "cheer";
}

export interface Point {
  x: number;
  y: number;
}

export interface ArenaLayout {
  kind: "wide" | "tall";
  width: number;
  height: number;
  generatorSize: number;
  agentSize: number;
  generators: Point[];
  agents: Point[];
  /** Where the round banner hangs. */
  banner: Point;
}

function spread(count: number, from: number, to: number, maxStep: number): number[] {
  if (count <= 1) return [(from + to) / 2];
  const step = Math.min(maxStep, (to - from) / (count - 1));
  const start = (from + to) / 2 - (step * (count - 1)) / 2;
  return Array.from({ length: count }, (_, index) => start + index * step);
}

/** Generators on one side, agents on the other: side by side on a wide
 * stage, top and bottom on a phone. Staggered in two rows so nobody hides
 * behind a neighbour. */
export function arenaLayout(
  kind: "wide" | "tall",
  generators: number,
  agents: number,
): ArenaLayout {
  if (kind === "wide") {
    const gs = 64;
    const as = 70;
    return {
      kind,
      width: 960,
      height: 330,
      generatorSize: gs,
      agentSize: as,
      generators: spread(generators, 40, 300, 66).map((x, i) => ({ x, y: i % 2 ? 158 : 92 })),
      agents: spread(agents, 620, 880, 72).map((x, i) => ({ x, y: i % 2 ? 182 : 116 })),
      banner: { x: 480, y: 34 },
    };
  }
  const gs = 58;
  const as = 64;
  return {
    kind,
    width: 420,
    height: 540,
    generatorSize: gs,
    agentSize: as,
    generators: spread(generators, 24, 336, 64).map((x, i) => ({ x, y: i % 2 ? 92 : 24 })),
    agents: spread(agents, 20, 334, 70).map((x, i) => ({ x, y: i % 2 ? 420 : 350 })),
    banner: { x: 210, y: 266 },
  };
}

/** The throwing hand of a generator (its 100 × 140 box, hand at 88, 78). */
export function handPoint(layout: ArenaLayout, index: number): Point {
  const p = layout.generators[index] ?? { x: 0, y: 0 };
  const s = layout.generatorSize / 100;
  return { x: p.x + 88 * s, y: p.y + 74 * s };
}

/** An agent's forehead (where a case bonks) and raised hands (a catch). */
export function headPoint(layout: ArenaLayout, index: number): Point {
  const p = layout.agents[index] ?? { x: 0, y: 0 };
  const s = layout.agentSize / 100;
  return { x: p.x + 50 * s, y: p.y + 22 * s };
}

export function catchPoint(layout: ArenaLayout, index: number): Point {
  const p = layout.agents[index] ?? { x: 0, y: 0 };
  const s = layout.agentSize / 100;
  return { x: p.x + 50 * s, y: p.y - 6 * s };
}

export interface Flight {
  kind: "single" | "double" | "dupe";
  from: Point;
  /** First landing (the only one for a single flight). */
  to: Point;
  /** Second landing after a bounce (split cases). */
  bounce: Point | null;
  /** Apex heights for each arc, as absolute y. */
  peaks: number[];
  durationMs: number;
}

/** The arc a case flies: trivial into the target's hands, unsolved onto its
 * head, a split case off the misser's head into the catcher's hands, a
 * duplicate halfway across before it is stamped. */
export function flightFor(layout: ArenaLayout, shot: ArenaThrow): Flight {
  const from = handPoint(layout, shot.generator);
  const lift = layout.kind === "wide" ? 90 : 60;
  const apex = (a: Point, b: Point): number => Math.min(a.y, b.y) - lift;
  if (shot.outcome === "duplicate") {
    const target = headPoint(layout, 0);
    const to = { x: (from.x + target.x) / 2, y: (from.y + target.y) / 2 + 20 };
    return {
      kind: "dupe",
      from,
      to,
      bounce: null,
      peaks: [apex(from, to)],
      durationMs: flightMs("duplicate"),
    };
  }
  if (shot.outcome === "discriminating" && shot.missers.length && shot.catchers.length) {
    const to = headPoint(layout, shot.missers[0]!);
    const bounce = catchPoint(layout, shot.catchers[0]!);
    return {
      kind: "double",
      from,
      to,
      bounce,
      peaks: [apex(from, to), Math.min(to.y, bounce.y) - lift * 0.55],
      durationMs: flightMs("discriminating"),
    };
  }
  const target = shot.catchers[0] ?? shot.missers[0] ?? 0;
  const to = shot.catchers.length ? catchPoint(layout, target) : headPoint(layout, target);
  return {
    kind: "single",
    from,
    to,
    bounce: null,
    peaks: [apex(from, to)],
    durationMs: flightMs(shot.outcome),
  };
}

/** Where a case rests in the static (reduced-motion) tableau. */
export function restingPoint(flight: Flight): Point {
  return flight.bounce ?? flight.to;
}

/** The throw the static tableau freezes on: the first split case when the
 * round had one (the moment the competition is about), else the first. */
export function tableauThrow(throws: readonly ArenaThrow[]): ArenaThrow | null {
  return throws.find((shot) => shot.outcome === "discriminating") ?? throws[0] ?? null;
}
