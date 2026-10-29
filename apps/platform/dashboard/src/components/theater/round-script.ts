// Generator round → arena script.
//
// The arena replays the newest scored generator round as a short loop:
// Paper people (the round's counted generators) throw cases at Paper Dittos
// (the round's panel agents). Every number comes from the public round
// detail (`/generator/rounds/{n}`): each generator's case outcome counts and
// each agent's pairwise standing. The public payload does not say which
// agent solved which case, so a split ("discriminating") case is dramatized
// from the standings: the agent that catches it is drawn in proportion to
// its pairwise score and the one it bonks in proportion to the remainder.
// The theater says so in its text alternative; the scoreboard beside the
// stage shows the real per-generator and per-agent totals, never the sample.
import { shortKey } from "../../lib/format";
import type {
  GeneratorRoundColumn,
  GeneratorRoundDetail,
  GeneratorRoundStanding,
} from "../generators/generator-data";
import type { DittoPersona, HumanPersona } from "./persona";
import { agentPersona, hashSeed, humanPersona, personaKey, seededRandom } from "./persona";

export const OUTCOMES = ["discriminating", "trivial", "unsolved", "duplicate"] as const;
export type CaseOutcome = (typeof OUTCOMES)[number];

/** What each kind of case looks like in flight. */
export const OUTCOME_PROP: Record<CaseOutcome, "maze" | "sticky" | "knot" | "copy"> = {
  discriminating: "maze",
  trivial: "sticky",
  unsolved: "knot",
  duplicate: "copy",
};

export interface ArenaGenerator {
  key: string;
  columnId: string;
  name: string;
  hotkey: string | null;
  avatarUrl: string | null;
  isDefault: boolean;
  persona: HumanPersona;
  score: number;
  counts: Record<CaseOutcome, number>;
  cases: number;
}

export interface ArenaAgent {
  key: string;
  agentId: string;
  name: string;
  avatarUrl: string | null;
  persona: DittoPersona;
  rank: number;
  wins: number;
  ties: number;
  losses: number;
  pairwise: number;
  reference: boolean;
}

export interface ArenaThrow {
  index: number;
  generator: number;
  outcome: CaseOutcome;
  prop: (typeof OUTCOME_PROP)[CaseOutcome];
  /** Agents that catch (solve) it. */
  catchers: number[];
  /** Agents it bonks (no solve). */
  missers: number[];
  caption: string;
}

export interface RoundScript {
  round: number;
  generators: ArenaGenerator[];
  agents: ArenaAgent[];
  throws: ArenaThrow[];
  /** Real totals across the counted generators. */
  totals: Record<CaseOutcome, number> & { cases: number };
}

export interface AgentLabel {
  name?: string | null;
  avatarUrl?: string | null;
}

export interface ScriptOptions {
  maxThrows?: number;
  maxAgents?: number;
  maxGenerators?: number;
}

/** Split `total` slots across `weights` by largest remainder; ties go to the
 * earlier index so the split is deterministic. Zero weights get nothing. */
export function apportion(weights: readonly number[], total: number): number[] {
  const clean = weights.map((weight) => (Number.isFinite(weight) && weight > 0 ? weight : 0));
  const sum = clean.reduce((acc, weight) => acc + weight, 0);
  if (sum <= 0 || total <= 0) return clean.map(() => 0);
  const exact = clean.map((weight) => (weight / sum) * total);
  const floors = exact.map(Math.floor);
  let left = total - floors.reduce((acc, value) => acc + value, 0);
  const order = exact
    .map((value, index) => ({ index, rem: value - Math.floor(value) }))
    .filter((item) => clean[item.index]! > 0)
    .sort((a, b) => b.rem - a.rem || a.index - b.index);
  for (const item of order) {
    if (left <= 0) break;
    floors[item.index]! += 1;
    left -= 1;
  }
  return floors;
}

/** Spread each bucket's quota evenly through one sequence: at every step the
 * bucket furthest behind its share goes next. `[2, 1]` → `[0, 1, 0]`. */
