import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { syncFromLocation } from "../stores/routeStore";
import { refreshAllEndpoints } from "../data/useEndpoint";
import { installFixtureFetch, loadFixture } from "../test-fixtures";
import type { OperationsPayload } from "../types/fleet";
import { OperationsPage } from "./OperationsPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const OPERATIONS_CSS = readFileSync(join(HERE, "..", "styles", "pages", "operations.css"), "utf-8");

const operations = loadFixture<OperationsPayload>("operations");
const validatorRows = operations.validators.validators ?? [];
const hotkeyOf = (prefix: string): string =>
  String(
    validatorRows.find((v) => String(v.validator_hotkey).startsWith(prefix))?.validator_hotkey,
  );

const DITTO = hotkeyOf("5HmP9732");
const TAO = hotkeyOf("5FU3YKmv");
const YUMA = hotkeyOf("5CqJAjSj");
const OBSOLETE = hotkeyOf("5HKpbkeL");

let restoreFetch: (() => void) | null = null;
let fetchSpy: ReturnType<typeof vi.spyOn> | null = null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/operations");
  syncFromLocation();
  restoreFetch = installFixtureFetch();
  fetchSpy = vi.spyOn(globalThis, "fetch");
});

afterEach(() => {
  cleanup();
  fetchSpy?.mockRestore();
  fetchSpy = null;
  restoreFetch?.();
  restoreFetch = null;
  vi.useRealTimers();
});

function fetchedPaths(): string[] {
  return (fetchSpy?.mock.calls ?? []).map((call: unknown[]) => String(call[0]));
}

async function renderPage(): Promise<void> {
  render(() => <OperationsPage />);
  await waitFor(() =>
    expect(document.querySelector("#fleet-rows tr[data-entity-id]")).toBeTruthy(),
  );
}

function text(id: string): string {
  return document.getElementById(id)?.textContent ?? "";
}

