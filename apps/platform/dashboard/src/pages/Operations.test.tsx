// Parity tests for the operations page (assert-inventory rows 15, 16, 17,
// 18, 22, 26). The old suite grepped the monolith's source; these render the
// SolidJS port against the recorded fixtures (frozen clock 2026-07-31T14:00Z,
// the golden renderer's instant) and assert the same contracts on the DOM.
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

// Fixture cast: Ditto (healthy, top stake), Rizzo, WildSage active; TAO.com
// (offline + scorer down), Yuma (offline) and an anonymous obsolete build
// folded away as inoperative.
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
    expect(document.getElementById("fleet-summary")?.textContent).toContain("active validators"),
  );
}

function text(id: string): string {
  return document.getElementById(id)?.textContent ?? "";
}

// ── Row 15: test_includes_accessible_fleet_status ───────────────────────────
// Guards the fleet health table (headings, screener toggle, offline/retired
// split) and the removal of privacy-leaking / hardcoded-threshold copy.
describe("accessible fleet status (row 15)", () => {
  it("renders the fleet table with its headings and summary from the three feeds", async () => {
    await renderPage();
    // Endpoints: the page consumes exactly the three public feeds.
    expect(fetchedPaths().some((u) => u.includes("/public/operations"))).toBe(true);
    expect(fetchedPaths().some((u) => u.includes("/public/validator-names"))).toBe(true);
    expect(fetchedPaths().some((u) => u.includes("/public/screeners"))).toBe(true);

    expect(document.querySelector(".fleet-table-head h2")?.textContent).toBe("Validator fleet");
    // available + " of " + entries.length + " active " + kind — never the
    // removed '" reporting " + kind' phrasing.
    expect(text("fleet-summary")).toContain("3 of 3 active validators available");
    expect(text("fleet-summary")).toContain("· 3 inoperative");
    expect(text("fleet-summary")).toContain("snapshot 2h ago");

    const headings = Array.from(
      document.querySelectorAll("#fleet-table thead th"),
      (th) => th.textContent,
    );
    expect(headings).toEqual([
      "Validator",
      "Status",
      "First seen",
      "Last heartbeat",
      "Current work",
      "Version",
      "CPU",
      "Memory",
      "Disk",
      "Containers",
    ]);
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
    expect(text("fleet-summary")).toContain("2 of 2 active screeners available");
    expect(document.getElementById("fleet-table")).toHaveAttribute(
      "aria-label",
      "Screener fleet health",
    );
    // The screener fold keeps the old title + 24h retention note.
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

  it("fills the ledger (unknown bucket included) and explains missing telemetry", async () => {
    await renderPage();
    expect(text("fleet-count-healthy")).toBe("3");
    expect(text("fleet-count-critical")).toBe("2");
    expect(text("fleet-count-offline")).toBe("1");
    expect(text("fleet-count-unknown")).toBe("0");
    expect(document.querySelector(".fleet-ledger p")?.textContent).toContain(
      "Missing optional telemetry is not an outage.",
    );
  });

  it("never leaks the removed privacy note / allowlist / threshold copy", async () => {
    await renderPage();
    const body = document.body.textContent ?? "";
    expect(body).not.toContain("allowlisted");
    expect(document.querySelector(".privacy-note")).toBeNull();
    expect(document.querySelector(".fleet-health-note")).toBeNull();
    // Meters carry the value; only memory>=90 / disk>=95 warn (no CPU rule).
    const dittoRow = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`);
    const meters = Array.from(dittoRow?.querySelectorAll(".fleet-meter") ?? []);
    expect(meters.length).toBe(3);
    expect(meters.every((m) => !m.classList.contains("warn"))).toBe(true);
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
    // None of the folded rows leak into the open table.
    expect(document.querySelector(`#fleet-rows tr[data-entity-id="${TAO}"]`)).toBeNull();
  });

  it("reads the offline window from the snapshot instead of restating 15 minutes", async () => {
    await renderPage();
    // stale_window_seconds=900 → "15m"; the copy never hardcodes the window.
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
    expect(tao?.querySelector(".stage")?.textContent).toBe("Scorer down");
    const yuma = document.querySelector(`#fleet-retired-rows tr[data-entity-id="${YUMA}"]`);
    expect(yuma?.querySelector(".stage")?.textContent).toBe("Offline");
    expect(yuma?.querySelector(".stage")?.classList.contains("bad")).toBe(true);
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
    // if (folded) folded.open = true — the row must be readable, not hidden
    // behind a closed disclosure.
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
    expect(row?.querySelector(".stage")?.textContent).toBe("Obsolete build");
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
    expect(row?.querySelector(".stage")?.textContent).toBe("Scorer down");
    expect((document.getElementById("fleet-retired") as HTMLDetailsElement).hidden).toBe(true);
  });

  it("names the bench gate from the snapshot version, never a literal", async () => {
    await renderPage();
    // The note names v7 because the snapshot says 7 — see fleet.test.ts for
    // the precedence ladder and the no-version fallback.
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
    // Banned per-panel endpoints from the pre-snapshot era.
    expect(fetchedPaths().some((u) => u.includes("/public/validators"))).toBe(false);
    expect(fetchedPaths().some((u) => u.includes("/public/activity?page=1&limit=200"))).toBe(false);
  });

  it("stamps the shared snapshot note with reconciliation + age", async () => {
    await renderPage();
    const note = document.getElementById("operations-snapshot");
    expect(note).toHaveAttribute("aria-live", "polite");
    expect(note?.textContent).toBe(
      "Pipeline and fleet reconciled · recent history shown; full history in Activity · 2h ago",
    );
  });

  it("states absence when the snapshot fetch fails", async () => {
    restoreFetch?.();
    globalThis.fetch = (() => Promise.resolve(new Response("{}", { status: 500 }))) as typeof fetch;
    render(() => <OperationsPage />);
    await waitFor(() =>
      expect(text("operations-snapshot")).toBe("Shared operations snapshot unavailable"),
    );
    expect(text("fleet-summary")).toBe("Validator status unavailable");
    expect(text("fleet-count-healthy")).toBe("–");
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
      document.querySelectorAll("#fleet-rows td:nth-child(2) .stage"),
      (el) => el.textContent,
    );
    expect(stages).toEqual(["Mismatch", "Heartbeat stale"]);
    // A mismatch counts warning in the ledger, not healthy; a merely-stale
    // heartbeat is not a fault, so that node stays healthy (updateFleetLedger
    // 8845–8859 has no heartbeat_stale bucket).
    expect(text("fleet-count-warning")).toBe("1");
    expect(text("fleet-count-healthy")).toBe("1");
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
      expect(document.querySelector("td.fleet-work-col .benchmark-progress")).toBeTruthy(),
    );
    const cell = document.querySelector("td.fleet-work-col") as HTMLElement;
    expect(cell.querySelector(".benchmark-version-chip")?.textContent).toBe("Bench v7");
    const bar = cell.querySelector("progress") as HTMLProgressElement;
    expect(bar).toHaveAttribute("max", "100");
    expect(bar).toHaveAttribute("value", "47");
    expect(bar.getAttribute("aria-label")).toContain("Running benchmark");
    expect(cell.textContent).toContain("Benchmark 47% · 132 of 281 checks");
    // Per-second elapsed timer node, driven from progress.started_at.
    const time = cell.querySelector(".benchmark-progress-time");
    expect(time).toHaveAttribute("data-started-at", "2026-07-31T13:00:00Z");
    expect(time?.textContent).toBe("1h 0m 0s");
    // The agent under evaluation is linked, not just named.
    expect(cell.querySelector('.benchmark-agent [data-entity-link="agent"]')).toBeTruthy();
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
      expect(document.getElementById("fleet-summary")?.textContent).toContain("active validators"),
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
    // Ditto (2.6e14) > Rizzo (1.8e13) > WildSage (5953).
    expect(order[0]).toBe(DITTO);
    const ditto = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`);
    expect(ditto?.querySelector(".fleet-node-name")?.textContent).toBe("Ditto");
    // The hotkey stays the anchor identity beside the decoration, with copy.
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
      expect(document.getElementById("fleet-summary")?.textContent).toContain(
        "active validators available",
      ),
    );
    // No decoration: the hotkey-only identity renders instead of a name.
    const ditto = document.querySelector(`#fleet-rows tr[data-entity-id="${DITTO}"]`);
    expect(ditto?.querySelector(".fleet-node-name")).toBeNull();
    expect(ditto?.querySelector(".fleet-node.copyable")).toBeTruthy();
  });
});

