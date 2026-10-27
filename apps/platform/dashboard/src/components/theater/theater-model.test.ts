import { describe, expect, it } from "vitest";

import type { FleetReport } from "../../types/fleet";
import type { PipelineEntryExt } from "../operations/pipeline";
import { agentPersona } from "./persona";
import {
  SPOT_CAPACITY,
  SPOT_SCENE,
  assemblyStation,
  buildTheater,
  sceneSpot,
  securityLight,
  theaterSummary,
} from "./theater-model";

const entry = (over: Partial<PipelineEntryExt>): PipelineEntryExt => ({
  agent_id: "00000000-0000-4000-8000-000000000001",
  name: "vesper",
  status: "scored",
  submitted_at: "2026-10-27T00:00:00Z",
  ...over,
});

const screenerAt = (agentId: string, stage: string): FleetReport =>
  ({
    screeners: [{ active_agent_id: agentId, screening_progress: { stage } }],
  }) as unknown as FleetReport;

describe("stage → scene mapping", () => {
  it("stages every lifecycle status in its act", () => {
    const cases: [string, string | null, string | null][] = [
      ["waiting_screening", null, "intake"],
      ["screening", null, "assembly"],
      ["screening", "downloading", "assembly"],
      ["screening", "building", "assembly"],
      ["screening", "health_check", "assembly"],
      ["screening", "source_review_35", "xray"],
      ["screening", "submitting", "xray"],
      ["screening_failed", null, "secondary"],
      ["under_review", null, "secondary"],
      ["waiting_validator", null, "gate"],
      ["below_score_floor", null, "gate"],
      ["scored", null, "podium"],
      ["live", null, "podium"],
      ["rejected", null, null],
      ["retired", null, null],
      ["not_queued", null, null],
    ];
    for (const [status, stage, spot] of cases) {
      expect(sceneSpot(entry({ status }), stage, 13), `${status} @ ${stage}`).toBe(spot);
    }
  });

  it("puts any submission with a live validator run on the arena floor", () => {
    const run = { bench_version: 13, percent: 42, stage: "running_cases" };
    expect(
      sceneSpot(entry({ status: "waiting_validator", active_benchmarks: [run] }), null, 13),
    ).toBe("floor");
    // A scored agent's continual retest is live work too.
    expect(sceneSpot(entry({ status: "scored", active_benchmarks: [run] }), null, 13)).toBe(
      "floor",
    );
    // A run for an older benchmark is not this queue's work.
    expect(
      sceneSpot(
        entry({ status: "scored", active_benchmarks: [{ ...run, bench_version: 12 }] }),
        null,
        13,
      ),
    ).toBe("podium");
  });

  it("maps spots onto the three acts", () => {
    expect(SPOT_SCENE.intake).toBe("factory");
    expect(SPOT_SCENE.assembly).toBe("factory");
    expect(SPOT_SCENE.xray).toBe("security");
    expect(SPOT_SCENE.secondary).toBe("security");
    expect(SPOT_SCENE.gate).toBe("arena");
    expect(SPOT_SCENE.floor).toBe("arena");
    expect(SPOT_SCENE.podium).toBe("arena");
  });

  it("folds admission steps onto the factory's machines", () => {
    expect(assemblyStation("validating")).toBe("stamp");
    expect(assemblyStation("building")).toBe("paint");
    expect(assemblyStation("starting")).toBe("dress");
    expect(assemblyStation(null)).toBeNull();
    expect(assemblyStation("mystery")).toBeNull();
  });

  it("lights security by the real outcome", () => {
    expect(securityLight("scored")).toBe("green");
    expect(securityLight("waiting_validator")).toBe("green");
    expect(securityLight("under_review")).toBe("amber");
    expect(securityLight("screening_failed")).toBe("amber");
    expect(securityLight("rejected")).toBe("red");
    expect(securityLight("screening")).toBe("pending");
    expect(securityLight("")).toBe("pending");
  });
});