// ── Row 15: test_includes_accessible_fleet_status ───────────────────────────
// Guards the fleet health table (headings, screener toggle, offline/retired
// split) and the removal of privacy-leaking / hardcoded-threshold copy.
describe("accessible fleet status (row 15)", () => {
  it("renders the fleet table with its headings from the three feeds", async () => {
    await renderPage();
    expect(fetchedPaths().some((u) => u.includes("/public/operations"))).toBe(true);
    expect(fetchedPaths().some((u) => u.includes("/public/validator-names"))).toBe(true);
    expect(fetchedPaths().some((u) => u.includes("/public/screeners"))).toBe(true);

    // One heading owns this page: the h1. The tabpanel is named by its tab and
    // the table by its own aria-label, so neither head restates the surface.
    // A reporting fleet states no counts, no retired total and no snapshot age
    // either — the header pill reads the same generated_at this line did.
    expect(document.querySelector(".operations-head h2")).toBeNull();
    expect(document.querySelector(".fleet-table-head h2")).toBeNull();
    expect(text("fleet-summary")).toBe("");
    expect(text("operations-snapshot")).toBe("");

    const headings = Array.from(
      document.querySelectorAll("#fleet-table thead th"),
      (th) => th.textContent,
    );
    // Three columns, not ten: identity + verdict, live work, host. First
    // seen, heartbeat protocol and container counts moved to the drill-down;
    // status and heartbeat age fold into the identity cell.
    expect(headings).toEqual(["Validator", "Current work", "Host"]);
    expect(headings).not.toContain("Status");
    expect(headings).not.toContain("First seen");
    expect(headings).not.toContain("Containers");
    expect(document.getElementById("fleet-rows")).toBeTruthy();
    expect(document.getElementById("fleet-retired-rows")).toBeTruthy();
  });

  it("uses accessible tabs and swaps the fleet panel through them", async () => {
    await renderPage();
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Validators",
      "Screeners",
      "Targon builds",
    ]);
    expect(document.getElementById("operations-tab-validators")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(text("fleet-node-heading")).toBe("Validator");

    fireEvent.click(document.getElementById("operations-tab-screeners") as HTMLButtonElement);
    await waitFor(() => expect(text("fleet-node-heading")).toBe("Screener"));
    expect(document.getElementById("operations-tab-screeners")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.getElementById("fleet-table")).toHaveAttribute(
      "aria-label",
      "Screener fleet health",
    );
    expect(text("fleet-retired-title")).toBe("Recently offline");
    expect(text("fleet-retired-note")).toContain("Heartbeat history remains visible for 24 hours");

    const screenersTab = document.getElementById("operations-tab-screeners") as HTMLButtonElement;
    fireEvent.keyDown(screenersTab, { key: "ArrowRight" });
    expect(document.getElementById("operations-tab-builds")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.activeElement).toBe(document.getElementById("operations-tab-builds"));
    expect(document.getElementById("fleet-table")).toBeNull();

    fireEvent.keyDown(document.activeElement as HTMLButtonElement, { key: "Home" });
    expect(document.getElementById("operations-tab-validators")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("raises faults in the head and stays silent about sound nodes", async () => {
    await renderPage();
    expect(text("fleet-count-critical")).toBe("2");
    expect(text("fleet-count-offline")).toBe("1");
    expect(document.querySelector(".fleet-ledger")).toHaveAttribute("aria-label", "Fleet faults");
    // Healthy and operator-paused answer for nothing, and an empty bucket is
    // not a fault — each row already spells its own verdict beside its name.
    expect(document.getElementById("fleet-count-healthy")).toBeNull();
    expect(document.getElementById("fleet-count-paused")).toBeNull();
    expect(document.getElementById("fleet-count-unknown")).toBeNull();
    expect(document.getElementById("fleet-count-stale")).toBeNull();
    expect(document.querySelector(".fleet-ledger-row.critical")?.getAttribute("title")).toContain(
      "the platform leases it no work",
    );
  });

  it("renders an operator-paused validator as paused while live work drains", async () => {
    const hotkey = "5PausedValidatorHotkey000000000000000000000000000";
    const paused = {
      ...operations,
      validators: {
        ...operations.validators,
        reported_count: 1,
        online_count: 1,
        validators: [
          {
            validator_hotkey: hotkey,
            availability: "available",
            issuance_paused: true,
            health: "critical",
            state: "running_benchmark",
            assignment_state: "synchronized",
            configured_slots: 2,
            allowed_slots: 0,
            healthy_slots: ["slot-0", "slot-1"],
            admission: "accepting",
            protocol_version: 18,
            software_version: "0.63.2",
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
            active_benchmarks: [
              {
                slot_id: "slot-0",
                stage: "running_benchmark",
                percent: 47,
                completed_checks: 132,
                total_checks: 281,
                bench_version: 7,
                started_at: "2026-07-31T13:00:00Z",
                agent_id: "agent-draining",
                agent_name: "Draining",
              },
            ],
          },
        ],
      },
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(paused), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;

    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelector(`#fleet-rows tr[data-entity-id="${hotkey}"]`)).toBeTruthy(),
    );

    const row = document.querySelector(`#fleet-rows tr[data-entity-id="${hotkey}"]`);
    const status = row?.querySelector(".fleet-node-status");
    expect(status?.textContent).toBe("Paused");
    expect(status?.classList.contains("paused")).toBe(true);
    expect(row?.querySelector(".fleet-node-dot")?.classList.contains("paused")).toBe(true);
    // An operator pause outranks the failing health underneath it, and a pause
    // is not a fault: the head raises nothing for this fleet.
    expect(document.querySelector(".fleet-ledger")).toBeNull();
    expect(row?.querySelector("td.fleet-host-cell .fleet-protocol")?.textContent).toContain(
      "0 of 2 slots",
    );
    const line = row?.querySelector(".fleet-slot-line");
    expect(line?.querySelector(".fleet-slot-pct")?.textContent).toBe("47%");
    expect(line?.querySelector(".fleet-slot-count")?.textContent).toBe("132/281");
    expect(row?.querySelector(".stage.capped")?.textContent).toBe("Capped");
  });

  it("states every cell-level status once, on one rail above the work", async () => {
    // Three chip families used to sit at three different depths of the work
    // cell: the worker state as a direct child, "No active work" a level in
    // from inside the slot list, and the updater notice below the ledger.
    const hotkey = "5IdleDrainingValidatorHotkey000000000000000000000";
    const idle = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            validator_hotkey: hotkey,
            availability: "available",
            health: "healthy",
            state: "polling",
            admission: "accepting",
            configured_slots: 2,
            allowed_slots: 2,
            healthy_slots: ["slot-0", "slot-1"],
            protocol_version: 23,
            software_version: "0.78.7",
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
            active_benchmarks: [],
            assigned_benchmarks: [],
            stack: { mode: "managed" },
            updater_status: {
              enabled: true,
              channel: "compat-2",
              state: "draining",
              transaction_phase: "prepared",
              current_version: "0.78.7",
              candidate_descriptor: null,
              candidate_version: "0.78.8",
              failed_candidate_count: 0,
              retry_after: null,
              suppressed: false,
              last_success_at: null,
              last_failure_at: null,
              last_failure_reason: null,
              observed_at: 1_775_132_100,
            },
          },
        ],
      },
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      if (String(input).includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(idle), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() => expect(document.querySelector(".fleet-work-status")).toBeTruthy());

    const cell = document.querySelector("td.fleet-work-col") as HTMLElement;
    const rails = cell.querySelectorAll(".fleet-work-status");
    expect(rails.length).toBe(1);
    expect([...rails[0]!.children].map((chip) => chip.textContent)).toEqual([
      "Polling",
      "No active work",
    ]);
    expect(cell.querySelector(".fleet-slot-overview")?.textContent).not.toContain("No active work");
    // The state block runs rail → updater notice → slot ledger, so a drain is
    // read before the empty slots it explains.
    expect([...cell.children].map((child) => child.className)).toEqual([
      "fleet-work-status",
      "fleet-updater-notice",
      "fleet-slot-overview",
    ]);
    expect(cell.querySelector(".fleet-updater-notice .stage")?.textContent).toBe("Safe drain");
  });

  it("never leaks the removed privacy note / allowlist / threshold copy", async () => {
    await renderPage();
    const body = document.body.textContent ?? "";
    expect(body).not.toContain("allowlisted");
    expect(document.querySelector(".privacy-note")).toBeNull();
    expect(document.querySelector(".fleet-health-note")).toBeNull();
    // Host load is one three-bar chart, not three columns; only memory>=90 /
    // disk>=95 warn (no CPU rule). Container counts left for the drill-down.
    const dittoRow = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`);
    const bars = Array.from(dittoRow?.querySelectorAll(".fleet-resource") ?? []);
    expect(bars.length).toBe(3);
    expect(bars.map((bar) => bar.querySelector(".fleet-resource-label")?.textContent)).toEqual([
      "CPU",
      "MEM",
      "DISK",
    ]);
    expect(bars.every((bar) => !bar.classList.contains("warn"))).toBe(true);
    expect(dittoRow?.querySelector(".fleet-container-health")).toBeNull();
  });
});

// ── Row 16: test_inoperative_fleet_nodes_fold_into_the_collapsible ──────────
// Validator last-reports are never pruned, so dead hosts must fold into a
// collapsible (one offline rule for both fleets); the offline window is read
// from the snapshot, never restated in copy; folded validators keep their
// badge, drill-down and deep link; the closed summary names every fold reason
// (a ledger count with no visible row is how a broken validator went
// invisible before).
describe("inoperative fold (row 16)", () => {
  it("folds inoperative validators with a summary naming every reason", async () => {
    await renderPage();
    const retired = document.getElementById("fleet-retired") as HTMLDetailsElement;
    expect(retired.hidden).toBe(false);
    expect(text("fleet-retired-title")).toBe("Inoperative validators");
    expect(text("fleet-retired-summary")).toBe("3 validators · 2 offline · 1 obsolete build");
    expect(document.querySelectorAll("#fleet-retired-rows tr").length).toBe(3);
    expect(document.querySelector(`#fleet-rows tr[data-entity-id="${TAO}"]`)).toBeNull();
  });

  it("reads the offline window from the snapshot instead of restating 15 minutes", async () => {
    await renderPage();
    expect(text("fleet-retired-note")).toContain("No heartbeat for over 15m");
    expect(text("fleet-retired-note")).toContain("cannot serve bench v7");
    expect(text("fleet-retired-note")).not.toContain("15 minutes");
  });

  it("keeps a folded validator's badge and entity identity", async () => {
    await renderPage();
    const tao = document.querySelector(`#fleet-retired-rows tr[data-entity-id="${TAO}"]`);
    expect(tao).toBeTruthy();
    expect(tao).toHaveAttribute("data-entity-kind", "validator");
    // The reason survives the fold: a dead scorer is not flattened to
    // "offline" (offlineAwareFleetStatus only renames non-bad statuses).
    expect(tao?.querySelector(".fleet-node-status")?.textContent).toBe("Scorer down");
    const yuma = document.querySelector(`#fleet-retired-rows tr[data-entity-id="${YUMA}"]`);
    expect(yuma?.querySelector(".fleet-node-status")?.textContent).toBe("Offline");
    expect(yuma?.querySelector(".fleet-node-status")?.classList.contains("bad")).toBe(true);
  });

  it("unfolds and highlights a deep-linked inoperative validator", async () => {
    await renderPage();
    const retired = document.getElementById("fleet-retired") as HTMLDetailsElement;
    expect(retired.open).toBe(false);
    history.replaceState(null, "", "/#/operations?validator=" + TAO);
    syncFromLocation();
    await waitFor(() => {
      const row = document.querySelector(`#fleet-retired-rows tr[data-entity-id="${TAO}"]`);
      expect(row?.classList.contains("entity-target")).toBe(true);
      expect(row).toHaveAttribute("aria-current", "true");
    });
    expect(retired.open).toBe(true);
  });
});

