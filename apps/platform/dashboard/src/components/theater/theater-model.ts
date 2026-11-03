// Stage → scene mapping for the pipeline theater.
//
// The theater tells the submission pipeline as one story in three acts:
// a factory builds each agent into its Paper Ditto, airport security screens
// it, and the arena scores it. This module is the pure fold from the public
// operations snapshot (plus the screener feed for live stages) to who stands
// where in each act and which authoritative numbers each act's board shows.
// It invents nothing: an unknown status stands nowhere, an unreported
// screener stage stays on the assembly line rather than being guessed into
// review, and every headline number comes from `status_counts` or from the
// same live-run rows the pipeline board counts.
import type { FleetReport } from "../../types/fleet";
import type { PipelineEntryExt } from "../operations/pipeline";
import {
  activeScreenerFor,
  parkedReading,
  pipelineBoardStage,
  queueRelevantBenchmark,
} from "../operations/pipeline";
import { admissionStepKey, reviewStageIndex, screenerStageLabel } from "../operations/progress";
import type { DittoPersona } from "./persona";
import { agentPersona, personaKey } from "./persona";

export type SceneId = "factory" | "security" | "arena";

/** Where in an act a submission stands. */
export type SceneSpot =
  /** Queued for admission: a blank sheet waiting in the hopper. */
  | "intake"
  /** A screener is fetching, building or booting it (or has not said). */
  | "assembly"
  /** Its bags are on the belt: automated source review. */
  | "xray"
  /** Pulled aside: held for review, or screening needs a manual retry. */
  | "secondary"
  /** Admitted, waiting at the arena gate for a validator. */
  | "gate"
  /** On the arena floor: a validator run is in progress. */
  | "floor"
  /** Scored and live. */
  | "podium";

export const SPOT_SCENE: Record<SceneSpot, SceneId> = {
  intake: "factory",
  assembly: "factory",
  xray: "security",
  secondary: "security",
  gate: "arena",
  floor: "arena",
  podium: "arena",
};

/** Which assembly station a building Ditto stands under: the admission step
 * the screener reports, folded onto the factory's three machines. */
export type Station = "stamp" | "paint" | "dress";

export interface CastMember {
  key: string;
  agentId: string | null;
  name: string;
  avatarUrl: string | null;
  persona: DittoPersona;
  status: string;
  spot: SceneSpot;
  station: Station | null;
  /** Live stage label from the screener or validator, when reported. */
  stageLabel: string | null;
  /** 0–3 inside source review (scan, trace, safety, verdict), 4 once done. */
  reviewStage: number | null;
  /** Live validator progress (0–100) for a run on the floor. */
  percent: number | null;
  /** Parked with no attempts left (the board's "stuck" quick-filter). */
  stuck: boolean;
  /** The board's parked reading, verbatim, when the row is parked. */
  holdLabel: string | null;
  /** Why it was pulled aside, when the API published a reason. */
  reason: string | null;
  submittedAt: string | null;
}

/** The light a Ditto walks out of security under. */
export type SecurityLight = "green" | "amber" | "red" | "pending";

export function securityLight(status: string): SecurityLight {
  switch (status) {
    case "waiting_validator":
    case "below_score_floor":
    case "evaluating":
    case "scored":
    case "live":
      return "green";
    case "under_review":
    case "screening_failed":
    case "quarantined":
      return "amber";
    case "rejected":
    case "banned":
      return "red";
    default:
      return "pending";
  }
}

function screenerStage(
  screeners: FleetReport | null | undefined,
  entry: PipelineEntryExt,
): string | null {
  return activeScreenerFor(screeners, entry.agent_id)?.screening_progress?.stage ?? null;
}

/** The scene spot for one submission, or null when it stands in no act
 * (rejected, retired, not queued: those are tallied, not staged). */