// ── Pipeline board on the shared snapshot (map: operations markup 2742–2862) ─
describe("pipeline board from the shared snapshot", () => {
  it("renders the four stage columns with authoritative counts", async () => {
    await renderPage();
    const stages = Array.from(
      document.querySelectorAll("#pipeline-overview .pipeline-column"),
      (col) => col.getAttribute("data-pipeline-stage"),
    );
    expect(stages).toEqual(["admission", "waiting_validator", "evaluating", "scored"]);
    expect(text("pipeline-scored-count")).toContain("628");
    expect(document.querySelectorAll("#pipeline-scored .pipeline-item").length).toBe(50);
    expect(document.querySelector("#pipeline-scored .pipeline-more")?.textContent).toBe(
      "578 older submissions in Activity",
    );
    // Newest score first.
    expect(
      document.querySelector("#pipeline-scored .pipeline-item .pipeline-item-name")?.textContent,
    ).toBe("blackhole_v8");
  });

  it("separates the stuck backlog behind its quick-filter", async () => {
    await renderPage();
    // The three waiting entries all exhausted their retry budget: the count
    // stays authoritative while the actionable lane reads empty.
    const count = document.getElementById("pipeline-wait-validator-count") as HTMLElement;
    expect(count.textContent).toContain("3");
    const stuck = count.querySelector("[data-pipeline-stuck-filter]") as HTMLButtonElement;
    expect(stuck.textContent).toBe("3 stuck");
    expect(stuck).toHaveAttribute("aria-pressed", "false");
    expect(document.querySelector("#pipeline-wait-validator .pipeline-empty")?.textContent).toBe(
      "No submissions waiting.",
    );

    fireEvent.click(stuck);
    await waitFor(() =>
      expect(document.querySelectorAll("#pipeline-wait-validator .pipeline-item").length).toBe(3),
    );
    const chips = document.querySelectorAll("#pipeline-wait-validator .retry-chip.exhausted");
    expect(chips.length).toBe(3);
    expect(chips[0]?.textContent).toBe("Stuck · needs operator");
  });

  it("hides the rescreen notice when no policy rescreen is queued", async () => {
    await renderPage();
    expect((document.getElementById("rescreen-notice") as HTMLElement).hidden).toBe(true);
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
    expect(lane?.textContent).toContain("Targon → local allowed");
    expect(lane?.textContent).toContain("Builder container crashed");
    expect(lane?.textContent).toContain("this view tracks builders only");
    expect(container.querySelector("#fleet-table")).toBeNull();
  });
});