// ── Row 17: test_a_validator_that_cannot_serve_the_scored_bench_is_gated ────
// Obsolete-build validators fold away ("Healthy · Idle" beside the working
// fleet was a fiction) but a CURRENT validator with a broken scorer stays
// visible — hiding it would repeat #511. Badge precedence: Obsolete build >
// Scorer down > bench gate; the bench version comes from the snapshot, never
// a literal.
describe("bench serviceability gate (row 17)", () => {
  it("folds an obsolete build even while its host is available", async () => {
    await renderPage();
    const row = document.querySelector(`#fleet-retired-rows tr[data-entity-id="${OBSOLETE}"]`);
    expect(row).toBeTruthy();
    expect(row?.querySelector(".fleet-node-status")?.textContent).toBe("Obsolete build");
    expect(document.querySelector(`#fleet-rows tr[data-entity-id="${OBSOLETE}"]`)).toBeNull();
  });

  it("keeps a current validator with a broken scorer in the open table", async () => {
    // Synthetic: available + serving-obsolete only in the scorer — the fold
    // rule is offline||software_obsolete, never `!== "serving"`.
    const scorerDown = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            validator_hotkey: "5LiveScorerDownValidatorHotkey00000000000000000",
            availability: "available",
            health: "critical",
            state: "idle",
            bench_serviceability: "serving",
            scorer_liveness: "not_serving",
            configured_slots: 1,
            healthy_slots: ["slot-0"],
            protocol_version: 18,
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
          },
        ],
      },
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(scorerDown), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelectorAll("#fleet-rows tr[data-entity-id]").length).toBe(1),
    );
    const row = document.querySelector("#fleet-rows tr[data-entity-id]");
    expect(row?.querySelector(".fleet-node-status")?.textContent).toBe("Scorer down");
    expect((document.getElementById("fleet-retired") as HTMLDetailsElement).hidden).toBe(true);
  });

  it("names the bench gate from the snapshot version, never a literal", async () => {
    await renderPage();
    expect(text("fleet-retired-note")).toContain("cannot serve bench v7");
    expect(text("fleet-retired-note")).not.toContain("No bench v7,");
  });
});

