// Pure fleet helpers: slot funding (#540 — funds, not advertised capacity),
// evicted-but-running slots (#537 — never idle), the inoperative fold rule
// (#511/#514 neighbours), the snapshot-read offline window, and the
// stake-then-hotkey ordering.
import { describe, expect, it } from "vitest";

import {
  benchGateLabel,
  preserveTransientValidatorTelemetry,
  cappedSlotIds,
  fleetLedgerCounts,
  fleetStatusFor,
  fleetWindowLabel,
  fleetWork,
  fundedSlotCount,
  groupScreenerHosts,
  isInoperativeFleetEntry,
  offlineAwareFleetStatusFor,
  orphanedSlotView,
  slotCapacityTitle,
  sortFleetEntries,
  screenerHostId,
  screenerWorkerLabel,
  validatorAssignmentView,
  validatorSlotIds,
} from "./fleet";
import type { FleetEntryExt, ValidatorTelemetryCache } from "./fleet";

describe("screener worker host grouping", () => {
  const workers: FleetEntryExt[] = [
    { screener_hotkey: "shared", instance_id: "subnet-screener-1-worker-2" },
    { screener_hotkey: "shared", instance_id: "subnet-screener-1-worker-1" },
    { screener_hotkey: "shared", instance_id: "subnet-screener-2-worker-1" },
    { screener_hotkey: "shared", instance_id: "legacy-node" },
  ];

  it("uses the node prefix rather than the fleet-wide hotkey as the host boundary", () => {
    expect(
      groupScreenerHosts(workers).map((group) => [group.hostId, group.workers.length]),
    ).toEqual([
      ["subnet-screener-1", 2],
      ["subnet-screener-2", 1],
      ["legacy-node", 1],
    ]);
    expect(groupScreenerHosts(workers)[0]?.workers.map((worker) => worker.instance_id)).toEqual([
      "subnet-screener-1-worker-1",
      "subnet-screener-1-worker-2",
    ]);
  });

  it("shortens only a recognized local worker suffix", () => {
    expect(screenerHostId(workers[1]!)).toBe("subnet-screener-1");
    expect(screenerWorkerLabel(workers[1]!, "subnet-screener-1")).toBe("Worker 1");
    expect(screenerWorkerLabel(workers[3]!, "legacy-node")).toBe("legacy-node");
  });
});

describe("slots dispatch funds, not advertised capacity (#540)", () => {
  const entry: FleetEntryExt = {
    validator_hotkey: "5V",
    configured_slots: 8,
    healthy_slots: ["slot-0", "slot-1", "slot-2", "slot-3", "slot-4", "slot-5", "slot-6", "slot-7"],
    allowed_slots: 6,
  };

  it("caps the leftover healthy slots from the highest ordinal down", () => {
    expect(Object.keys(cappedSlotIds(entry)).sort()).toEqual(["slot-6", "slot-7"]);
  });

  it("charges running slots against the cap before idle ones", () => {
    const busy: FleetEntryExt = {
      ...entry,
      allowed_slots: 1,
      configured_slots: 2,
      healthy_slots: ["slot-0", "slot-1"],
      active_benchmarks: [{ slot_id: "slot-0", stage: "running_benchmark" }],
    };
    // Live work is never marked: lowering the cap costs new leases only.
    expect(Object.keys(cappedSlotIds(busy))).toEqual(["slot-1"]);
  });

  it("leaves unhealthy slots alone — the cap is not what idles them", () => {
    const partly: FleetEntryExt = {
      ...entry,
      allowed_slots: 0,
      configured_slots: 2,
      healthy_slots: ["slot-1"],
    };
    expect(Object.keys(cappedSlotIds(partly))).toEqual(["slot-1"]);
  });

  it("says nothing when the payload predates allowed_slots", () => {
    expect(cappedSlotIds({ ...entry, allowed_slots: undefined })).toEqual({});
  });

  it("counts funded slots as min(allowed, healthy) with the full tooltip", () => {
    expect(fundedSlotCount(entry)).toBe(6);
    expect(fundedSlotCount({ ...entry, allowed_slots: undefined })).toBe(8);
    expect(slotCapacityTitle(entry, { max_concurrent_slots: 6 })).toBe(
      "8 advertised · 8 healthy · 6 funded by the operator cap · fleet cap 6 per validator",
    );
  });
});

