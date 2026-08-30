// Tests for the validator modal's Capabilities, Stack identity, and Component
// health sections.
//
// These sections escaped BOTH gates of the SPA port: the modal only opens on
// interaction, so the per-page DOM goldens never contained it, and the old
// Python suites barely asserted it. Nothing here is incidental — every row is
// an operator answering "which half of this validator is lying to me", so the
// tests pin the copy, the collapsed/open state and the order, not just
// presence.
//
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { rankEntries } from "../lib/scoring";
import { syncFromLocation } from "../stores/routeStore";
import { installFixtureFetch, loadFixture } from "../test-fixtures";
import type { FleetEntry, OperationsPayload } from "../types/fleet";
import type { LeaderboardPayload } from "../types/leaderboard";
import type { BenchmarkProgress } from "../types/pipeline";
import type { FleetEntryExt } from "./operations/fleet";
import { EntityPanel } from "./EntityPanel";

const leaderboard = loadFixture<LeaderboardPayload>("leaderboard");
const entries = rankEntries(leaderboard.entries ?? []);
const operations = loadFixture<OperationsPayload>("operations");
const validatorRows = operations.validators.validators ?? [];

const hotkeyOf = (prefix: string): string =>
  String(
    validatorRows.find((v) => String(v.validator_hotkey).startsWith(prefix))?.validator_hotkey,
  );

/** Protocol 18, managed signed release, fresh identity-verified scorer, five
 * of six components observed (model_relay and ollama absent on both sides). */
const MANAGED = hotkeyOf("5HmP9732");
/** Protocol 18 source build: no release descriptor digest. */
const SOURCE = hotkeyOf("5CqJAjSj");
/** Protocol 15: legacy v2-only scorer, a probe that never served, and all six
 * components observed (ollama reports an embedding model). */
const LEGACY = hotkeyOf("5FU3YKmv");
/** Protocol 6: no capabilities, no stack, no per-component health at all. */
const ANCIENT = hotkeyOf("5HKpbkeL");

const COMPONENT_LABELS = [
  "Validator worker",
  "Scorer · dittobench-api",
  "Sandbox Docker",
  "Model relay",
  "Pylon",
  "Ollama",
];

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
  document.body.classList.remove("entity-page");
});

/** A deep copy of the recorded snapshot with one validator patched, so a
 * synthetic shape never leaks into the next test. */
function patched(hotkey: string, patch: Partial<FleetEntryExt>): OperationsPayload {
  const payload = structuredClone(operations);
  const rows = payload.validators.validators ?? [];
  const index = rows.findIndex((row) => row.validator_hotkey === hotkey);
  rows[index] = { ...(rows[index] as FleetEntry), ...patch } as FleetEntry;
  return payload;
}

function open(hotkey: string, payload: OperationsPayload = operations): void {
  render(() => (
    <EntityPanel
      entries={() => entries}
      operations={() => payload}
      validatorNames={() => ({})}
      currentBench={() => 7}
      settledView={() => false}
    />
  ));
  history.replaceState(null, "", "/#/operations?validator=" + hotkey);
  syncFromLocation();
}

/** Top-level sections of the validator body, in render order. */
function sections(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>("#d-stats .vdetail > details.cgroup"));
}

function sectionTitles(): string[] {
  return sections().map((el) => el.firstElementChild?.textContent ?? "");
}

function section(title: string): HTMLElement {
  const found = sections().find((el) => el.firstElementChild?.textContent === title);
  if (!found) throw new Error("missing section: " + title);
  return found;
}

/** One component's collapsible group inside Component health. */
function component(label: string): HTMLElement {
  const groups = Array.from(
    section("Component health").querySelectorAll<HTMLElement>("details.cgroup"),
  );
  const found = groups.find(
    (el) => el.querySelector("summary.cgsum > span")?.textContent === label,
  );
  if (!found) throw new Error("missing component: " + label);
  return found;
}

/** Stat rows directly inside a scope (never a nested component group). */
function rows(scope: ParentNode): HTMLElement[] {
  return Array.from(scope.querySelectorAll<HTMLElement>(".stat-row"));
}