// ── Row 18: test_operations_panels_share_one_snapshot_and_show_skew ─────────
// All operations panels consume exactly one /public/operations fetch (no
// per-panel refetches), the bench badge never max()-promotes versions
// (lib/bench-state owns that contract), and platform/heartbeat assignment
// skew is surfaced.
describe("one shared snapshot + skew (row 18)", () => {
  it("fetches /public/operations exactly once for every panel", async () => {
    await renderPage();
    const opsCalls = fetchedPaths().filter((u) => u.includes("/public/operations"));
    expect(opsCalls.length).toBe(1);
    expect(fetchedPaths().some((u) => u.includes("/public/validators"))).toBe(false);
    expect(fetchedPaths().some((u) => u.includes("/public/activity?page=1&limit=200"))).toBe(false);
  });

  it("keeps the snapshot note mounted and mute while the feed is sound", async () => {
    await renderPage();
    // The note's live region has to exist before it has something to announce.
    // A reconciled snapshot is not that: its age is the header pill's own
    // number, and its history pointer belongs to the page that shows history,
    // where Pipeline.test.tsx asserts the full stamp.
    const note = document.getElementById("operations-snapshot");
    expect(note).toBeTruthy();
    expect(note).toHaveAttribute("aria-live", "polite");
    expect(note?.textContent).toBe("");
  });

  it("states absence when the snapshot fetch fails", async () => {
    restoreFetch?.();
    globalThis.fetch = (() => Promise.resolve(new Response("{}", { status: 500 }))) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(text("operations-snapshot")).toBe("Shared operations snapshot unavailable"),
    );
    expect(text("fleet-summary")).toBe("Validator status unavailable");
    expect(document.querySelector(".fleet-ledger")).toBeNull();
    expect(document.querySelector("#fleet-rows .empty-msg")?.textContent).toBe(
      "Validator status is temporarily unavailable.",
    );
  });

  it("surfaces platform/heartbeat assignment skew in the status column", async () => {
    const skewed = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            validator_hotkey: "5MismatchValidatorHotkey000000000000000000000000",
            availability: "available",
            health: "healthy",
            state: "running_benchmark",
            assignment_state: "assignment_mismatch",
            assigned_agent_id: "assigned-agent-id",
            assigned_agent_name: "PlatformPick",
            reported_agent_id: "reported-agent-id",
            configured_slots: 1,
            healthy_slots: ["slot-0"],
            protocol_version: 18,
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
          },
          {
            validator_hotkey: "5StaleHeartbeatValidatorHotkey000000000000000000",
            availability: "available",
            health: "healthy",
            state: "polling",
            assignment_state: "heartbeat_stale",
            assigned_agent_id: "assigned-agent-id",
            configured_slots: 1,
            healthy_slots: ["slot-0"],
            protocol_version: 18,
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
          },
        ],
      },
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(skewed), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelectorAll("#fleet-rows tr[data-entity-id]").length).toBe(2),
    );
    const stages = Array.from(
      document.querySelectorAll("#fleet-rows .fleet-node-status"),
      (el) => el.textContent,
    );
    expect(stages).toEqual(["Mismatch", "Heartbeat stale"]);
    // A mismatch is a fault and reaches the head; a merely-stale heartbeat is
    // not, so that node counts healthy and stays out of it (updateFleetLedger
    // 8845–8859 has no heartbeat_stale bucket).
    expect(text("fleet-count-warning")).toBe("1");
    expect(document.querySelectorAll(".fleet-ledger-row").length).toBe(1);
  });
});