describe("evicted-but-running slots are never idle (#537)", () => {
  it("keeps an orphaned slot in the slot union", () => {
    // Omitting it is what let a host still executing a benchmark render as a
    // full row of idle slots.
    const ids = validatorSlotIds({
      configured_slots: 2,
      orphaned_slots: [{ slot_id: "slot-5", state: "still_running" }],
    });
    expect(ids).toEqual(["slot-0", "slot-1", "slot-5"]);
  });

  it("distinguishes the validator's signed claim from honest ignorance", () => {
    const still = orphanedSlotView({
      slot_id: "slot-5",
      state: "still_running",
      orphaned_for_seconds: 7200,
      agent_id: "agent-still",
      agent_name: "StillRunning",
    });
    expect(still.label).toBe("Evicted · still running · 2h");
    expect(still.detail).toContain("refused with a 409");
    expect(still.detail).toContain("This slot is not free.");

    const unknown = orphanedSlotView({
      slot_id: "slot-5",
      state: "evicted",
      orphaned_for_seconds: 7200,
      protocol_version: 15,
      reason: "protocol omits quiet slots",
    });
    expect(unknown.label).toBe("Evicted · state unknown · 2h");
    expect(unknown.detail).toContain("Heartbeat protocol 15");
    expect(unknown.detail).toContain("Do not count this slot as headroom.");
  });
});

describe("inoperative fold rule and status precedence (#511/#514)", () => {
  it("folds exactly offline hosts and obsolete builds", () => {
    expect(isInoperativeFleetEntry({ availability: "offline" })).toBe(true);
    expect(isInoperativeFleetEntry({ bench_serviceability: "software_obsolete" })).toBe(true);
    // A CURRENT validator with a broken scorer is a live incident, not an
    // obsolete build: it stays visible (never `!== "serving"`).
    expect(
      isInoperativeFleetEntry({ availability: "available", scorer_liveness: "not_serving" }),
    ).toBe(false);
    expect(
      isInoperativeFleetEntry({
        availability: "available",
        bench_serviceability: "scorer_unverified",
      }),
    ).toBe(false);
  });

  it("orders the badges: Obsolete build > Scorer down > bench gate", () => {
    expect(
      fleetStatusFor(
        { bench_serviceability: "software_obsolete", scorer_liveness: "not_serving" },
        7,
      ),
    ).toEqual(["Obsolete build", "bad"]);
    expect(
      fleetStatusFor(
        { bench_serviceability: "scorer_unverified", scorer_liveness: "not_serving" },
        7,
      ),
    ).toEqual(["Scorer down", "bad"]);
    expect(fleetStatusFor({ bench_serviceability: "scorer_unverified" }, 7)).toEqual([
      "No bench v7",
      "bad",
    ]);
  });

  it("reads the gate version from the snapshot, falling back honestly", () => {
    expect(benchGateLabel(8)).toBe("No bench v8");
    expect(benchGateLabel(null)).toBe("Bench unsupported");
    expect(fleetStatusFor({ bench_serviceability: "scorer_unverified" }, null)).toEqual([
      "Bench unsupported",
      "bad",
    ]);
  });

  it("names an offline node offline unless a real fault outranks it", () => {
    expect(offlineAwareFleetStatusFor({ availability: "offline", health: "healthy" }, 7)).toEqual([
      "Offline",
      "bad",
    ]);
    expect(
      offlineAwareFleetStatusFor({ availability: "offline", scorer_liveness: "not_serving" }, 7),
    ).toEqual(["Scorer down", "bad"]);
  });
});