function rowKeys(scope: ParentNode): string[] {
  return rows(scope).map((el) => el.querySelector(".k")?.textContent ?? "");
}

function row(scope: ParentNode, key: string): HTMLElement {
  const found = rows(scope).find((el) => el.querySelector(".k")?.textContent === key);
  if (!found) throw new Error("missing row: " + key);
  return found;
}

function value(scope: ParentNode, key: string): string {
  return row(scope, key).querySelector(".v")?.textContent ?? "";
}

function chipOf(scope: ParentNode, key: string): [string, string] {
  const chip = row(scope, key).querySelector(".stage");
  return [chip?.textContent ?? "", chip?.className ?? ""];
}

function subheads(scope: ParentNode): string[] {
  return Array.from(scope.querySelectorAll<HTMLElement>(".subhead")).map(
    (el) => el.textContent ?? "",
  );
}

describe("validator modal section order", () => {
  it("renders all five signed-report sections, Component health among them", () => {
    open(MANAGED);
    expect(sectionTitles()).toEqual([
      "Signed report",
      "Capabilities",
      "Stack identity",
      "Component health",
      "Host metrics",
    ]);
    // Signed report and Component health answer the two questions an operator
    // opens this modal with; the other three are one click away.
    expect(section("Signed report").hasAttribute("open")).toBe(true);
    expect(section("Component health").hasAttribute("open")).toBe(true);
    expect(section("Capabilities").hasAttribute("open")).toBe(false);
    expect(section("Stack identity").hasAttribute("open")).toBe(false);
    expect(section("Host metrics").hasAttribute("open")).toBe(false);
  });
});