// ── Row 22: test_includes_accessible_benchmark_progress ─────────────────────
// Live benchmark/screening progress: stage labels, version chips, rescore
// state, <progress> accessibility, reduced-motion / forced-colors / mobile
// media queries, and per-second elapsed timers. The unit half (labels,
// determinate/stalled/indeterminate bars, ticker) lives in
// components/operations/progress.test.tsx.
describe("accessible benchmark progress (row 22)", () => {
  it("keeps the reduced-motion, forced-colors and mobile rules in the page stylesheet", () => {
    expect(OPERATIONS_CSS).toContain("@media (prefers-reduced-motion: reduce)");
    expect(OPERATIONS_CSS).toContain("@media (forced-colors: active)");
    expect(OPERATIONS_CSS).toContain("@media (max-width: 720px)");
    expect(OPERATIONS_CSS).toContain(".fleet-work-col");
    expect(OPERATIONS_CSS).toContain("bench-indeterminate");
  });

  it("renders live benchmark progress in the fleet work cell with a version chip", async () => {
    const working = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            validator_hotkey: "5WorkingValidatorHotkey0000000000000000000000000",
            availability: "available",
            health: "healthy",
            state: "running_benchmark",
            configured_slots: 1,
            healthy_slots: ["slot-0"],
            protocol_version: 18,
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
            active_benchmarks: [
              {
                slot_id: "slot-0",
                stage: "running_benchmark",
                percent: 47,
                completed_checks: 132,
                total_checks: 281,
                bench_version: 7,
                started_at: "2026-07-31T13:00:00Z",
                agent_id: "agent-under-test",
                agent_name: "UnderTest",
              },
            ],
          },
        ],
      },
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(working), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelector("td.fleet-work-col .fleet-slot-line")).toBeTruthy(),
    );
    const cell = document.querySelector("td.fleet-work-col") as HTMLElement;
    const line = cell.querySelector(".fleet-slot-line") as HTMLElement;
    expect(line.querySelector(".benchmark-version-chip")?.textContent).toBe("v7");
    expect(line.querySelector(".benchmark-version-chip")).toHaveAttribute("title", "Bench v7");
    const bar = line.querySelector("progress") as HTMLProgressElement;
    expect(bar).toHaveAttribute("max", "100");
    expect(bar).toHaveAttribute("value", "47");
    expect(bar.getAttribute("aria-label")).toContain("Running benchmark");
    expect(bar.getAttribute("aria-label")).toContain("132 of 281 checks");
    expect(line.getAttribute("title")).toContain("Running benchmark");
    expect(line.querySelector(".fleet-slot-pct")?.textContent).toBe("47%");
    expect(line.querySelector(".fleet-slot-count")?.textContent).toBe("132/281");
    expect(line.querySelector(".fleet-slot-note")?.textContent).toBe("");
    expect(line.querySelector(".fleet-slot-agent")?.textContent).toBe("UnderTest");
    expect(line.querySelector(".fleet-slot-agent")).toHaveAttribute("title", "agent-under-test");
    expect(line.querySelector(".fleet-slot-id")?.textContent).toBe("0");
    expect(line).toHaveAttribute("data-slot", "slot-0");
    const time = line.querySelector(".fleet-slot-elapsed");
    expect(time).toHaveAttribute("data-started-at", "2026-07-31T13:00:00Z");
    expect(time?.textContent).toBe("1h 0m 0s");
    expect(line.querySelector('.fleet-slot-agent [data-entity-link="agent"]')).toBeTruthy();
  });

  it("shows that a managed update is safely draining active runs", async () => {
    const hotkey = "5ManagedDrainValidatorHotkey0000000000000000000000";
    const candidate = `ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:a62c6be5${"0".repeat(56)}`;
    const draining = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            validator_hotkey: hotkey,
            availability: "available",
            health: "healthy",
            state: "running_benchmark",
            configured_slots: 2,
            healthy_slots: ["slot-0", "slot-1"],
            protocol_version: 23,
            software_version: "0.68.21",
            reported_at: "2026-07-31T13:55:00Z",
            seen_at: "2026-07-31T13:55:00Z",
            stack: { mode: "managed" },
            updater_status: {
              enabled: true,
              channel: "compat-2",
              state: "draining",
              transaction_phase: "prepared",
              current_version: "0.68.21",
              candidate_descriptor: candidate,
              candidate_version: null,
              failed_candidate_count: 0,
              retry_after: null,
              suppressed: false,
              last_success_at: 1_775_131_200,
              last_failure_at: null,
              last_failure_reason: null,
              observed_at: 1_775_132_100,
            },
            active_benchmarks: [
              {
                slot_id: "slot-0",
                stage: "running_benchmark",
                percent: 62,
                completed_checks: 218,
                total_checks: 351,
                bench_version: 9,
                started_at: "2026-07-31T13:00:00Z",
                agent_id: "agent-one",
                agent_name: "One",
              },
              {
                slot_id: "slot-1",
                stage: "running_benchmark",
                percent: 66,
                completed_checks: 233,
                total_checks: 351,
                bench_version: 9,
                started_at: "2026-07-31T13:00:00Z",
                agent_id: "agent-two",
                agent_name: "Two",
              },
            ],
          },
        ],
      },
    } satisfies OperationsPayload;
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      if (String(input).includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(draining), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;

    render(() => <OperationsPage />);
    await waitFor(() => expect(document.querySelector(".fleet-updater-notice")).toBeTruthy());

    const row = document.querySelector(`#fleet-rows tr[data-entity-id="${hotkey}"]`);
    const notice = row?.querySelector(".fleet-updater-notice");
    expect(notice).toHaveAttribute("aria-label", "Managed updater status");
    expect(notice?.querySelector(".stage")?.textContent).toBe("Safe drain");
    expect(notice?.textContent).toContain("Target a62c6be5…");
    expect(notice?.textContent).toContain("Finishing 2 active runs before restart · no new work.");
    expect(row?.querySelector(".fleet-updater-mode")?.textContent).toBe("Managed · compat-2");
    // Who owns updates stays in the row; when it last succeeded is drill-down
    // detail and no longer competes with live work for the reader's eye.
    expect(row?.querySelector(".fleet-updater-success")).toBeNull();
  });

  it("shows the screener stage vocabulary on the screener fleet", async () => {
    const screening = {
      screeners: [
        {
          screener_hotkey: "5ScreenerHotkey000000000000000000000000000000000",
          instance_id: "screener-1",
          availability: "available",
          health: "healthy",
          state: "screening",
          protocol_version: 4,
          policy_version: 9,
          reported_at: "2026-07-31T13:55:00Z",
          seen_at: "2026-07-31T13:55:00Z",
          active_agent_id: "agent-in-screening",
          active_agent_name: "Screenee",
          screening_progress: { stage: "building", started_at: "2026-07-31T13:50:00Z" },
        },
      ],
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/screeners")) {
        return Promise.resolve(new Response(JSON.stringify(screening), { status: 200 }));
      }
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(operations), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelector("#fleet-rows tr[data-entity-id]")).toBeTruthy(),
    );
    fireEvent.click(document.getElementById("operations-tab-screeners") as HTMLButtonElement);
    await waitFor(() =>
      expect(document.querySelector("td.fleet-work-col .screener-progress")).toBeTruthy(),
    );
    const cell = document.querySelector("td.fleet-work-col") as HTMLElement;
    expect(cell.textContent).toContain("Building image");
    expect(cell.querySelector(".screener-progress-time")).toHaveAttribute(
      "data-started-at",
      "2026-07-31T13:50:00Z",
    );
  });

  it("announces screener hardware beside the load it is carrying", async () => {
    const screening = {
      screeners: [
        {
          screener_hotkey: "5ScreenerHotkey000000000000000000000000000000000",
          instance_id: "screener-1",
          availability: "available",
          health: "healthy",
          state: "polling",
          protocol_version: 6,
          policy_version: 11,
          reported_at: "2026-07-31T13:55:00Z",
          seen_at: "2026-07-31T13:55:00Z",
          host_specs: {
            cpu_count: 16,
            cpu_physical_cores: 8,
            memory_total_mib: 64000,
            disk_total_gib: 500,
            architecture: "x86_64",
          },
          system_metrics: { cpu_percent: 20, memory_percent: 35, disk_percent: 45 },
        },
        {
          screener_hotkey: "5ScreenerHotkey000000000000000000000000000000000",
          instance_id: "screener-legacy",
          availability: "available",
          health: "healthy",
          state: "polling",
          protocol_version: 5,
          policy_version: 11,
          reported_at: "2026-07-31T13:55:00Z",
          seen_at: "2026-07-31T13:55:00Z",
        },
      ],
    };
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/screeners")) {
        return Promise.resolve(new Response(JSON.stringify(screening), { status: 200 }));
      }
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(operations), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelector("#fleet-rows tr[data-entity-id]")).toBeTruthy(),
    );
    fireEvent.click(document.getElementById("operations-tab-screeners") as HTMLButtonElement);
    await waitFor(() => expect(document.querySelector(".fleet-host-specs")).toBeTruthy());

    const announced = document.querySelectorAll(".fleet-host-specs");
    // Only the v6 worker announced anything; the v5 worker stays silent rather
    // than rendering an invented default.
    expect(announced).toHaveLength(1);
    const specs = announced[0] as HTMLElement;
    expect(specs.textContent).toBe("16 vCPU · 63 GiB · 500 GiB disk");
    expect(specs).toHaveAttribute(
      "title",
      "Announced host · 16 logical CPUs (8 physical cores, x86_64) · 63 GiB RAM · 500 GiB disk",
    );
  });

  it("shows LongMem confirmation separately from ordinary validator slots", async () => {
    const confirmation = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            ...operations.validators.validators?.[1],
            active_benchmarks: [],
            assigned_benchmarks: [],
            configured_slots: 2,
            allowed_slots: 2,
            healthy_slots: ["slot-0", "slot-1"],
            admission: "accepting",
            confirmation_benchmarks: [
              {
                bundle_id: "b6b0b030-4ef6-4918-b083-60de95e6a8d1",
                slot_id: "longmem-0",
                bench_version: 9 as const,
                mode: "shadow" as const,
                profile_revision: "longmemeval-s-native-memory-tools-v2",
                attempt: 1,
                issued_at: "2026-07-31T13:00:00Z",
                deadline: "2026-07-31T14:30:00Z",
                stage: "running_confirmation" as const,
                completed: 117,
                total: 500,
                reported_agent_id: "fd870bca-b6b0-4ef6-8918-b08360de95e6",
                progress_reported_at: "2026-07-31T13:15:00Z",
                subjects: [
                  {
                    agent_id: "fd870bca-b6b0-4ef6-8918-b08360de95e6",
                    agent_name: "Memory agent",
                  },
                ],
              },
            ],
          },
        ],
      },
    } satisfies OperationsPayload;
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(confirmation), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;

    render(() => <OperationsPage />);
    await waitFor(() => expect(document.querySelector(".fleet-confirmation-lane")).toBeTruthy());
    const lane = document.querySelector(".fleet-confirmation-lane") as HTMLElement;
    expect(lane.textContent).toContain("LongMemEval");
    expect(lane.textContent).toContain("Independent confirmation lane · ablations included");
    expect(lane.querySelector(".fleet-slot-id")?.textContent).toBe("0");
    expect(lane.querySelector(".fleet-slot-id")).toHaveAttribute("title", "longmem-0");
    expect(lane.textContent).toContain("Shadow");
    expect(lane.textContent).toContain("Running LongMemEval");
    expect(lane.textContent).toContain("Attempt 1");
    expect(lane.textContent).toContain("117/500 cases");
    expect(lane.querySelector("progress")).toHaveAttribute("value", "117");
    expect(lane.querySelector("progress")).toHaveAttribute("max", "500");
    expect(lane.querySelector('[data-entity-link="agent"]')).toBeTruthy();
    expect(document.querySelector(".fleet-slot-disclosure")?.textContent).toContain(
      "2 slots · 0 running · 2 idle",
    );
  });

  it("does not treat a job-level 0/1 heartbeat as LongMem case progress", async () => {
    const confirmation = {
      ...operations,
      validators: {
        ...operations.validators,
        validators: [
          {
            ...operations.validators.validators?.[1],
            active_benchmarks: [],
            assigned_benchmarks: [],
            configured_slots: 2,
            allowed_slots: 2,
            healthy_slots: ["slot-0", "slot-1"],
            admission: "accepting",
            confirmation_benchmarks: [
              {
                bundle_id: "b6b0b030-4ef6-4918-b083-60de95e6a8d1",
                slot_id: "longmem-0",
                bench_version: 9 as const,
                mode: "shadow" as const,
                profile_revision: "longmemeval-s-native-memory-tools-v2",
                attempt: 2,
                issued_at: "2026-07-31T13:00:00Z",
                deadline: "2026-07-31T14:30:00Z",
                stage: "running_confirmation" as const,
                completed: 0,
                total: 1,
                reported_agent_id: "fd870bca-b6b0-4ef6-8918-b08360de95e6",
                progress_reported_at: "2026-07-31T13:15:00Z",
                subjects: [
                  {
                    agent_id: "fd870bca-b6b0-4ef6-8918-b08360de95e6",
                    agent_name: "Memory agent",
                  },
                ],
              },
            ],
          },
        ],
      },
    } satisfies OperationsPayload;
    restoreFetch?.();
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(confirmation), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ validators: [] }), { status: 200 }));
    }) as typeof fetch;

    render(() => <OperationsPage />);
    await waitFor(() => expect(document.querySelector(".fleet-confirmation-lane")).toBeTruthy());
    const lane = document.querySelector(".fleet-confirmation-lane") as HTMLElement;
    expect(lane.textContent).toContain("Attempt 2");
    expect(lane.textContent).toContain("Running LongMemEval");
    expect(lane.textContent).toContain("Heartbeat");
    expect(lane.textContent).toContain("Memory agent");
    expect(lane.textContent).not.toContain("0/1");
    expect(lane.querySelector("progress")).toBeNull();
  });
});