// ── Weekend drift: renamed lanes, the integrity-review branch, and the
// last-reconciled-snapshot rule (Python guards
// test_operations_refresh_keeps_last_successful_snapshot and the #623/#635
// board reshape) ─────────────────────────────────────────────────────────────
describe("weekend drift: board reshape and refresh resilience", () => {
  it("renders the mechanical-admission lane names and the atlas explainer", async () => {
    const { container } = render(() => <OperationsPage />);
    await waitFor(() => {
      expect(container.querySelector("#pipeline-admission-title")?.textContent).toBe(
        "Build & admission",
      );
    });
    expect(container.querySelectorAll("#pipeline-overview .pipeline-column")).toHaveLength(4);
    expect(container.querySelector("#pipeline-wait-validator-title")?.textContent).toBe(
      "Waiting for validators",
    );
    expect(container.querySelector("#pipeline-evaluating-title")?.textContent).toBe("Scoring");
    expect(container.querySelector("#pipeline-scored-title")?.textContent).toBe("Scored & live");
    expect(container.textContent).toContain(
      "Mechanical admission builds a verified image before validators.",
    );
  });

  it("shows the conditional integrity-review branch with the authoritative count", async () => {
    const { container } = render(() => <OperationsPage />);
    await waitFor(() => {
      expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("53");
    });
    const aside = container.querySelector(".pipeline-review-branch");
    expect(aside?.querySelector(".pipeline-review-eyebrow")?.textContent).toBe(
      "Conditional after scoring",
    );
    expect(aside?.querySelector("#pipeline-review-title")?.textContent).toBe(
      "Source integrity review",
    );
    // Only qualifiers and anomaly holds enter — the copy says so, and the
    // fixture window carries none, so the branch states that rather than
    // implying review of everything.
    expect(aside?.textContent).toContain("Only leaderboard qualifiers and robust anomaly holds");
    expect(container.querySelector("#pipeline-review-items")?.textContent).toContain(
      "No submissions are held for integrity review.",
    );
  });

  it("keeps the last reconciled snapshot when a refresh fails", async () => {
    const { container } = render(() => <OperationsPage />);
    await waitFor(() => {
      expect(text("operations-snapshot")).toContain("Pipeline and fleet reconciled");
    });
    const rowsBefore = container.querySelectorAll("#fleet-rows tr").length;
    expect(rowsBefore).toBeGreaterThan(0);

    // Every subsequent operations fetch fails; the board must NOT blank.
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
    // A refresh failure does not invalidate the last reconciled snapshot:
    // the fleet table and the board keep rendering it while polls retry.
    expect(container.querySelectorAll("#fleet-rows tr").length).toBe(rowsBefore);
    expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("53");
    expect(text("operations-snapshot")).toContain("2h ago");
  });

  it("cold-start failure still renders the unavailable placeholders", async () => {
    restoreFetch?.();
    restoreFetch = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    const { container } = render(() => <OperationsPage />);
    await waitFor(() => {
      expect(text("operations-snapshot")).toBe("Shared operations snapshot unavailable");
    });
    expect(container.querySelector("#pipeline-review-count")?.textContent).toBe("–");
    expect(container.querySelector("#pipeline-review-items")?.textContent).toContain(
      "Review state unavailable.",
    );
  });
});