export function sceneSpot(
  entry: PipelineEntryExt,
  stage: string | null,
  activeVersion: number | null,
): SceneSpot | null {
  if (pipelineBoardStage(entry, activeVersion) === "evaluating") return "floor";
  switch (entry.status) {
    case "waiting_screening":
      return "intake";
    case "screening": {
      const step = admissionStepKey(stage);
      return step === "review" || step === "submit" ? "xray" : "assembly";
    }
    case "screening_failed":
    case "under_review":
      return "secondary";
    case "waiting_validator":
    case "below_score_floor":
      return "gate";
    case "scored":
    case "live":
      return "podium";
    default:
      return null;
  }
}

export function assemblyStation(stage: string | null): Station | null {
  const step = admissionStepKey(stage);
  if (step === "fetch") return "stamp";
  if (step === "build") return "paint";
  if (step === "boot") return "dress";
  return null;
}

export function castMember(
  entry: PipelineEntryExt,
  spot: SceneSpot,
  stage: string | null,
  activeVersion: number | null,
  index: number,
): CastMember {
  const key = entry.agent_id ? "a:" + entry.agent_id : "i:" + index;
  const run = (entry.active_benchmarks ?? []).find((progress) =>
    queueRelevantBenchmark(progress, activeVersion),
  );
  const parked = parkedReading(entry);
  return {
    key,
    agentId: entry.agent_id ?? null,
    name: entry.name || "Unnamed agent",
    avatarUrl: entry.avatar_url ?? null,
    persona: agentPersona(personaKey(entry.agent_id, entry.name)),
    status: String(entry.status || ""),
    spot,
    station: spot === "assembly" ? assemblyStation(stage) : null,
    stageLabel:
      spot === "assembly" || spot === "xray"
        ? stage
          ? screenerStageLabel(stage)
          : null
        : run?.stage
          ? run.stage.replace(/_/g, " ")
          : null,
    reviewStage: spot === "xray" ? reviewStageIndex(stage) : null,
    percent: spot === "floor" && run && Number.isFinite(run.percent) ? Number(run.percent) : null,
    stuck: entry.retry_state === "exhausted",
    holdLabel: parked?.label ?? null,
    reason: entry.review_reason || entry.screening_reason || null,
    submittedAt: entry.submitted_at ?? null,
  };
}

export interface TheaterInput {
  entries: readonly PipelineEntryExt[];
  statusCounts: Record<string, number>;
  screeners: FleetReport | null | undefined;
  activeVersion: number | null;
}

export interface FactoryAct {
  /** Authoritative: waiting_screening. */
  queued: number;
  /** Authoritative: screening (building and in review together). */
  screening: number;
  /** In screening and still on the assembly line, from the live rows. */
  building: number;
  intake: CastMember[];
  stations: CastMember[];
}

export interface SecurityAct {
  /** In screening with a screener reporting source review, from live rows. */
  reviewing: number;
  /** Authoritative: under_review. */
  held: number;
  /** Authoritative: screening_failed (manual retry required). */
  retry: number;
  /** Authoritative: rejected, all time. */
  rejected: number;
  scanning: CastMember[];
  secondary: CastMember[];
}

export interface ArenaAct {
  /** Authoritative: waiting_validator + below_score_floor. */
  waiting: number;
  /** Waiting rows with no attempts left, from the live rows. */
  stuck: number;
  /** Live validator runs (the same rows the board's Scoring lane counts). */
  scoring: number;
  /** Authoritative: scored + live. */
  scored: number;
  gate: CastMember[];
  floor: CastMember[];
}

export interface Theater {
  factory: FactoryAct;
  security: SecurityAct;
  arena: ArenaAct;
  /** The newest submission in the snapshot: when an act has nobody live,
   * it replays this agent's own trip through it. */
  featured: CastMember | null;
  /** Every staged submission, for the text alternative. */
  cast: CastMember[];
}

const count = (counts: Record<string, number>, ...statuses: string[]): number =>
  statuses.reduce((total, status) => total + (Number(counts[status]) || 0), 0);

const newestFirst = (a: CastMember, b: CastMember): number =>
  new Date(b.submittedAt || 0).getTime() - new Date(a.submittedAt || 0).getTime();

