// Slot fan-out behaviors, ported from ditto/tests/api_server/test_dashboard_slots.py
// (#540, written 2026-07-29 and deleted with this file's arrival — it was the
// second Python source suite, missed by the parity inventory).
//
// Why that suite existed, in its own words: the pre-SPA dashboard had no test
// runner, "and a substring cannot tell 'renders two jobs' from 'renders one
// job twice'", so it lifted these five functions out of `index.html` with
// regexes and executed them under node against fixture heartbeats. Under the
// SPA they are ordinary imports from ./fleet, and the four rendering-level
// guards run against the real components instead of grepping source text.
//
// This file is plain .ts, so the two component tests invoke the page/panel as
// functions (Solid components are functions; `render` supplies the root) —
// same contract as the JSX in src/pages/Operations.test.tsx, no JSX syntax.
import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { EntityPanel } from "../EntityPanel";
import { OperationsPage } from "../../pages/OperationsPage";
import { syncFromLocation } from "../../stores/routeStore";
import { installFixtureFetch, loadFixture } from "../../test-fixtures";
import type { OperationsPayload } from "../../types/fleet";
import type { BenchmarkProgress } from "../../types/pipeline";
import {
  anyBenchmarkStage,
  cappedSlotIds,
  fundedSlotCount,
  slotOrdinal,
  validatorAssignmentView,
  validatorSlotIds,
} from "./fleet";
import { fleetStatus } from "../EntityPanel";
import type { FleetEntryExt } from "./fleet";

/** The Python suite's `_benchmark` fixture: one slot's live run. */
function bench(slotId: string, stage: string | null = "running_benchmark"): BenchmarkProgress {
  return {
    slot_id: slotId,
    agent_id: "agent-" + slotId,
    agent_name: "Agent " + slotId,
    bench_version: 7,
    stage,
    completed_checks: 10,
    total_checks: 282,
    percent: 5,
    stalled: false,
  };
}

/** `TestCappedSlots._capped`: the capped set, ordered numerically so the
 * assertion reads like the slot column an operator sees. */
function cappedIds(entry: FleetEntryExt): string[] {
  return Object.keys(cappedSlotIds(entry)).sort((a, b) => slotOrdinal(a) - slotOrdinal(b));
}

// ── TestValidatorSlotIds ────────────────────────────────────────────────────