// ── Row 26: test_validator_names_remain_optional_untrusted_decoration ───────
// Validator display names / stake weights are optional decoration from a
// separate feed (reset on refetch, escaped, the hotkey stays the anchor
// identity), the fleet sorts by stake then hotkey, and unavailability is
// flagged rather than fatal.
describe("validator names are untrusted decoration (row 26)", () => {
  it("sorts the fleet by stake weight, then hotkey, and keeps the hotkey anchor", async () => {
    await renderPage();
    const order = Array.from(document.querySelectorAll("#fleet-rows tr[data-entity-id]"), (row) =>
      row.getAttribute("data-entity-id"),
    );
    expect(order[0]).toBe(DITTO);
    const ditto = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`);
    expect(ditto?.querySelector(".fleet-node-name")?.textContent).toBe("Ditto");
    const key = ditto?.querySelector(".fleet-node-key");
    expect(key?.querySelector('[data-entity-link="validator"]')?.textContent).toBe(
      DITTO.slice(0, 8) + "…" + DITTO.slice(-6),
    );
    expect(key?.querySelector("button.copy")).toHaveAttribute("data-key", DITTO);
  });

  it("renders a hostile display name as inert text", async () => {
    const names = {
      validators: [
        {
          validator_hotkey: DITTO,
          display_name: "<img src=x onerror=alert(1)>",
          stake_weight: 1,
        },
      ],
    };
    restoreFetch?.();
    const fixtures = installFixtureFetch();
    const fixtureFetch = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/validator-names")) {
        return Promise.resolve(new Response(JSON.stringify(names), { status: 200 }));
      }
      return fixtureFetch(input);
    }) as typeof fetch;
    restoreFetch = fixtures;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(
        document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"] .fleet-node-name`),
      ).toBeTruthy(),
    );
    const name = document.querySelector(
      `#fleet-rows tr[data-entity-id="${DITTO}"] .fleet-node-name`,
    ) as HTMLElement;
    expect(name.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(name.querySelector("img")).toBeNull();
  });

  it("keeps the fleet readable when the name feed fails", async () => {
    restoreFetch?.();
    const fixtures = installFixtureFetch();
    const fixtureFetch = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/public/validator-names")) {
        return Promise.resolve(new Response("{}", { status: 500 }));
      }
      return fixtureFetch(input);
    }) as typeof fetch;
    restoreFetch = fixtures;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`)).toBeTruthy(),
    );
    const ditto = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`);
    expect(ditto?.querySelector(".fleet-node-name")).toBeNull();
    expect(ditto?.querySelector(".fleet-node.copyable")).toBeTruthy();
  });
});