/** How many figures each spot draws; counts beyond these are on the boards. */
export const SPOT_CAPACITY: Record<SceneSpot, number> = {
  intake: 4,
  assembly: 3,
  xray: 2,
  secondary: 3,
  gate: 4,
  floor: 3,
  podium: 0,
};

export function buildTheater(input: TheaterInput): Theater {
  const cast: CastMember[] = [];
  input.entries.forEach((entry, index) => {
    const stage = screenerStage(input.screeners, entry);
    const spot = sceneSpot(entry, stage, input.activeVersion);
    if (spot) cast.push(castMember(entry, spot, stage, input.activeVersion, index));
  });
  const at = (spot: SceneSpot): CastMember[] => cast.filter((member) => member.spot === spot);
  const shown = (spot: SceneSpot, members: CastMember[]): CastMember[] =>
    members.slice(0, SPOT_CAPACITY[spot]);

  const assembly = at("assembly");
  const xray = at("xray");
  const gate = at("gate");
  const floor = at("floor");
  // Parked rows sit on the bench at the gate; the ones still advancing stand
  // in line ahead of them.
  const gateOrder = [...gate.filter((m) => !m.stuck), ...gate.filter((m) => m.stuck)];

  const runs = input.entries.reduce(
    (total, entry) =>
      total +
      (entry.active_benchmarks ?? []).filter((progress) =>
        queueRelevantBenchmark(progress, input.activeVersion),
      ).length,
    0,
  );

  let featured: CastMember | null = null;
  input.entries.forEach((entry, index) => {
    const stage = screenerStage(input.screeners, entry);
    const spot = sceneSpot(entry, stage, input.activeVersion);
    const candidate = castMember(entry, spot ?? "podium", stage, input.activeVersion, index);
    if (!entry.submitted_at) return;
    if (!featured || newestFirst(candidate, featured) < 0) featured = candidate;
  });

  return {
    factory: {
      queued: count(input.statusCounts, "waiting_screening"),
      screening: count(input.statusCounts, "screening"),
      building: assembly.length,
      intake: shown("intake", at("intake")),
      stations: shown("assembly", assembly),
    },
    security: {
      reviewing: xray.length,
      held: count(input.statusCounts, "under_review"),
      retry: count(input.statusCounts, "screening_failed"),
      rejected: count(input.statusCounts, "rejected"),
      scanning: shown("xray", xray),
      secondary: shown("secondary", at("secondary").sort(newestFirst)),
    },
    arena: {
      waiting: count(input.statusCounts, "waiting_validator", "below_score_floor"),
      stuck: gate.filter((member) => member.stuck).length,
      scoring: runs,
      scored: count(input.statusCounts, "scored", "live"),
      gate: shown("gate", gateOrder),
      floor: shown("floor", floor),
    },
    featured,
    cast,
  };
}

/** One plain sentence per act, for the theater's text alternative. */
export function theaterSummary(theater: Theater): {
  factory: string;
  security: string;
  arena: string;
} {
  const f = theater.factory;
  const s = theater.security;
  const a = theater.arena;
  const names = (members: CastMember[]): string =>
    members.length ? ": " + members.map((member) => member.name).join(", ") : "";
  return {
    factory:
      `Build: ${f.queued} queued for admission, ${f.screening} in screening` +
      (f.building ? `, ${f.building} on the assembly line${names(f.stations)}` : "") +
      ".",
    security:
      `Screening: ${s.reviewing} in source review${names(s.scanning)}, ` +
      `${s.held} held for review, ${s.retry} awaiting a manual retry, ` +
      `${s.rejected} rejected in total.`,
    arena:
      `Scoring: ${a.waiting} waiting for validators` +
      (a.stuck ? ` (${a.stuck} on hold)` : "") +
      `, ${a.scoring} validator ${a.scoring === 1 ? "run" : "runs"} in progress${names(a.floor)}, ` +
      `${a.scored} scored and live.`,
  };
}