describe("validator modal · Capabilities (9101–9114)", () => {
  it("reports every capability flag plus the scorer's own probe", () => {
    open(MANAGED);
    const caps = section("Capabilities");
    expect(value(caps, "Screened images")).toBe("Yes");
    expect(value(caps, "Requires screened image")).toBe("Yes");
    expect(value(caps, "Source-build fallback")).toBe("No");
    expect(value(caps, "Managed full stack")).toBe("Yes");
    expect(value(caps, "Stack auto-updater")).toBe("Yes");
    expect(value(caps, "Sandbox egress restricted")).toBe("Yes");
    expect(value(caps, "Executor isolation")).toBe("privileged_dind");
    // The scorer block, in the monolith's order: verdict, then the probe that
    // produced it, then what the scorer says it can serve.
    expect(rowKeys(caps)).toEqual([
      "Screened images",
      "Requires screened image",
      "Source-build fallback",
      "Managed full stack",
      "Stack auto-updater",
      "Sandbox egress restricted",
      "Executor isolation",
      "Scorer status",
      "Scorer probe",
      "Probe observed",
      "Last served",
      "Supported benchmarks",
      "Capability observed",
      "Scorer version",
      "Scorer revision",
    ]);
    expect(chipOf(caps, "Scorer status")).toEqual(["Fresh, identity-verified", "stage good"]);
    expect(chipOf(caps, "Scorer probe")).toEqual(["Serving", "stage good"]);
    expect(value(caps, "Scorer probe")).toBe("Serving · HTTP 200");
    expect(value(caps, "Supported benchmarks")).toBe("v7");
    expect(value(caps, "Scorer version")).toBe("0.41.0");
    // Probe times are epoch seconds on the wire: relative text, exact instant
    // in the title.
    const observed = row(caps, "Probe observed").querySelector(".fleet-time");
    expect(observed).toHaveAttribute("title", "2026-07-31T11:48:41.000Z");
    expect(observed?.textContent).toBe("2h ago");
    expect(row(caps, "Last served").querySelector(".fleet-time")).toHaveAttribute(
      "title",
      "2026-07-31T11:48:41.000Z",
    );
  });

  it("gives the scorer revision the mono/copy treatment, elided but copyable", () => {
    // A git revision of the scorer source, not a credential — read from the
    // recorded fixture rather than restated, so the two assertions below
    // cannot drift from what the row actually renders.
    const scorerRevision = String(
      validatorRows.find((v) => v.validator_hotkey === MANAGED)?.capabilities?.scorer_benchmarks
        ?.source_revision,
    );
    expect(scorerRevision).toMatch(/^[0-9a-f]{40}$/);
    open(MANAGED);
    const revision = row(section("Capabilities"), "Scorer revision");
    expect(revision.querySelector(".v")?.className).toBe("v mono");
    const copyable = revision.querySelector(".copyable");
    expect(copyable).toHaveAttribute("title", scorerRevision);
    expect(copyable?.querySelector("span")?.textContent).toBe("25e8f296a573…0660f2f416");
    const copy = revision.querySelector("button.copy");
    expect(copy).toHaveAttribute("data-key", scorerRevision);
    expect(copy).toHaveAttribute("data-copy-label", "scorer source revision");
  });

  it("reads a failing probe as evidence, not just a verdict", () => {
    open(LEGACY);
    const caps = section("Capabilities");
    expect(chipOf(caps, "Scorer status")).toEqual(["Legacy scorer (v2 only)", "stage warn"]);
    expect(chipOf(caps, "Scorer probe")).toEqual(["No usable answer", "stage bad"]);
    expect(value(caps, "Scorer probe")).toBe("No usable answer · HTTP 404 · 3284 in a row");
    // Never served is a different claim from "served a while ago".
    expect(value(caps, "Last served")).toBe("Not since this validator started");
    expect(value(caps, "Supported benchmarks")).toBe("v2");
    // Rows the payload has nothing for are absent, not blank or guessed.
    expect(rowKeys(caps)).not.toContain("Capability observed");
    expect(rowKeys(caps)).not.toContain("Scorer version");
    expect(rowKeys(caps)).not.toContain("Scorer revision");
  });

  it("names the missing heartbeat protocol instead of implying an unequipped validator", () => {
    open(ANCIENT);
    const caps = section("Capabilities");
    expect(rowKeys(caps)).toEqual(["Capabilities"]);
    expect(value(caps, "Capabilities")).toBe("Not reported (requires heartbeat protocol 7)");
    expect(row(caps, "Capabilities").querySelector(".muted")).toBeTruthy();
  });

  it("separates an unreported scorer (protocol 8) from an unreported probe (15)", () => {
    const capabilities = {
      ...validatorRows.find((v) => v.validator_hotkey === MANAGED)?.capabilities,
    };
    open(MANAGED, patched(MANAGED, { capabilities: { ...capabilities, scorer_benchmarks: null } }));
    expect(value(section("Capabilities"), "Scorer benchmarks")).toBe(
      "Not reported (requires heartbeat protocol 8)",
    );
    cleanup();

    const scorer = { ...capabilities.scorer_benchmarks, probe: null };
    open(
      MANAGED,
      patched(MANAGED, { capabilities: { ...capabilities, scorer_benchmarks: scorer } }),
    );
    const caps = section("Capabilities");
    expect(chipOf(caps, "Scorer status")).toEqual(["Fresh, identity-verified", "stage good"]);
    expect(value(caps, "Scorer probe")).toBe("Not reported (requires heartbeat protocol 15)");
    expect(rowKeys(caps)).not.toContain("Probe observed");
  });

  it("says nothing rather than No for a capability the heartbeat omitted", () => {
    open(MANAGED, patched(MANAGED, { capabilities: { screened_images: null } }));
    const caps = section("Capabilities");
    expect(value(caps, "Screened images")).toBe("Not reported");
    expect(value(caps, "Requires screened image")).toBe("Not reported");
    expect(value(caps, "Executor isolation")).toBe("unknown");
  });
});