describe("fleet ledger", () => {
  it("keeps an explicit pause visible, then counts faults before liveness", () => {
    const counts = fleetLedgerCounts([
      { health: "critical", availability: "stale" },
      { health: "critical", availability: "available", issuance_paused: true },
      { availability: "offline" },
      { health: "healthy" },
      { assignment_state: "assignment_mismatch", health: "healthy" },
      {},
    ]);
    expect(counts).toEqual({
      healthy: 1,
      critical: 1,
      warning: 1,
      stale: 0,
      offline: 1,
      paused: 1,
      unknown: 1,
    });
  });

  it("shows paused as the fleet verdict even when health also needs attention", () => {
    expect(
      fleetStatusFor({ availability: "available", health: "critical", issuance_paused: true }, 7),
    ).toEqual(["Paused", "paused"]);
  });
});

describe("the offline window comes from the snapshot", () => {
  it("formats seconds instead of restating 15 minutes", () => {
    expect(fleetWindowLabel(900)).toBe("15m");
    expect(fleetWindowLabel(5400)).toBe("1h 30m");
    expect(fleetWindowLabel(7200)).toBe("2h");
    expect(fleetWindowLabel(0)).toBe("the offline window");
    expect(fleetWindowLabel(undefined)).toBe("the offline window");
  });
});

describe("stake-weight ordering (row 26)", () => {
  const stakes = { A: 10, B: 20 };
  const entries: FleetEntryExt[] = [
    { validator_hotkey: "C" },
    { validator_hotkey: "A" },
    { validator_hotkey: "B" },
    { validator_hotkey: "D" },
  ];

  it("sorts validators by stake desc, then hotkey, weightless last", () => {
    const sorted = sortFleetEntries(entries, "validator", stakes).map((e) => e.validator_hotkey);
    expect(sorted).toEqual(["B", "A", "C", "D"]);
  });

  it("leaves the screener fleet in feed order", () => {
    const sorted = sortFleetEntries(entries, "screener", stakes).map((e) => e.validator_hotkey);
    expect(sorted).toEqual(["C", "A", "B", "D"]);
  });
});

describe("platform/heartbeat assignment skew (row 18)", () => {
  it("names both sides of a mismatch — Platform and Heartbeat", () => {
    const view = validatorAssignmentView({
      assignment_state: "assignment_mismatch",
      assigned_agent_id: "assigned-agent-id",
      assigned_agent_name: "PlatformPick",
      reported_agent_id: "reported-agent-id",
    });
    expect(view?.label).toBe("Assignment mismatch");
    expect(view?.tone).toBe("bad");
    expect(view?.lines.map((line) => line.heading)).toEqual(["Platform", "Heartbeat"]);
    expect(view?.lines[0]?.agent).toEqual({
      id: "assigned-agent-id",
      label: "PlatformPick · assigned",
    });
    expect(view?.lines[1]?.agent).toEqual({ id: "reported-agent-id", label: "Agent · reported" });
  });

  it("states the missing side honestly instead of dropping the line", () => {
    const view = validatorAssignmentView({ assignment_state: "assignment_mismatch" });
    expect(view?.lines[0]?.fallback).toBe("No active assignment");
    expect(view?.lines[1]?.fallback).toBe("No active agent");
  });

  it("keeps assigning a normal hand-off and a stale heartbeat a warning", () => {
    const assigning = validatorAssignmentView({
      assignment_state: "assigning",
      assigned_agent_id: "assigned-agent-id",
    });
    expect(assigning?.label).toBe("Assigning");
    expect(assigning?.tone).toBe("progress");
    expect(assigning?.lines[0]?.suffix).toBe(" · handing off");

    const stale = validatorAssignmentView({ assignment_state: "heartbeat_stale" });
    expect(stale?.label).toBe("Heartbeat stale");
    expect(stale?.tone).toBe("warn");
    expect(stale?.lines[0]?.fallback).toBe("Unknown");

    expect(validatorAssignmentView({ assignment_state: null })).toBeNull();
  });
});