describe("validatorSlotIds: every slot a validator could show work on", () => {
  it("renders one row per configured slot", () => {
    expect(
      validatorSlotIds({
        configured_slots: 4,
        healthy_slots: ["slot-0", "slot-1", "slot-2", "slot-3"],
        admission: "accepting",
        active_benchmarks: [bench("slot-0"), bench("slot-2")],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-0", "slot-1", "slot-2", "slot-3"]);
  });

  it("makes two concurrent jobs both addressable", () => {
    // The regression the whole slot fan-out change exists for: two jobs, two
    // distinct rows — never one row rendered twice (see also the fleet-table
    // rendering guard at the bottom of this file).
    const slots = validatorSlotIds({
      configured_slots: 2,
      healthy_slots: ["slot-0", "slot-1"],
      admission: "accepting",
      active_benchmarks: [bench("slot-0"), bench("slot-1")],
      assigned_benchmarks: [],
    });
    expect(slots).toEqual(["slot-0", "slot-1"]);
    expect(new Set(slots).size).toBe(2);
  });

  it("still shows an active slot outside the configured range", () => {
    // A job must never be dropped just because the count looks smaller.
    // Synthesising ids from `configured_slots` alone hid exactly this case,
    // which is when an operator most needs to see the work.
    expect(
      validatorSlotIds({
        configured_slots: 1,
        healthy_slots: ["slot-0"],
        admission: "accepting",
        active_benchmarks: [bench("slot-3")],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-0", "slot-3"]);
  });

  it("shows assigned-but-not-yet-active slots", () => {
    expect(
      validatorSlotIds({
        configured_slots: 1,
        healthy_slots: ["slot-0"],
        admission: "accepting",
        active_benchmarks: [],
        assigned_benchmarks: [bench("slot-1")],
      }),
    ).toEqual(["slot-0", "slot-1"]);
  });

  it("orders slots numerically, not lexically", () => {
    expect(
      validatorSlotIds({
        configured_slots: 1,
        healthy_slots: [],
        admission: "accepting",
        active_benchmarks: [bench("slot-2"), bench("slot-1")],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-0", "slot-1", "slot-2"]);
  });

  it("leaves a single-slot validator unchanged", () => {
    expect(
      validatorSlotIds({
        configured_slots: 1,
        healthy_slots: ["slot-0"],
        admission: "accepting",
        active_benchmarks: [bench("slot-0")],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-0"]);
  });

  it("falls back to one slot when the capacity fields are missing", () => {
    expect(validatorSlotIds({})).toEqual(["slot-0"]);
  });

  // The orphaned-slot arm of the same union (#537) is asserted in
  // fleet.test.ts, "keeps an orphaned slot in the slot union".
});

// ── slotOrdinal: the ordering primitive the fan-out sorts on ────────────────
// The Python suite only exercised it through validatorSlotIds; these pin the
// two properties that ordering depends on directly, because a lexical sort
// reads plausibly right until a fleet grows past ten slots.

describe("slotOrdinal: numeric slot order", () => {
  it("compares by ordinal, so slot-10 follows slot-9", () => {
    expect(
      ["slot-10", "slot-2", "slot-9", "slot-0"].sort((a, b) => slotOrdinal(a) - slotOrdinal(b)),
    ).toEqual(["slot-0", "slot-2", "slot-9", "slot-10"]);
    // The lexical answer would have been slot-0, slot-10, slot-2, slot-9.
    expect(slotOrdinal("slot-10")).toBeGreaterThan(slotOrdinal("slot-9"));
    expect(slotOrdinal("slot-007")).toBe(7);
    // Only the leading prefix is stripped; a bare ordinal still parses.
    expect(slotOrdinal("12")).toBe(12);
  });

  it("sorts an id with no ordinal last instead of claiming slot zero", () => {
    // A slot naming scheme this build does not know, or a heartbeat with no
    // slot_id at all, must not sort ahead of slot-0 (which would reorder real
    // work) and must not drop out of the union.
    expect(slotOrdinal("gpu-a")).toBe(Number.MAX_SAFE_INTEGER);
    expect(slotOrdinal(undefined)).toBe(Number.MAX_SAFE_INTEGER);
    expect(slotOrdinal(null)).toBe(Number.MAX_SAFE_INTEGER);
    expect(
      validatorSlotIds({
        configured_slots: 2,
        healthy_slots: ["slot-0", "slot-1", "gpu-a"],
        active_benchmarks: [bench("slot-11"), bench("slot-2")],
      }),
    ).toEqual(["slot-0", "slot-1", "slot-2", "slot-11", "gpu-a"]);
  });
});

// ── TestAnyBenchmarkStage ───────────────────────────────────────────────────

describe("anyBenchmarkStage: any slot's stage is live progress", () => {
  it("counts a stage on a higher slot as progress", () => {
    // Keying off slot zero alone suppressed live progress for slot one: the
    // row fell back to the plain worker-state chip while a benchmark ran.
    expect(
      anyBenchmarkStage({ active_benchmarks: [bench("slot-1")], active_benchmark: null }),
    ).toBe(true);
  });

  it("reports no granular progress for an idle validator", () => {
    expect(anyBenchmarkStage({ active_benchmarks: [], active_benchmark: null })).toBe(false);
  });

  it("still counts a legacy single benchmark", () => {
    expect(anyBenchmarkStage({ active_benchmarks: [], active_benchmark: bench("slot-0") })).toBe(
      true,
    );
  });

  it("needs a stage, not merely a slot entry", () => {
    // Beyond the Python suite: a leased slot that has not reported a stage yet
    // is not progress — the row must keep its worker-state chip rather than
    // render an empty progress line. The scan is over every slot, so a stage
    // on the second entry alone is enough.
    expect(anyBenchmarkStage({ active_benchmarks: [bench("slot-0", null)] })).toBe(false);
    expect(anyBenchmarkStage({ active_benchmark: bench("slot-0", null) })).toBe(false);
    expect(anyBenchmarkStage({ active_benchmarks: [bench("slot-0", null), bench("slot-1")] })).toBe(
      true,
    );
    expect(anyBenchmarkStage({})).toBe(false);
  });
});

// ── TestCappedSlots ─────────────────────────────────────────────────────────
// Advertised capacity above the operator cap must not read as idle. The fleet
// ran eight advertised slots under a cap of six, and the table drew eight rows
// the operator could reasonably read as eight usable slots; two of them were
// never going to receive a ticket.

describe("cappedSlotIds: capacity the operator cap will not fund", () => {
  // "Eight advertised, cap of six: the top two are capped, not idle" is
  // asserted in fleet.test.ts, "caps the leftover healthy slots from the
  // highest ordinal down"; the cases below are the ones it does not cover.

  it("caps nothing on a validator inside the cap", () => {
    expect(
      cappedIds({
        configured_slots: 4,
        allowed_slots: 4,
        healthy_slots: ["slot-0", "slot-1", "slot-2", "slot-3"],
        admission: "accepting",
        active_benchmarks: [],
        assigned_benchmarks: [],
      }),
    ).toEqual([]);
  });

  it("charges running work first and never marks it", () => {
    // Lowering the cap costs new leases only, never one in flight. slot-1 runs
    // and spends one of the two, slot-0 takes the other — so the leftover
    // budget lands on the lowest idle ordinal, not on whichever slot sorts
    // first in the payload. (fleet.test.ts has the smaller 2-slot variant.)
    expect(
      cappedIds({
        configured_slots: 4,
        allowed_slots: 2,
        healthy_slots: ["slot-0", "slot-1", "slot-2", "slot-3"],
        admission: "accepting",
        active_benchmarks: [bench("slot-1")],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-2", "slot-3"]);
  });

  it("never lets leases beyond the cap borrow from idle slots", () => {
    // A cap dropped under live work caps every idle slot, not a negative:
    // two leases against a cap of one leaves a budget of zero, not minus one.
    expect(
      cappedIds({
        configured_slots: 4,
        allowed_slots: 1,
        healthy_slots: ["slot-0", "slot-1", "slot-2", "slot-3"],
        admission: "accepting",
        active_benchmarks: [bench("slot-0"), bench("slot-1")],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-2", "slot-3"]);
  });

  it("leaves an unhealthy slot its own state", () => {
    // Unavailable is about the validator; capped is about the operator. slot-1
    // is unhealthy, so it is not the cap keeping work off it — and it must not
    // spend a slot of the budget either, which is why slot-2 stays idle and
    // only slot-3 is capped.
    expect(
      cappedIds({
        configured_slots: 4,
        allowed_slots: 2,
        healthy_slots: ["slot-0", "slot-2", "slot-3"],
        admission: "accepting",
        active_benchmarks: [],
        assigned_benchmarks: [],
      }),
    ).toEqual(["slot-3"]);
  });

  // "A dashboard served against an older API must not invent a cap" (a payload
  // with no allowed_slots marks nothing) is asserted in fleet.test.ts, "says
  // nothing when the payload predates allowed_slots".
});

// ── TestFundedSlotCount ─────────────────────────────────────────────────────
// The fleet table's numerator: what can take work, not what is advertised.

describe("fundedSlotCount: the cap and health, whichever binds", () => {
  it("binds on health when health is the smaller number", () => {
    // The cap-binds direction (8 healthy, cap 6 → 6) and the missing-field
    // fallback are asserted in fleet.test.ts, "counts funded slots as
    // min(allowed, healthy) with the full tooltip"; this is the other side of
    // the min(), where the validator itself is the constraint.
    expect(
      fundedSlotCount({
        configured_slots: 8,
        allowed_slots: 6,
        healthy_slots: ["slot-0", "slot-1"],
      }),
    ).toBe(2);
    expect(fundedSlotCount({ configured_slots: 4, healthy_slots: ["slot-0", "slot-1"] })).toBe(2);
  });
});

// ── TestDashboardSource: the four guards that were source greps ─────────────
// The Python file could only assert that the monolith's text contained
// `class="fleet-slot"` / `class="stage capped"` / `fundedSlotCount(entry)` and
// that the modal sorted its active slots. Rendering the real components makes
// the same guards observable instead of textual.

const HERE = dirname(fileURLToPath(import.meta.url));
const STYLES = join(HERE, "..", "..", "styles");
const OPERATIONS_CSS = readFileSync(join(STYLES, "pages", "operations.css"), "utf-8");
const WIDGETS_CSS = readFileSync(join(STYLES, "widgets.css"), "utf-8");

const operations = loadFixture<OperationsPayload>("operations");
const validatorRows = operations.validators.validators ?? [];
// Ditto: 8 advertised, 8 healthy, funded 6 — the recorded shape the capped
// state exists for, under the fixture's fleet cap of 6 per validator.
const DITTO = String(
  validatorRows.find((entry) => String(entry.validator_hotkey).startsWith("5HmP9732"))
    ?.validator_hotkey,
);

let restoreFetch: (() => void) | null = null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/operations");
  syncFromLocation();
  restoreFetch = installFixtureFetch();
});

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
  vi.useRealTimers();
});

/** A one-off operations snapshot carrying `validators` (the fixture's
 * slot_policy and generated_at are kept, so the cap tooltip and snapshot age
 * read as they do in production). */
function snapshotWith(validators: FleetEntryExt[]): OperationsPayload {
  return { ...operations, validators: { ...operations.validators, validators } };
}

async function renderFleet(payload: OperationsPayload = operations): Promise<void> {
  restoreFetch?.();
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/public/operations")) {
      return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
    }
    return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
  }) as typeof fetch;
  render(() => OperationsPage());
  await waitFor(() =>
    expect(document.querySelectorAll("#fleet-rows tr[data-entity-id]").length).toBeGreaterThan(0),
  );
}

function workCell(hotkey: string): HTMLElement {
  const cell = document.querySelector(
    `#fleet-rows tr[data-entity-id="${hotkey}"] td.fleet-work-col`,
  );
  if (!cell) throw new Error("no work cell for " + hotkey);
  return cell as HTMLElement;
}

function slotRows(hotkey: string): HTMLElement[] {
  return Array.from(workCell(hotkey).querySelectorAll<HTMLElement>(".fleet-slot"));
}

/** A synthetic validator that lands in the open fleet table. */
function validator(hotkey: string, extra: FleetEntryExt): FleetEntryExt {
  return {
    validator_hotkey: hotkey,
    availability: "available",
    health: "healthy",
    state: "running_benchmark",
    admission: "accepting",
    protocol_version: 18,
    reported_at: "2026-07-31T13:55:00Z",
    seen_at: "2026-07-31T13:55:00Z",
    ...extra,
  };
}

describe("slot fan-out in the fleet table", () => {
  it("summarises inactive slots by default and keeps every exact state in a disclosure", async () => {
    await renderFleet();
    const disclosure = workCell(DITTO).querySelector(
      ".fleet-slot-disclosure",
    ) as HTMLDetailsElement;
    expect(disclosure.open).toBe(false);
    expect(disclosure.querySelector("summary")?.textContent).toBe(
      "8 slots · 0 running · 6 idle · 2 capped",
    );
    const rows = slotRows(DITTO);
    // The disclosure still preserves all eight exact slot states in numeric
    // order; they simply do not make the default table row eight times taller.
    expect(rows.length).toBe(8);
    expect(rows.map((row) => row.querySelector(".fleet-protocol")?.textContent)).toEqual([
      "slot-0",
      "slot-1",
      "slot-2",
      "slot-3",
      "slot-4",
      "slot-5",
      "slot-6",
      "slot-7",
    ]);
    expect(rows.map((row) => row.getAttribute("title"))).toEqual(
      rows.map((row) => row.querySelector(".fleet-protocol")?.textContent),
    );
    expect(rows.every((row) => row.classList.contains("fleet-slot-inactive"))).toBe(true);
    expect(OPERATIONS_CSS).toContain(".fleet-slot {");
    expect(OPERATIONS_CSS).toContain(".fleet-slot-disclosure");
  });

  it("renders a capped slot as its own state — not Idle, not Unavailable", async () => {
    await renderFleet();
    const states = slotRows(DITTO).map((row) => row.querySelector(".stage")?.textContent);
    // Six funded slots read Idle; the two above the cap read Capped. "Idle"
    // would claim usable capacity no ticket can reach, and "Unavailable" would
    // blame the validator for an operator setting.
    expect(states).toEqual(["Idle", "Idle", "Idle", "Idle", "Idle", "Idle", "Capped", "Capped"]);
    expect(states).not.toContain("Unavailable");
    const capped = workCell(DITTO).querySelectorAll("span.stage.capped");
    expect(capped.length).toBe(2);
    // The tooltip names the cap from the snapshot's slot policy, not a literal.
    expect(capped[0]).toHaveAttribute(
      "title",
      "Operator cap: 6 concurrent slots per validator. No ticket is issued here.",
    );
    // Its own tone: dimmer than idle, never an alert.
    expect(WIDGETS_CSS).toContain(".stage.capped {");
  });

  it("reports funded capacity in the row, not advertised or healthy", async () => {
    await renderFleet();
    const protocol = document.querySelector(
      `#fleet-rows tr[data-entity-id="${DITTO}"] td:nth-child(6) .fleet-protocol`,
    );
    // The numerator is what dispatch funds. Ditto is 8 healthy of 8 advertised
    // under a cap of 6, so the old "healthy of advertised" phrasing would have
    // read "8 of 8" — a validator with two unusable slots reported as full.
    expect(protocol?.textContent).toBe("Protocol 18 · 6 of 8 slots · accepting");
    expect(protocol?.textContent).not.toContain("8 of 8");
    expect(protocol?.textContent).not.toContain("8/8");
    // The breakdown behind the number lives in the tooltip (the string itself
    // is asserted in fleet.test.ts via slotCapacityTitle).
    expect(protocol?.getAttribute("title")).toBe(
      "8 advertised · 8 healthy · 6 funded by the operator cap · fleet cap 6 per validator",
    );
  });

  it("never synthesises slot ids from a count, and draws two jobs as two rows", async () => {
    // The old loop built ids as `"slot-" + slotIndex` up to configured_slots,
    // so a job on a higher slot vanished from the table entirely; and a single
    // rendered job could not be told apart from two.
    const overflow = validator("5OverflowSlotValidatorHotkey00000000000000000000", {
      configured_slots: 1,
      healthy_slots: ["slot-0"],
      allowed_slots: 1,
      active_benchmarks: [bench("slot-3")],
    });
    const concurrent = validator("5TwoConcurrentJobsValidatorHotkey0000000000000000", {
      configured_slots: 2,
      healthy_slots: ["slot-0", "slot-1"],
      allowed_slots: 2,
      active_benchmarks: [bench("slot-1"), bench("slot-0")],
    });
    await renderFleet(snapshotWith([overflow, concurrent]));

    const overflowCell = workCell(String(overflow.validator_hotkey));
    const visibleRows = Array.from(overflowCell.querySelectorAll<HTMLElement>(".fleet-slot-line"));
    const foldedRows = Array.from(
      overflowCell.querySelectorAll<HTMLElement>(".fleet-slot-inactive"),
    );
    expect(visibleRows.map((row) => row.querySelector(".fleet-slot-id")?.textContent)).toEqual([
      "slot-3",
    ]);
    expect(foldedRows.map((row) => row.querySelector(".fleet-protocol")?.textContent)).toEqual([
      "slot-0",
    ]);
    // The out-of-range slot is where the work is; it must be the row that
    // stays visible and shows a running benchmark.
    expect(visibleRows[0]?.querySelector("progress, .bench-bar")).toBeTruthy();
    expect(foldedRows[0]?.querySelector("progress, .bench-bar")).toBeNull();

    const cell = workCell(String(concurrent.validator_hotkey));
    // Two distinct jobs, in slot order, each naming its own agent — the exact
    // reading a substring assertion could not make.
    expect(cell.querySelectorAll(".fleet-slot-line").length).toBe(2);
    expect(
      cell.querySelectorAll(".fleet-slot-line progress, .fleet-slot-line .bench-bar").length,
    ).toBe(2);
    expect(
      Array.from(cell.querySelectorAll(".fleet-slot-agent"), (node) => node.getAttribute("title")),
    ).toEqual(["agent-slot-0", "agent-slot-1"]);
  });
});

describe("validator detail modal", () => {
  it("renders every active slot, in slot order", async () => {
    // The modal used to show only the lowest slot, so a second concurrent
    // benchmark was invisible here — the reading an operator opens the modal
    // to get ("does this host have room") was wrong by a whole job.
    const entry = validator("5ModalSlotFanOutValidatorHotkey000000000000000000", {
      configured_slots: 4,
      healthy_slots: ["slot-0", "slot-1", "slot-2", "slot-3"],
      allowed_slots: 4,
      // Deliberately out of order on the wire: the modal sorts by ordinal.
      active_benchmarks: [bench("slot-2"), bench("slot-1")],
    });
    const payload = snapshotWith([entry]);
    render(() =>
      EntityPanel({
        entries: () => [],
        operations: () => payload,
        validatorNames: () => ({}),
        currentBench: () => 7,
        settledView: () => false,
      }),
    );
    history.replaceState(null, "", "/#/operations?validator=" + entry.validator_hotkey);
    syncFromLocation();
    await waitFor(() =>
      expect(document.getElementById("modal")?.classList.contains("open")).toBe(true),
    );

    const stats = document.getElementById("d-stats") as HTMLElement;
    const keys = Array.from(stats.querySelectorAll(".stat-row .k"), (node) => node.textContent);
    // The capacity summary stays (advertised, healthy and funded are three
    // different numbers), and each running slot gets its own row below it.
    expect(keys).toContain("Slots");
    expect(keys.filter((key) => key?.startsWith("slot-"))).toEqual(["slot-1", "slot-2"]);
    expect(stats.textContent).toContain(
      "4 healthy of 4 · 4 funded by the operator cap · accepting",
    );
    expect(stats.querySelectorAll(".benchmark-progress").length).toBe(2);
    expect(
      Array.from(stats.querySelectorAll(".benchmark-agent"), (node) => node.getAttribute("title")),
    ).toEqual(["agent-slot-1", "agent-slot-2"]);
  });
});

// ── The grace window has to be visible to do its job ────────────────────────
// preserveTransientValidatorTelemetry stamps `_telemetry_grace`, but nothing
// read it: a one-poll gap rendered as a red fleet-wide "Mismatch", which is
// the exact reading the grace window exists to prevent (monolith fleetStatus
// 8663–8665, renderValidatorAssignment 8710–8714).
describe("a telemetry gap inside the grace window is not a mismatch", () => {
  const mismatched = {
    validator_hotkey: "hk-grace",
    assignment_state: "assignment_mismatch",
    availability: "online",
    health: "healthy",
  };

  it("reads as delayed telemetry while the grace holds, red once it lapses", () => {
    expect(fleetStatus({ ...mismatched, _telemetry_grace: true })).toEqual([
      "Telemetry delayed",
      "warn",
    ]);
    expect(fleetStatus(mismatched)).toEqual(["Mismatch", "bad"]);
  });

  it("explains the wait instead of naming both sides of a skew", () => {
    const view = validatorAssignmentView({ ...mismatched, _telemetry_grace: true });
    expect(view?.label).toBe("Telemetry delayed");
    expect(view?.tone).toBe("warn");
    expect(view?.lines).toHaveLength(1);
    expect(view?.lines[0]?.heading).toBeUndefined();
    expect(view?.lines[0]?.fallback).toBe(
      "Waiting for the next signed slot update; the last reported progress is retained briefly.",
    );
    // Without the grace flag the skew is named on both sides, as before.
    expect(validatorAssignmentView(mismatched)?.lines.map((l) => l.heading)).toEqual([
      "Platform",
      "Heartbeat",
    ]);
  });
});