describe("validator modal · Stack identity (9116–9126)", () => {
  it("names the signed managed release and pins the descriptor digest", () => {
    open(MANAGED);
    const stack = section("Stack identity");
    expect(rowKeys(stack)).toEqual(["Stack mode", "Compose schema", "Release descriptor"]);
    expect(value(stack, "Stack mode")).toBe("Managed (signed GHCR release)");
    expect(value(stack, "Compose schema")).toBe("1");
    const descriptor = row(stack, "Release descriptor");
    expect(descriptor.querySelector(".v")?.className).toBe("v mono");
    expect(descriptor.querySelector(".copyable")).toHaveAttribute(
      "title",
      "sha256:181dca5089981df874790b424123d59b65374fdf1cae49ea21a54ee47301e30a",
    );
    expect(descriptor.querySelector(".copyable > span")?.textContent).toBe(
      "sha256:181dc…e47301e30a",
    );
    expect(descriptor.querySelector("button.copy")).toHaveAttribute(
      "data-copy-label",
      "release descriptor digest",
    );
  });

  it("reads anything that is not the managed release as a source build", () => {
    open(SOURCE);
    const stack = section("Stack identity");
    expect(value(stack, "Stack mode")).toBe("Source build");
    // No signed descriptor exists for a source build; the row is omitted.
    expect(rowKeys(stack)).toEqual(["Stack mode", "Compose schema"]);
  });

  it("names the missing heartbeat protocol when no stack is reported", () => {
    open(ANCIENT);
    const stack = section("Stack identity");
    expect(rowKeys(stack)).toEqual(["Stack identity"]);
    expect(value(stack, "Stack identity")).toBe("Not reported (requires heartbeat protocol 7)");
  });
});