describe("Targon submission build provenance", () => {
  it("shows provider provenance independently from the screener fleet", async () => {
    const payload: OperationsPayload = {
      ...operations,
      submission_builds: {
        window_hours: 24,
        active_count: 1,
        targon_completed_count: 12,
        fallback_authorized_count: 2,
        builds: [
          {
            agent_id: "4f44ebd4-72ad-4e96-bbec-e7393d95b913",
            agent_name: "Targon Trial",
            agent_version: 3,
            status: "consumed",
            provider: "targon",
            attempt_count: 1,
            output_sha256: "a".repeat(64),
            output_size_bytes: 104857600,
            created_at: "2026-07-31T12:00:00Z",
            completed_at: "2026-07-31T12:08:00Z",
            consumed_at: "2026-07-31T12:09:00Z",
            updated_at: "2026-07-31T12:09:00Z",
          },
          {
            agent_id: "5e5509dc-80ae-42c8-b954-121330697292",
            agent_name: "Fallback Trial",
            agent_version: 1,
            status: "fallback_required",
            provider: "targon",
            attempt_count: 3,
            error_code: "TARGON_SUBMISSION_RUNTIME_ERROR",
            created_at: "2026-07-31T11:00:00Z",
            updated_at: "2026-07-31T11:15:00Z",
          },
        ],
      },
    };
    restoreFetch?.();
    restoreFetch = null;
    const fixtures = installFixtureFetch();
    const fixtureFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("/public/operations")) {
        return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
      }
      return fixtureFetch(input);
    }) as typeof fetch;
    restoreFetch = fixtures;

    const { container } = render(() => <OperationsPage />);
    fireEvent.click(container.querySelector("#operations-tab-builds") as HTMLButtonElement);
    await waitFor(() => {
      expect(container.querySelector(".submission-builds")?.textContent).toContain(
        "12Targon imports",
      );
    });
    const lane = container.querySelector(".submission-builds");
    expect(lane?.textContent).toContain("12Targon imports");
    expect(lane?.textContent).toContain("Targon TrialSubmission v3TargonImported1");
    expect(lane?.textContent).toContain("Manual retry required");
    expect(lane?.textContent).toContain("Builder container crashed");
    expect(lane?.textContent).toContain("this view tracks builders only");
    expect(container.querySelector("#fleet-table")).toBeNull();
  });
});