export function smoothSequence(quotas: readonly number[]): number[] {
  const total = quotas.reduce((acc, quota) => acc + Math.max(0, quota), 0);
  const used = quotas.map(() => 0);
  const out: number[] = [];
  for (let step = 0; step < total; step++) {
    let best = -1;
    let bestLag = Infinity;
    quotas.forEach((quota, index) => {
      if (used[index]! >= quota) return;
      const lag = (used[index]! + 0.5) / quota;
      if (lag < bestLag) {
        bestLag = lag;
        best = index;
      }
    });
    used[best]! += 1;
    out.push(best);
  }
  return out;
}

function weightedPick(random: () => number, weights: readonly number[]): number {
  const sum = weights.reduce((acc, weight) => acc + weight, 0);
  if (sum <= 0) return -1;
  let roll = random() * sum;
  for (let index = 0; index < weights.length; index++) {
    roll -= weights[index]!;
    if (roll < 0) return index;
  }
  return weights.length - 1;
}

function generatorName(column: GeneratorRoundColumn): string {
  const named = (column as GeneratorRoundColumn & { name?: string | null }).name;
  if (named) return named;
  if (column.public_default) return "Public default";
  return column.miner_hotkey ? shortKey(column.miner_hotkey) : column.column_id;
}

function countsOf(column: GeneratorRoundColumn): Record<CaseOutcome, number> {
  const n = (value: number | null | undefined): number =>
    Number.isFinite(value) && Number(value) > 0 ? Number(value) : 0;
  return {
    discriminating: n(column.discriminating),
    trivial: n(column.trivial),
    unsolved: n(column.unsolved),
    duplicate: n(column.duplicate),
  };
}

export function arenaGenerators(
  columns: readonly GeneratorRoundColumn[],
  max = 6,
): ArenaGenerator[] {
  return columns
    .filter((column) => column.counted)
    .slice()
    .sort((a, b) => (a.column_id < b.column_id ? -1 : a.column_id > b.column_id ? 1 : 0))
    .slice(0, max)
    .map((column) => {
      const counts = countsOf(column);
      const named = column as GeneratorRoundColumn & { avatar_url?: string | null };
      return {
        key: "g:" + column.submission_id,
        columnId: column.column_id,
        name: generatorName(column),
        hotkey: column.miner_hotkey,
        avatarUrl: named.avatar_url ?? null,
        isDefault: column.public_default,
        persona: humanPersona(column.submission_id),
        score: Number.isFinite(column.score) ? column.score : 0,
        counts,
        cases: OUTCOMES.reduce((acc, outcome) => acc + counts[outcome], 0),
      };
    });
}

export function arenaAgents(
  standings: readonly GeneratorRoundStanding[],
  label: (agentId: string) => AgentLabel | null | undefined,
  max = 5,
): ArenaAgent[] {
  return standings
    .slice()
    .sort((a, b) => a.rank - b.rank || (a.agent_id < b.agent_id ? -1 : 1))
    .slice(0, max)
    .map((standing) => {
      const known = label(standing.agent_id);
      return {
        key: "a:" + standing.agent_id,
        agentId: standing.agent_id,
        name: known?.name || "Agent " + standing.agent_id.slice(0, 8),
        avatarUrl: known?.avatarUrl ?? null,
        persona: agentPersona(personaKey(standing.agent_id)),
        rank: standing.rank,
        wins: standing.wins,
        ties: standing.ties,
        losses: standing.losses,
        pairwise: Number.isFinite(standing.pairwise_score) ? standing.pairwise_score : 0,
        reference: Boolean(standing.reference),
      };
    });
}

function caption(
  outcome: CaseOutcome,
  thrower: string,
  catchers: string[],
  missers: string[],
): string {
  switch (outcome) {
    case "trivial":
      return `${thrower} lobs an easy one. Every agent solves it, so it separates nobody.`;
    case "unsolved":
      return `${thrower}'s case stumps the whole panel${missers[0] ? `, ${missers[0]} included` : ""}.`;
    case "duplicate":
      return `${thrower} throws a repeat. Duplicates are stamped and never count.`;
    default:
      return `${thrower} sends a labyrinth case: ${catchers[0] ?? "one agent"} solves it, ${missers[0] ?? "another"} does not.`;
  }
}