describe("validator modal · Component health (9128–9139, 9160–9162)", () => {
  it("lists the six components in a fixed order, whatever the payload holds", () => {
    open(MANAGED);
    const labels = Array.from(
      section("Component health").querySelectorAll<HTMLElement>("summary.cgsum > span:first-child"),
    ).map((el) => el.textContent ?? "");
    expect(labels).toEqual(COMPONENT_LABELS);
  });

  it("explains configured versus observed identity when health is present", () => {
    open(MANAGED);
    const intro = section("Component health").querySelector("p.muted") as HTMLElement;
    // Explanatory, not a row: the intro stays smaller than the stats it heads.
    expect(intro.style.fontSize).toBe("12px");
    expect(intro?.textContent).toBe(
      "Configured identity is what Compose intends to run; observed identity is what a live " +
        "probe independently verified; readiness is a real request answered just now. " +
        "Per-component probe times are independent of heartbeat freshness.",
    );
  });

  it("explains the protocol gap instead, when health is absent", () => {
    open(ANCIENT);
    const health = section("Component health");
    expect(health.querySelector("p.muted")?.textContent).toBe(
      "This validator reports heartbeat protocol 6. Per-component runtime health arrives with " +
        "protocol 9.",
    );
    // Still six groups: the components are named even when nothing observed
    // them, so the absence is legible rather than an empty section.
    expect(health.querySelectorAll("details.cgroup").length).toBe(6);
    for (const label of COMPONENT_LABELS) {
      expect(value(component(label), "Health")).toBe(
        "Not reported (requires heartbeat protocol 9)",
      );
      expect(component(label).querySelector("summary .stage")?.textContent).toBe("Unknown");
      expect(subheads(component(label))).toEqual([]);
    }
  });

  it("shows an observed component's readiness, probe time and both identities", () => {
    open(MANAGED);
    const worker = component("Validator worker");
    expect(worker.hasAttribute("open")).toBe(false);
    expect(worker.querySelector("summary .stage")?.textContent).toBe("Healthy");
    expect(worker.querySelector("summary .stage")?.className).toBe("stage good");
    // Probe freshness is per component and independent of the heartbeat, so
    // the summary carries its own time.
    const probed = worker.querySelector("summary .probe-time");
    expect(probed?.textContent).toBe("probed 2h ago");
    expect(probed?.querySelector(".fleet-time")).toHaveAttribute(
      "title",
      "2026-07-31T11:48:41.000Z",
    );
    // Observed identity first (a version only — the probe verified no digest),
    // then the configured pin, which is the whole point of the pairing.
    expect(rowKeys(worker)).toEqual([
      "Health",
      "Required component",
      "Endpoint ready",
      "Last probe",
      "Version",
      "Provenance",
      "Image digest",
      "Source revision",
      "Version",
    ]);
    expect(chipOf(worker, "Health")).toEqual(["Healthy", "stage good"]);
    expect(value(worker, "Required component")).toBe("Yes");
    expect(value(worker, "Endpoint ready")).toBe("Yes");
    expect(subheads(worker)).toEqual(["Observed identity", "Configured identity"]);
    expect(value(worker, "Version")).toBe("0.41.0");
    expect(value(worker, "Provenance")).toBe("signed_descriptor");
    expect(row(worker, "Image digest").querySelector("button.copy")).toHaveAttribute(
      "data-copy-label",
      "Validator worker configured image digest",
    );
    expect(row(worker, "Image digest").querySelector(".copyable > span")?.textContent).toBe(
      "sha256:a4d1e…7bb1a92a07",
    );
  });

  it("distinguishes a component nobody probed from one nobody reported", () => {
    open(MANAGED);
    // Absent on both sides: neither configured nor observed.
    const relay = component("Model relay");
    expect(relay.querySelector("summary .stage")?.textContent).toBe("Unknown");
    expect(relay.querySelector("summary .probe-time")).toBeNull();
    expect(rowKeys(relay)).toEqual(["Health"]);
    expect(value(relay, "Health")).toBe("Not reported (requires heartbeat protocol 9)");
    // Observed and healthy, but the probe could not verify an identity.
    const sandbox = component("Sandbox Docker");
    expect(value(sandbox, "Observed identity")).toBe("Not independently observed");
    expect(subheads(sandbox)).toEqual(["Observed identity", "Configured identity"]);
  });

  it("reads a self-declared unknown as 'Not observed', never as unreported", () => {
    // The validator answered; it just has not probed. Collapsing the two would
    // let a live component that stopped reporting hide behind a protocol gap.
    open(
      MANAGED,
      patched(MANAGED, {
        stack_health: { model_relay: { health: "unknown", required: false } },
      }),
    );
    const relay = component("Model relay");
    expect(relay.querySelector("summary .stage")?.textContent).toBe("Not observed");
    expect(chipOf(relay, "Health")).toEqual(["Not observed", "stage unknown"]);
    expect(value(relay, "Required component")).toBe("No");
    expect(component("Validator worker").querySelector("summary .stage")?.textContent).toBe(
      "Unknown",
    );
  });

  it("names Ollama's readiness as an embedding model, other components as a route", () => {
    open(LEGACY);
    expect(value(component("Ollama"), "Embedding model ready")).toBe("Yes");
    expect(rowKeys(component("Model relay"))).not.toContain("Model route ready");
    cleanup();
    open(
      LEGACY,
      patched(LEGACY, {
        stack_health: { model_relay: { health: "degraded", required: true, model_ready: false } },
      }),
    );
    expect(value(component("Model relay"), "Model route ready")).toBe("No");
    expect(rowKeys(component("Model relay"))).not.toContain("Embedding model ready");
  });

  it("confirms an observed identity that matches the configured pin", () => {
    open(MANAGED);
    // dittobench_api pins a source revision the live probe independently
    // reported; the image digest is unobserved, so the revision is compared.
    const note = component("Scorer · dittobench-api").querySelector(".identity-note");
    expect(note?.className).toBe("identity-note good");
    expect(note?.textContent).toBe("Observed source revision matches the configured pin.");
  });

  it("flags an observed identity that differs from the configured pin", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        stack_health: {
          ditto_subnet: {
            health: "identity_mismatch",
            required: true,
            observed_identity: { image_digest: "sha256:deadbeef" },
          },
        },
      }),
    );
    const worker = component("Validator worker");
    expect(worker.querySelector("summary .stage")?.textContent).toBe("Identity mismatch");
    const note = worker.querySelector(".identity-note");
    expect(note?.className).toBe("identity-note warn");
    expect(note?.textContent).toBe("Observed image digest differs from the configured pin.");
  });

  it("stays silent when only one side has an identity to compare", () => {
    open(MANAGED);
    // Configured pins a digest and a revision; the probe reported a version
    // only, so there is nothing to compare and no note is invented.
    expect(component("Validator worker").querySelector(".identity-note")).toBeNull();
    expect(component("Sandbox Docker").querySelector(".identity-note")).toBeNull();
  });

  it("says a configured component pins nothing rather than showing empty rows", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        stack: { mode: "managed", compose_schema: 1, components: { pylon: { provenance: null } } },
      }),
    );
    const pylon = component("Pylon");
    expect(value(pylon, "Provenance")).toBe("unknown");
    expect(value(pylon, "Identity")).toBe("None pinned");
  });
});