describe("buildTheater", () => {
  it("takes headline numbers from status_counts, not from the window", () => {
    const theater = buildTheater({
      entries: [entry({ status: "under_review" })],
      statusCounts: {
        waiting_screening: 2,
        screening: 1,
        under_review: 20,
        screening_failed: 4,
        rejected: 1703,
        waiting_validator: 3,
        below_score_floor: 1,
        scored: 1900,
        live: 30,
      },
      screeners: null,
      activeVersion: 13,
    });
    expect(theater.factory).toMatchObject({ queued: 2, screening: 1, building: 0 });
    expect(theater.security).toMatchObject({ held: 20, retry: 4, rejected: 1703, reviewing: 0 });
    expect(theater.arena).toMatchObject({ waiting: 4, scored: 1930, scoring: 0 });
  });

  it("casts live screening work by the screener's stage", () => {
    const building = entry({ agent_id: "a-build", name: "builder", status: "screening" });
    const reviewing = entry({ agent_id: "a-review", name: "reviewer", status: "screening" });
    const screeners = {
      screeners: [
        { active_agent_id: "a-build", screening_progress: { stage: "building" } },
        { active_agent_id: "a-review", screening_progress: { stage: "source_review_85" } },
      ],
    } as unknown as FleetReport;
    const theater = buildTheater({
      entries: [building, reviewing],
      statusCounts: { screening: 2 },
      screeners,
      activeVersion: 13,
    });
    expect(theater.factory.stations.map((m) => [m.name, m.station, m.stageLabel])).toEqual([
      ["builder", "paint", "Building image"],
    ]);
    expect(theater.security.scanning.map((m) => [m.name, m.reviewStage])).toEqual([
      ["reviewer", 2],
    ]);
    expect(theater.security.reviewing).toBe(1);
  });

  it("keeps an unreported stage on the assembly line instead of guessing review", () => {
    const theater = buildTheater({
      entries: [entry({ agent_id: "x", status: "screening" })],
      statusCounts: { screening: 1 },
      screeners: screenerAt("someone-else", "source_review_10"),
      activeVersion: 13,
    });
    expect(theater.factory.stations).toHaveLength(1);
    expect(theater.factory.stations[0]!.station).toBeNull();
    expect(theater.security.scanning).toHaveLength(0);
  });

  it("benches parked rows behind the ones still advancing at the gate", () => {
    const theater = buildTheater({
      entries: [
        entry({
          agent_id: "p",
          name: "parked",
          status: "waiting_validator",
          retry_state: "exhausted",
        }),
        entry({ agent_id: "m", name: "moving", status: "waiting_validator" }),
      ],
      statusCounts: { waiting_validator: 2 },
      screeners: null,
      activeVersion: 13,
    });
    expect(theater.arena.gate.map((m) => m.name)).toEqual(["moving", "parked"]);
    expect(theater.arena.stuck).toBe(1);
    expect(theater.arena.gate[1]!.holdLabel).toBe("On hold · needs operator review");
  });

  it("counts live validator runs the way the board's Scoring lane does", () => {
    const run = (percent: number) => ({ bench_version: 13, percent, stage: "running_cases" });
    const theater = buildTheater({
      entries: [entry({ agent_id: "r", status: "scored", active_benchmarks: [run(10), run(60)] })],
      statusCounts: { scored: 1 },
      screeners: null,
      activeVersion: 13,
    });
    expect(theater.arena.scoring).toBe(2);
    expect(theater.arena.floor[0]).toMatchObject({ percent: 10, stageLabel: "running cases" });
  });

  it("caps the figures each spot draws", () => {
    const many = Array.from({ length: 9 }, (_, i) =>
      entry({ agent_id: "q" + i, status: "waiting_screening" }),
    );
    const theater = buildTheater({
      entries: many,
      statusCounts: { waiting_screening: 9 },
      screeners: null,
      activeVersion: 13,
    });
    expect(theater.factory.intake).toHaveLength(SPOT_CAPACITY.intake);
    expect(theater.factory.queued).toBe(9);
  });

  it("features the newest submission and keeps its persona across acts", () => {
    const old = entry({ agent_id: "old", name: "old", submitted_at: "2026-10-01T00:00:00Z" });
    const fresh = entry({
      agent_id: "fresh",
      name: "fresh",
      status: "under_review",
      submitted_at: "2026-10-26T00:00:00Z",
    });
    const theater = buildTheater({
      entries: [old, fresh],
      statusCounts: {},
      screeners: null,
      activeVersion: 13,
    });
    expect(theater.featured?.name).toBe("fresh");
    expect(theater.featured?.persona).toEqual(agentPersona("fresh"));
    expect(theater.security.secondary[0]?.persona).toEqual(theater.featured?.persona);
  });

  it("summarizes each act in a sentence", () => {
    const theater = buildTheater({
      entries: [
        entry({
          agent_id: "w",
          name: "waiter",
          status: "waiting_validator",
          retry_state: "exhausted",
        }),
      ],
      statusCounts: { waiting_validator: 1, scored: 5, rejected: 2, under_review: 1 },
      screeners: null,
      activeVersion: 13,
    });
    const summary = theaterSummary(theater);
    expect(summary.factory).toBe("Build: 0 queued for admission, 0 in screening.");
    expect(summary.security).toBe(
      "Screening: 0 in source review, 1 held for review, 0 awaiting a manual retry, 2 rejected in total.",
    );
    expect(summary.arena).toBe(
      "Scoring: 1 waiting for validators (1 on hold), 0 validator runs in progress, 5 scored and live.",
    );
  });
});