// ── Refresh resilience (Python guard
// test_operations_refresh_keeps_last_successful_snapshot); the pipeline
// board's half of this rule lives in Pipeline.test.tsx ──────────────────────
describe("refresh resilience", () => {
  it("keeps the last reconciled snapshot when a refresh fails", async () => {
    const { container } = render(() => <OperationsPage />);
    await waitFor(() =>
      expect(container.querySelector("#fleet-rows tr[data-entity-id]")).toBeTruthy(),
    );
    expect(text("operations-snapshot")).toBe("");
    const rowsBefore = container.querySelectorAll("#fleet-rows tr").length;
    expect(rowsBefore).toBeGreaterThan(0);

    restoreFetch?.();
    restoreFetch = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    refreshAllEndpoints();
    await waitFor(() => {
      expect(text("operations-snapshot")).toContain(
        "Refresh delayed · showing last reconciled snapshot",
      );
    });
    expect(container.querySelectorAll("#fleet-rows tr").length).toBe(rowsBefore);
    expect(text("operations-snapshot")).toContain("2h ago");
  });
});

// ── Fleet on-chain weights: what each validator's revealed vector sets ──────
// The fleet page answers "which miner UIDs is THIS validator pointing at"
// per row, from /public/weights, with the leaderboard's gold-top-choice /
// magenta-support chip vocabulary. A validator the snapshot carries no
// vector for says "none revealed" — on a weight-setting fleet that is a
// finding, not missing data.
describe("fleet on-chain weights", () => {
  it("lists the miner UIDs each validator's revealed vector points at", async () => {
    await renderPage();
    expect(fetchedPaths().some((u) => u.includes("/public/weights"))).toBe(true);
    const row = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`) as HTMLElement;
    await waitFor(() => expect(row.querySelector(".fleet-chain-weights")).toBeTruthy());
    expect(row.querySelector(".fleet-chain-weights-label")?.textContent).toContain(
      "On-chain weights",
    );
    const chips = row.querySelectorAll(".chain-vector-chip") as NodeListOf<HTMLElement>;
    expect(chips.length).toBe(5);
    expect(chips[0]).toHaveClass("top-choice");
    expect(chips[0]?.textContent).toContain("UID 160");
    expect(chips[0]?.textContent).toContain("65.0%");
    expect(chips[1]).toHaveClass("support");
    expect(chips[1]?.textContent).toContain("UID 31");
    expect(row.querySelector(".fleet-chain-weights")?.getAttribute("title")).toContain(
      "Revealed at block",
    );
  });

  it('says "none revealed" for a validator the snapshot has no vector for', async () => {
    const base = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (raw.includes("/public/weights")) {
        const body = loadFixture<{ vectors: { validator_hotkey?: string }[] }>("weights");
        body.vectors = body.vectors.filter((vector) => vector.validator_hotkey !== DITTO);
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }
      return base(input);
    }) as typeof fetch;
    try {
      await renderPage();
      const row = document.querySelector(
        `#fleet-rows tr[data-entity-id="${DITTO}"]`,
      ) as HTMLElement;
      await waitFor(() =>
        expect(row.querySelector(".fleet-chain-weights-none")?.textContent).toBe("none revealed"),
      );
      expect(row.querySelectorAll(".chain-vector-chip").length).toBe(0);
      const withVector = document.querySelector(
        `#fleet-rows tr[data-entity-id="${hotkeyOf("5CFtzzb4")}"]`,
      ) as HTMLElement;
      expect(withVector.querySelectorAll(".chain-vector-chip").length).toBeGreaterThan(0);
    } finally {
      globalThis.fetch = base;
    }
  });
});