// ── Signed report: assignment, evicted leases and the legacy fallback ───────
// (renderValidatorDetail 9060–9099 + renderValidatorAssignment 8709–8767 and
// orphanedSlotLabel 8783–8804). All three shapes are absent from the recorded
// snapshot — every validator in it reports assignment_state "unassigned", no
// orphaned_slots and a null active_benchmark — so the payloads below are
// synthetic, patched onto a real recorded row.

const AGENT_A = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const AGENT_B = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const AGENT_C = "33333333-cccc-4ccc-8ccc-cccccccccccc";

function running(slotId: string, agentId: string): BenchmarkProgress {
  return {
    slot_id: slotId,
    agent_id: agentId,
    agent_name: "Mnemosyne",
    bench_version: 7,
    stage: "running_benchmark",
    percent: 42,
    completed_checks: 118,
    total_checks: 281,
    started_at: "2026-07-31T13:31:00Z",
  } as BenchmarkProgress;
}

/** The evicted slot-3 lease: the validator's own signed claim that it is still
 * executing, with a deadline still ahead of the frozen clock. */
const ORPHAN_RUNNING = {
  slot_id: "slot-3",
  state: "still_running",
  orphaned_for_seconds: 900,
  agent_id: AGENT_A,
  agent_name: "Mnemosyne",
  evicted_at: "2026-07-31T13:45:00Z",
  original_deadline: "2026-07-31T15:20:00Z",
  protocol_version: 18,
  reason: "operator eviction",
  bench_version: 7,
};
/** A higher slot the platform cannot see into: protocol 15 omits a
 * claimed-but-quiet slot, and this one carries no deadline either. */
const ORPHAN_UNKNOWN = {
  slot_id: "slot-10",
  state: "unknown",
  orphaned_for_seconds: 5400,
  agent_id: AGENT_C,
  agent_name: null,
  evicted_at: "2026-07-31T12:30:00Z",
  original_deadline: null,
  protocol_version: 15,
  reason: "slot omitted by heartbeat protocol 15",
  bench_version: 6,
};

/** The anchors inside one row's value, as [label, href] pairs. */
function anchors(scope: ParentNode, key: string): Array<[string, string]> {
  return Array.from(row(scope, key).querySelectorAll<HTMLAnchorElement>("a.entity-link")).map(
    (el) => [el.textContent ?? "", el.getAttribute("href") ?? ""],
  );
}

function assignmentHeadings(scope: ParentNode): string[] {
  return Array.from(row(scope, "Assignment").querySelectorAll(".assignment-detail b")).map(
    (el) => el.textContent ?? "",
  );
}