/** The whole replay: who throws what at whom, in order. */
export function buildRoundScript(
  detail: GeneratorRoundDetail,
  label: (agentId: string) => AgentLabel | null | undefined = () => null,
  options: ScriptOptions = {},
): RoundScript {
  const maxThrows = options.maxThrows ?? 24;
  const generators = arenaGenerators(detail.columns, options.maxGenerators ?? 6);
  const agents = arenaAgents(detail.standings, label, options.maxAgents ?? 5);
  const totals = { discriminating: 0, trivial: 0, unsolved: 0, duplicate: 0, cases: 0 };
  for (const generator of generators) {
    for (const outcome of OUTCOMES) totals[outcome] += generator.counts[outcome];
    totals.cases += generator.cases;
  }

  const round = detail.summary.round_index;
  const throws: ArenaThrow[] = [];
  if (agents.length === 0 || totals.cases === 0) {
    return { round, generators, agents, throws, totals };
  }

  // Each generator throws in proportion to the cases it produced, and its
  // own throws keep the proportions of its outcome buckets.
  const budget = Math.min(maxThrows, totals.cases);
  const perGenerator = apportion(
    generators.map((generator) => generator.cases),
    budget,
  );
  const queues = generators.map((generator, index) => {
    const quotas = apportion(
      OUTCOMES.map((outcome) => generator.counts[outcome]),
      perGenerator[index]!,
    );
    return smoothSequence(quotas).map((bucket) => OUTCOMES[bucket]!);
  });
  const order = smoothSequence(perGenerator);
  const cursor = generators.map(() => 0);
  const random = seededRandom(hashSeed("round:" + round));
  const n = agents.length;

  order.forEach((generatorIndex, index) => {
    const outcome = queues[generatorIndex]![cursor[generatorIndex]!++]!;
    let catchers: number[] = [];
    let missers: number[] = [];
    const target = index % n;
    if (outcome === "trivial") catchers = [target];
    else if (outcome === "unsolved") missers = [target];
    else if (outcome === "discriminating") {
      const catcher = weightedPick(
        random,
        agents.map((agent) => Math.max(0, agent.pairwise) + 0.15),
      );
      const missWeights = agents.map((agent, i) =>
        i === catcher ? 0 : Math.max(0, 1 - agent.pairwise) + 0.15,
      );
      const misser = n > 1 ? weightedPick(random, missWeights) : -1;
      catchers = [catcher];
      missers = misser >= 0 ? [misser] : [];
    }
    throws.push({
      index,
      generator: generatorIndex,
      outcome,
      prop: OUTCOME_PROP[outcome],
      catchers,
      missers,
      caption: caption(
        outcome,
        generators[generatorIndex]!.name,
        catchers.map((i) => agents[i]!.name),
        missers.map((i) => agents[i]!.name),
      ),
    });
  });
  return { round, generators, agents, throws, totals };
}

/** One sentence for the arena's text alternative. */
export function roundSummary(script: RoundScript): string {
  const t = script.totals;
  const leader = script.agents[0];
  const sharpest = script.generators
    .filter((generator) => generator.counts.discriminating > 0)
    .sort((a, b) => b.counts.discriminating - a.counts.discriminating)[0];
  return (
    `Generator round ${script.round}: ${script.generators.length} generators produced ${t.cases} cases ` +
    `(${t.discriminating} separated the agents, ${t.trivial} every agent solved, ` +
    `${t.unsolved} no agent solved, ${t.duplicate} duplicates). ` +
    (sharpest
      ? `${sharpest.name} wrote the most separating cases (${sharpest.counts.discriminating}). `
      : "") +
    (leader
      ? `${leader.name} led the panel at ${leader.wins} wins, ${leader.ties} ties, ${leader.losses} losses. `
      : "") +
    "Per-agent results on individual cases are not public, so the replay draws who solves a separating case from the panel standings."
  );
}