describe("fleet work vocabulary", () => {
  it("labels worker states with role-aware fallbacks", () => {
    expect(fleetWork("running_benchmark", "validator")).toEqual(["Running benchmark", "progress"]);
    expect(fleetWork("idle", "validator")).toEqual(["Idle", "good"]);
    expect(fleetWork(undefined, "screener")).toEqual(["Waiting for work", ""]);
    expect(fleetWork(undefined, "validator")).toEqual(["Unknown", ""]);
  });
});

// ── Weekend drift: transient validator telemetry grace (monolith 3155–3164,
// preserveTransientValidatorTelemetry 8458–8532; Python guard
// test_transient_validator_telemetry_uses_a_bounded_grace) ──────────────────
describe("transient validator telemetry uses a bounded grace", () => {
  const T0 = 1_000_000;
  const run = (progress: object, timeline: Array<{ at: number; slot: object | null }>) => {
    // A fresh cache per scenario: the module default would leak state
    // between tests exactly the way the monolith's page-lifetime cache is
    // supposed to persist between polls.
    const cache: ValidatorTelemetryCache = Object.create(null) as never;
    let report: { validators: Array<Record<string, unknown>> } | null = null;
    for (const step of timeline) {
      report = {
        validators: [
          {
            validator_hotkey: "hk-1",
            active_benchmarks: step.slot === null ? [] : [step.slot],
            assigned_benchmarks: [progress],
          },
        ],
      };
      preserveTransientValidatorTelemetry(report, T0 + step.at, cache);
    }
    return report as unknown as {
      validators: Array<{
        active_benchmarks: Array<Record<string, unknown>>;
        _telemetry_grace?: boolean;
      }>;
    };
  };
  const signed = { agent_id: "agent-a", slot_id: "s1", stage: "running_benchmark", percent: 40 };
  const empty = { agent_id: "agent-a", slot_id: "s1" };

  it("preserves the last signed slot progress through a short gap", () => {
    const report = run(signed, [
      { at: 0, slot: signed },
      { at: 8_000, slot: empty },
    ]);
    const [progress] = report.validators[0]!.active_benchmarks;
    expect(progress!.percent).toBe(40);
    expect(progress!._telemetry_delayed).toBe(true);
    expect(report.validators[0]!._telemetry_grace).toBe(true);
  });

  it("turns red once the grace expires — persistent mismatch is not papered over", () => {
    const report = run(signed, [
      { at: 0, slot: signed },
      { at: 25_000, slot: empty },
    ]);
    const [progress] = report.validators[0]!.active_benchmarks;
    expect(progress!._telemetry_delayed).toBeUndefined();
    expect(progress!.percent).toBeUndefined();
    expect(report.validators[0]!._telemetry_grace).toBe(false);
  });

  it("never lends one agent's progress to a different assignment", () => {
    const other = { agent_id: "agent-b", slot_id: "s1" };
    const report = run(other, [
      { at: 0, slot: signed },
      { at: 5_000, slot: other },
    ]);
    const [progress] = report.validators[0]!.active_benchmarks;
    expect(progress!._telemetry_delayed).toBeUndefined();
    expect(progress!.agent_id).toBe("agent-b");
  });

  it("projects a still-assigned slot that vanished from active back into view", () => {
    const report = run(signed, [
      { at: 0, slot: signed },
      { at: 6_000, slot: null },
    ]);
    const [progress] = report.validators[0]!.active_benchmarks;
    expect(progress!.percent).toBe(40);
    expect(progress!._telemetry_delayed).toBe(true);
  });

  it("evicts cache for validators absent from the report", () => {
    const cache: ValidatorTelemetryCache = Object.create(null) as never;
    preserveTransientValidatorTelemetry(
      { validators: [{ validator_hotkey: "hk-1", active_benchmarks: [signed] }] },
      T0,
      cache,
    );
    expect(cache["hk-1"]).toBeDefined();
    preserveTransientValidatorTelemetry(
      { validators: [{ validator_hotkey: "hk-2", active_benchmarks: [] }] },
      T0 + 1_000,
      cache,
    );
    expect(cache["hk-1"]).toBeUndefined();
  });
});