describe("validator modal · Assignment (9073–9074)", () => {
  it("names both sides of a skew, between Platform received and Slots", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        assignment_state: "assignment_mismatch",
        assigned_agent_id: AGENT_A,
        assigned_agent_name: "Mnemosyne",
        reported_agent_id: AGENT_B,
      }),
    );
    const signed = section("Signed report");
    // Position is the point: the row reads as the platform's last word on this
    // validator, immediately under the two heartbeat timestamps it contradicts.
    const keys = rowKeys(signed);
    expect(keys.slice(keys.indexOf("Platform received"), keys.indexOf("Slots") + 1)).toEqual([
      "Platform received",
      "Assignment",
      "Slots",
    ]);
    expect(chipOf(signed, "Assignment")).toEqual(["Assignment mismatch", "stage bad"]);
    expect(assignmentHeadings(signed)).toEqual(["Platform", "Heartbeat"]);
    expect(anchors(signed, "Assignment")).toEqual([
      ["Mnemosyne · 11111111", "/operations?agent=" + AGENT_A],
      ["Agent · 22222222", "/operations?agent=" + AGENT_B],
    ]);
  });

  it("reads a one-poll gap as delayed telemetry, not a fleet-wide failure", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        assignment_state: "assignment_mismatch",
        assigned_agent_id: AGENT_A,
        assigned_agent_name: "Mnemosyne",
        reported_agent_id: AGENT_B,
        _telemetry_grace: true,
      }),
    );
    const signed = section("Signed report");
    expect(chipOf(signed, "Assignment")).toEqual(["Telemetry delayed", "stage warn"]);
    // One plain sentence: neither side is named, because neither is at fault.
    expect(assignmentHeadings(signed)).toEqual([]);
    expect(anchors(signed, "Assignment")).toEqual([]);
    expect(row(signed, "Assignment").querySelector(".assignment-detail")?.textContent).toBe(
      "Waiting for the next signed slot update; the last reported progress is retained briefly.",
    );
  });

  it("says which side is empty rather than dropping the side", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        assignment_state: "assignment_mismatch",
        assigned_agent_id: null,
        assigned_agent_name: null,
        reported_agent_id: null,
      }),
    );
    const signed = section("Signed report");
    expect(anchors(signed, "Assignment")).toEqual([]);
    expect(value(signed, "Assignment")).toContain("No active assignment");
    expect(value(signed, "Assignment")).toContain("No active agent");
  });

  it("marks a hand-off in flight as a hand-off, not a mismatch", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        assignment_state: "assigning",
        assigned_agent_id: AGENT_A,
        assigned_agent_name: "Mnemosyne",
      }),
    );
    const signed = section("Signed report");
    expect(chipOf(signed, "Assignment")).toEqual(["Assigning", "stage progress"]);
    expect(assignmentHeadings(signed)).toEqual(["Platform assignment"]);
    expect(value(signed, "Assignment")).toContain("Mnemosyne · 11111111 · handing off");
  });

  it("falls back to a bare agent label while assigning with no id yet", () => {
    open(MANAGED, patched(MANAGED, { assignment_state: "assigning", assigned_agent_id: null }));
    const signed = section("Signed report");
    expect(anchors(signed, "Assignment")).toEqual([]);
    expect(value(signed, "Assignment")).toContain("Agent · handing off");
  });

  it("names only the platform's side when the validator went quiet", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        assignment_state: "heartbeat_stale",
        assigned_agent_id: AGENT_A,
        assigned_agent_name: "Mnemosyne",
      }),
    );
    const signed = section("Signed report");
    expect(chipOf(signed, "Assignment")).toEqual(["Heartbeat stale", "stage warn"]);
    // The heartbeat's side is exactly what went missing, so it is not invented.
    expect(assignmentHeadings(signed)).toEqual(["Platform assignment"]);
    expect(anchors(signed, "Assignment")).toEqual([
      ["Mnemosyne · 11111111", "/operations?agent=" + AGENT_A],
    ]);
  });

  it("says Unknown for a stale heartbeat with nothing assigned", () => {
    open(
      MANAGED,
      patched(MANAGED, { assignment_state: "heartbeat_stale", assigned_agent_id: null }),
    );
    expect(value(section("Signed report"), "Assignment")).toContain("Unknown");
  });

  it("renders no Assignment row at all once the assignment is reconciled", () => {
    open(MANAGED);
    expect(rowKeys(section("Signed report"))).not.toContain("Assignment");
  });
});

describe("validator modal · evicted leases (9068–9087)", () => {
  it("lists every orphaned slot in ordinal order ABOVE the running slots", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        // Deliberately out of order in the payload, and slot-10 sorts after
        // slot-9 numerically, never lexically.
        orphaned_slots: [ORPHAN_UNKNOWN, ORPHAN_RUNNING],
        active_benchmarks: [running("slot-1", AGENT_B)],
      }),
    );
    const keys = rowKeys(section("Signed report"));
    // An operator opening this modal is usually asking "does this host have
    // room", and the answer is no while any of these are listed.
    expect(keys.slice(keys.indexOf("Slots"))).toEqual([
      "Slots",
      "slot-3 · evicted lease",
      "slot-10 · evicted lease",
      "slot-1",
    ]);
  });

  it("carries the eviction chip, the doomed agent and when it stops burning CPU", () => {
    open(MANAGED, patched(MANAGED, { orphaned_slots: [ORPHAN_RUNNING] }));
    const signed = section("Signed report");
    const key = "slot-3 · evicted lease";
    expect(chipOf(signed, key)).toEqual(["Evicted · still running · 15m", "stage warn"]);
    expect(row(signed, key).querySelector(".stage")?.getAttribute("title")).toBe(
      "The validator still reports this slot occupied by Mnemosyne · 11111111, 15m after the " +
        "platform evicted the lease. The run cannot produce a score — its result will be refused " +
        "with a 409. Expected to self-terminate by its original deadline (in 2h). This slot is " +
        "not free.",
    );
    expect(anchors(signed, key)).toEqual([
      ["Mnemosyne · 11111111", "/operations?agent=" + AGENT_A],
    ]);
    const detail = row(signed, key).querySelector(".assignment-detail");
    // The deadline is ahead of now, so it counts forward: relTime would floor a
    // future instant to "0s ago", which reads as "already over".
    expect(detail?.textContent).toBe(
      "Lease released 15m ago · Self-terminates by in 2h · bench v7",
    );
    expect(detail?.querySelector(".fleet-time")?.getAttribute("title")).toBe(
      "2026-07-31T13:45:00Z",
    );
  });

  it("keeps state-unknown distinct from still-running and omits an absent deadline", () => {
    open(MANAGED, patched(MANAGED, { orphaned_slots: [ORPHAN_UNKNOWN] }));
    const signed = section("Signed report");
    const key = "slot-10 · evicted lease";
    expect(chipOf(signed, key)).toEqual(["Evicted · state unknown · 1h", "stage warn"]);
    expect(row(signed, key).querySelector(".stage")?.getAttribute("title")).toContain(
      "silence here is not evidence the slot is free (slot omitted by heartbeat protocol 15)",
    );
    // No agent name reported: the anchor still names the id it does have.
    expect(anchors(signed, key)).toEqual([["Agent · 33333333", "/operations?agent=" + AGENT_C]]);
    expect(row(signed, key).querySelector(".assignment-detail")?.textContent).toBe(
      "Lease released 1h ago · bench v6",
    );
  });

  it("falls back to slot-0 for an orphan record with no slot id", () => {
    open(MANAGED, patched(MANAGED, { orphaned_slots: [{ ...ORPHAN_RUNNING, slot_id: null }] }));
    expect(rowKeys(section("Signed report"))).toContain("slot-0 · evicted lease");
  });

  it("renders no evicted rows when the host has no orphaned slots", () => {
    open(MANAGED);
    expect(rowKeys(section("Signed report")).filter((key) => key.includes("evicted"))).toEqual([]);
  });
});

describe("validator modal · legacy single-benchmark fallback (9097–9098)", () => {
  it("shows the pre-fan-out active_benchmark as the running work", () => {
    open(
      MANAGED,
      patched(MANAGED, { active_benchmarks: [], active_benchmark: running("slot-0", AGENT_A) }),
    );
    const signed = section("Signed report");
    expect(rowKeys(signed)).toContain("Active benchmark");
    expect(value(signed, "Active benchmark")).toContain("Benchmark 42% · 118 of 281 checks");
    expect(anchors(signed, "Active benchmark")).toEqual([
      ["Mnemosyne · 11111111", "/operations?agent=" + AGENT_A],
    ]);
  });

  it("yields to the per-slot rows once the payload fans out", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        active_benchmarks: [running("slot-1", AGENT_B)],
        active_benchmark: running("slot-0", AGENT_A),
      }),
    );
    const keys = rowKeys(section("Signed report"));
    expect(keys).toContain("slot-1");
    expect(keys).not.toContain("Active benchmark");
  });

  it("stays quiet while the Assignment row already describes the hand-off", () => {
    open(
      MANAGED,
      patched(MANAGED, {
        active_benchmarks: [],
        active_benchmark: running("slot-0", AGENT_A),
        assignment_state: "assigning",
        assigned_agent_id: AGENT_A,
        assigned_agent_name: "Mnemosyne",
      }),
    );
    const keys = rowKeys(section("Signed report"));
    expect(keys).toContain("Assignment");
    expect(keys).not.toContain("Active benchmark");
  });

  it("renders neither row for a validator with no running work at all", () => {
    open(MANAGED);
    const keys = rowKeys(section("Signed report"));
    expect(keys).not.toContain("Active benchmark");
    expect(keys.filter((key) => key.startsWith("slot-"))).toEqual([]);
  });
});
