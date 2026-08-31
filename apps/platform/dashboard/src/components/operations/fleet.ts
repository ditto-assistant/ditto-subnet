// Pure fleet logic (monolith 8185–8527 + updateFleetLedger 8845–8884). The
// status ladder itself (fleetStatus / offlineAwareFleetStatus) is shared with
// the validator modal and lives in components/EntityPanel; the wrappers here
// only substitute the snapshot-read bench-gate label the shared copy cannot
// know ("No bench v7" instead of the no-version "Bench unsupported").
import { fleetStatus, offlineAwareFleetStatus } from "../EntityPanel";
import { relDuration, relTimeUntil } from "../../lib/format";
import type { FleetEntry, FleetReport, ScorerProbe, StackIdentity } from "../../types/fleet";
import type { BenchmarkProgress } from "../../types/pipeline";

/** A slot whose lease an operator evicted while the validator's benchmark
 * container may still be executing (#537). */
export interface OrphanedSlot {
  slot_id?: string | null;
  /** "still_running" is the validator's own signed claim; anything else is
   * honest ignorance (heartbeat protocol < 16 omits quiet slots). */
  state?: string | null;
  orphaned_for_seconds?: number | null;
  agent_id?: string | null;
  agent_name?: string | null;
  /** When the platform released its half of the lease. */
  evicted_at?: string | null;
  original_deadline?: string | null;
  protocol_version?: number | string | null;
  reason?: string | null;
  /** The benchmark version the doomed run is still burning CPU on. */
  bench_version?: number | string | null;
}

/** Fleet fields beyond the shared wire type. */
export interface FleetEntryExt extends FleetEntry {
  bench_serviceability?: string | null;
  scorer_liveness?: string | null;
  allowed_slots?: number | null;
  orphaned_slots?: OrphanedSlot[] | null;
  /** Set by preserveTransientValidatorTelemetry when a slot's prior signed
   * progress was carried through the grace window. */
  _telemetry_grace?: boolean;
}

export interface SlotPolicy {
  max_concurrent_slots?: number | null;
  disk_percent_ceiling?: number | null;
}

/** Snapshot fields beyond the shared FleetReport. */
export interface FleetReportExt extends FleetReport {
  active_bench_version?: number | null;
  stale_window_seconds?: number | null;
  slot_policy?: SlotPolicy | null;
}

export type FleetSingular = "validator" | "screener";

export interface ScreenerHostGroup {
  hostId: string;
  workers: FleetEntryExt[];
}

/** Local fleet services identify themselves as `<node>-worker-<n>`. The node
 * portion is the real machine boundary: every sibling reports the same host
 * load and host hardware, while work/state remain process-local. Legacy and
 * provider workers without that suffix remain a one-worker host of their own. */
export function screenerHostId(entry: FleetEntryExt): string {
  const instanceId = String(entry.instance_id || "").trim();
  if (instanceId) return instanceId.replace(/-worker-[1-9][0-9]*$/, "");
  return String(entry.screener_hotkey || "Unknown screener host");
}

/** Short process label inside a host disclosure. Keep the full instance id in
 * the DOM title/route identity; repeating the host prefix four times adds no
 * information once the workers are grouped underneath that host. */
export function screenerWorkerLabel(entry: FleetEntryExt, hostId: string): string {
  const instanceId = String(entry.instance_id || "").trim();
  const prefix = hostId + "-worker-";
  if (instanceId.startsWith(prefix)) return "Worker " + instanceId.slice(prefix.length);
  return instanceId || "Worker";
}

export function groupScreenerHosts(entries: readonly FleetEntryExt[]): ScreenerHostGroup[] {
  const groups = new Map<string, FleetEntryExt[]>();
  entries.forEach((entry) => {
    const hostId = screenerHostId(entry);
    const workers = groups.get(hostId);
    if (workers) workers.push(entry);
    else groups.set(hostId, [entry]);
  });
  return Array.from(groups, ([hostId, workers]) => ({
    hostId,
    // ES2022 target: sort a copy instead of mutating the feed-owned array.
    // oxlint-disable-next-line unicorn/no-array-sort
    workers: workers.slice().sort((left, right) => {
      const prefix = hostId + "-worker-";
      const leftId = String(left.instance_id || "");
      const rightId = String(right.instance_id || "");
      const leftOrdinal = leftId.startsWith(prefix) ? Number(leftId.slice(prefix.length)) : NaN;
      const rightOrdinal = rightId.startsWith(prefix) ? Number(rightId.slice(prefix.length)) : NaN;
      if (Number.isFinite(leftOrdinal) && Number.isFinite(rightOrdinal)) {
        return leftOrdinal - rightOrdinal;
      }
      return leftId.localeCompare(rightId);
    }),
  }));
}

/** Stake-weighted validator order (8190–8205): stake desc, weightless last,
 * hotkey as the deterministic tiebreak. Screeners keep feed order. */
export function sortFleetEntries(
  entries: FleetEntryExt[],
  singular: FleetSingular,
  stakes: Record<string, number>,
): FleetEntryExt[] {
  if (singular !== "validator") return entries;
  return entries.slice().sort((left, right) => {
    const leftHotkey = String(left.validator_hotkey || "");
    const rightHotkey = String(right.validator_hotkey || "");
    const leftStake = stakes[leftHotkey];
    const rightStake = stakes[rightHotkey];
    const leftHasStake = Number.isFinite(leftStake);
    const rightHasStake = Number.isFinite(rightStake);
    if (leftHasStake && rightHasStake && leftStake !== rightStake) {
      return (rightStake as number) - (leftStake as number);
    }
    if (leftHasStake !== rightHasStake) return leftHasStake ? -1 : 1;
    if (leftHotkey < rightHotkey) return -1;
    if (leftHotkey > rightHotkey) return 1;
    return 0;
  });
}

/** Worker-state chip (8207–8219). */
export function fleetWork(state: string | null | undefined, role: FleetSingular): [string, string] {
  const states: Record<string, [string, string]> = {
    polling: ["Polling", "progress"],
    running_benchmark: ["Running benchmark", "progress"],
    updating_weights: ["Updating weights", "progress"],
    screening: ["Screening", "progress"],
    idle: ["Idle", "good"],
    paused: ["Paused", "warn"],
    error: ["Error", "bad"],
  };
  const fallback: [string, string] =
    role === "screener" ? ["Waiting for work", ""] : ["Unknown", ""];
  return (state != null && states[state]) || fallback;
}

/** The bench gate names the version from the snapshot, never a literal
 * (8300–8303). */
export function benchGateLabel(version: number | null): string {
  return version ? "No bench v" + version : "Bench unsupported";
}

/** Shared fleetStatus with the versioned bench-gate label substituted for
 * the shared module's no-version fallback. Precedence is untouched:
 * Obsolete build > Scorer down > bench gate (8305–8328). */
export function fleetStatusFor(
  entry: FleetEntryExt,
  benchVersion: number | null,
): [string, string] {
  const status = fleetStatus(entry);
  if (status[0] === "Bench unsupported") return [benchGateLabel(benchVersion), "bad"];
  return status;
}

/** offlineAwareFleetStatus with the same bench-gate substitution: a badge on
 * an already-offline node only restates that it is gone unless it names a
 * real fault (8334–8338). */
export function offlineAwareFleetStatusFor(
  entry: FleetEntryExt,
  benchVersion: number | null,
): [string, string] {
  const status = offlineAwareFleetStatus(entry);
  if (status[0] === "Bench unsupported") return [benchGateLabel(benchVersion), "bad"];
  return status;
}

export const FLEET_LEDGER_KEYS = [
  "healthy",
  "critical",
  "warning",
  "stale",
  "offline",
  "paused",
  "unknown",
] as const;

export type FleetLedgerKey = (typeof FLEET_LEDGER_KEYS)[number];

/** Ledger buckets (updateFleetLedger 8845–8859). An explicit operator pause is
 * counted first because it is the authoritative dispatch state. Otherwise,
 * critical is counted before liveness: a validator whose scorer is not serving
 * is broken whether or not it is also quiet. */
export function fleetLedgerCounts(entries: FleetEntryExt[]): Record<FleetLedgerKey, number> {
  const counts: Record<FleetLedgerKey, number> = {
    healthy: 0,
    critical: 0,
    warning: 0,
    stale: 0,
    offline: 0,
    paused: 0,
    unknown: 0,
  };
  entries.forEach((entry) => {
    if (entry.issuance_paused || entry.availability === "paused") counts.paused++;
    else if (entry.health === "critical") counts.critical++;
    else if (entry.assignment_state === "assignment_mismatch") counts.warning++;
    else if (entry.availability === "stale") counts.stale++;
    else if (entry.availability === "offline") counts.offline++;
    else if (entry.health === "warning") counts.warning++;
    else if (entry.health === "healthy") counts.healthy++;
    else counts.unknown++;
  });
  return counts;
}

/** "Offline" is a window, not a constant: read it from the snapshot the rows
 * were classified with rather than restating 15 minutes in copy (8873–8880). */
export function fleetWindowLabel(seconds: unknown): string {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "the offline window";
  if (total < 3600) return Math.max(1, Math.round(total / 60)) + "m";
  const hours = Math.floor(total / 3600);
  const minutes = Math.round((total % 3600) / 60);
  return hours + "h" + (minutes ? " " + minutes + "m" : "");
}

/** Two ways to be inoperative, and both fold away (8882–8884): no heartbeat
 * inside the offline window, or software that cannot describe the benchmark
 * being scored. A CURRENT validator whose scorer broke is deliberately NOT
 * folded — that is a live incident and belongs in the open table. */
export function isInoperativeFleetEntry(entry: FleetEntryExt): boolean {
  return entry.availability === "offline" || entry.bench_serviceability === "software_obsolete";
}

/** Every slot a validator could show work on, ordered by ordinal
 * (8395–8416). The union matters: an orphaned slot reaches none of the other
 * lists, and omitting it is what let a host still executing a benchmark
 * render as a full row of idle slots. */
export function validatorSlotIds(entry: FleetEntryExt): string[] {
  const seen: Record<string, boolean> = {};
  const configured = Math.max(1, Number(entry.configured_slots) || 1);
  for (let index = 0; index < configured; index += 1) seen["slot-" + index] = true;
  (entry.healthy_slots || []).forEach((slotId) => {
    if (slotId) seen[slotId] = true;
  });
  [entry.active_benchmarks || [], entry.assigned_benchmarks || []].forEach((list) => {
    list.forEach((benchmark) => {
      if (benchmark && benchmark.slot_id) seen[benchmark.slot_id] = true;
    });
  });
  (entry.orphaned_slots || []).forEach((orphan) => {
    if (orphan && orphan.slot_id) seen[orphan.slot_id] = true;
  });
  return Object.keys(seen).sort((a, b) => slotOrdinal(a) - slotOrdinal(b));
}

/** Whether any slot line will be drawn for this validator — a leased or
 * assigned benchmark, or an orphaned lease. Read by the work cell's status
 * rail so "No active work" is stated once, beside the worker state, rather
 * than a second time from inside the slot list. */
export function hasVisibleSlotWork(entry: FleetEntryExt): boolean {
  return Boolean(
    (entry.active_benchmarks || []).length ||
    (entry.assigned_benchmarks || []).length ||
    (entry.orphaned_slots || []).some((orphan) => orphan && orphan.slot_id),
  );
}

/** Slot order is numeric, not lexical: "slot-10" comes after "slot-9". An id
 * that carries no ordinal (a missing `slot_id`, a scheme this build does not
 * know) sorts last rather than crashing or claiming slot zero — the wire type
 * makes `slot_id` optional, so the parameter is as wide as the payload
 * (8768–8771). */
export function slotOrdinal(slotId: string | null | undefined): number {
  const parsed = parseInt(String(slotId).replace(/^slot-/, ""), 10);
  return isNaN(parsed) ? Number.MAX_SAFE_INTEGER : parsed;
}

export interface OrphanedSlotViewModel {
  label: string;
  detail: string;
  agentId: string;
  agentLabel: string;
}

/** The two orphan states are deliberately never collapsed: "still running"
 * is the validator's own signed claim, "state unknown" is the honest answer
 * for a heartbeat protocol that omits a claimed-but-quiet slot (8433–8453). */
export function orphanedSlotView(orphan: OrphanedSlot): OrphanedSlotViewModel {
  const age = relDuration(orphan.orphaned_for_seconds);
  const agent = (orphan.agent_name || "Agent") + " · " + String(orphan.agent_id || "").slice(0, 8);
  const selfTerminates = orphan.original_deadline
    ? " Expected to self-terminate by its original deadline (" +
      relTimeUntil(orphan.original_deadline) +
      ")."
    : "";
  const detail =
    orphan.state === "still_running"
      ? "The validator still reports this slot occupied by " +
        agent +
        ", " +
        age +
        " after the platform evicted the lease. The run cannot produce a score — its result " +
        "will be refused with a 409." +
        selfTerminates +
        " This slot is not free."
      : "The lease was evicted " +
        age +
        " ago and the platform cannot tell whether the container is still running. " +
        "Heartbeat protocol " +
        String(orphan.protocol_version || "unknown") +
        " omits a claimed-but-quiet slot, so silence here is not evidence the slot is free (" +
        orphan.reason +
        ")." +
        selfTerminates +
        " Do not count this slot as headroom.";
  const label =
    orphan.state === "still_running"
      ? "Evicted · still running · " + age
      : "Evicted · state unknown · " + age;
  return { label, detail, agentId: String(orphan.agent_id || ""), agentLabel: agent };
}

/** A validator's orphaned slots in slot order (9068–9070). The modal lists
 * them ABOVE the running slots deliberately, so the ordering is part of the
 * contract rather than an accident of payload order. */
export function orphanedSlotsInOrder(entry: FleetEntryExt): OrphanedSlot[] {
  return (entry.orphaned_slots || [])
    .slice()
    .sort((left, right) => slotOrdinal(left.slot_id) - slotOrdinal(right.slot_id));
}

/**
 * Slots the operator cap will not hand a ticket to, keyed by slot id (#540,
 * 8468–8493). A validator advertises its own capacity; `allowed_slots` is
 * how much of it the platform funds. The cap counts concurrent LEASES, not
 * slot ordinals: running slots are charged first and the leftover idle ones
 * are marked from the highest ordinal down. Live work is never marked, and
 * an unhealthy slot is left alone — it is not the cap keeping work off it.
 */
export function cappedSlotIds(entry: FleetEntryExt): Record<string, boolean> {
  const allowed = Number(entry.allowed_slots);
  // A payload without the field predates it; saying nothing beats guessing.
  if (!isFinite(allowed)) return {};
  const busy: Record<string, boolean> = {};
  [entry.active_benchmarks || [], entry.assigned_benchmarks || []].forEach((list) => {
    list.forEach((benchmark) => {
      if (benchmark && benchmark.slot_id) busy[benchmark.slot_id] = true;
    });
  });
  const healthy: Record<string, boolean> = {};
  (entry.healthy_slots || []).forEach((slotId) => {
    if (slotId) healthy[slotId] = true;
  });
  let budget = Math.max(0, allowed - Object.keys(busy).length);
  const capped: Record<string, boolean> = {};
  validatorSlotIds(entry).forEach((slotId) => {
    if (busy[slotId] || !healthy[slotId]) return;
    if (budget > 0) {
      budget -= 1;
      return;
    }
    capped[slotId] = true;
  });
  return capped;
}

/** Slots that can take work right now: the operator cap and the validator's
 * own health, whichever binds first (8498–8502). */
export function fundedSlotCount(entry: FleetEntryExt): number {
  const healthy = (entry.healthy_slots || []).length;
  const allowed = Number(entry.allowed_slots);
  return isFinite(allowed) ? Math.min(allowed, healthy) : healthy;
}

/** Why the numerator is what it is, spelled out for the tooltip (8505–8517). */
export function slotCapacityTitle(entry: FleetEntryExt, slotPolicy: SlotPolicy | null): string {
  const parts = [
    String(entry.configured_slots || 1) + " advertised",
    String((entry.healthy_slots || []).length) + " healthy",
  ];
  if (isFinite(Number(entry.allowed_slots))) {
    parts.push(String(entry.allowed_slots) + " funded by the operator cap");
  }
  if (slotPolicy && isFinite(Number(slotPolicy.max_concurrent_slots))) {
    parts.push("fleet cap " + String(slotPolicy.max_concurrent_slots) + " per validator");
  }
  return parts.join(" · ");
}

export interface AssignmentAgentRef {
  id: string;
  label: string;
}

export interface AssignmentDetailLine {
  /** "Platform" / "Heartbeat" / "Platform assignment" — rendered bold.
   * Absent on the grace line, which is one plain sentence. */
  heading?: string;
  agent: AssignmentAgentRef | null;
  /** Copy when no agent id is on that side. */
  fallback: string;
  suffix: string;
}

export interface AssignmentView {
  label: string;
  tone: string;
  lines: AssignmentDetailLine[];
}

/**
 * Platform/heartbeat assignment skew (renderValidatorAssignment 8363–8389).
 * A mismatch names BOTH sides — Platform (what the platform assigned) and
 * Heartbeat (what the validator reports running) — so the skew is readable
 * rather than flattened to a bare warning; "assigning" is a normal hand-off;
 * a stale heartbeat names the platform's side only, because the validator's
 * side is exactly what went quiet. Null when the assignment is reconciled.
 */
export function validatorAssignmentView(entry: FleetEntryExt): AssignmentView | null {
  const assigned: AssignmentAgentRef | null = entry.assigned_agent_id
    ? {
        id: String(entry.assigned_agent_id),
        label:
          (entry.assigned_agent_name || "Agent") +
          " · " +
          String(entry.assigned_agent_id).slice(0, 8),
      }
    : null;
  if (entry.assignment_state === "assignment_mismatch" && entry._telemetry_grace) {
    return {
      label: "Telemetry delayed",
      tone: "warn",
      lines: [
        {
          agent: null,
          fallback:
            "Waiting for the next signed slot update; the last reported progress is retained briefly.",
          suffix: "",
        },
      ],
    };
  }
  if (entry.assignment_state === "assignment_mismatch") {
    const reported: AssignmentAgentRef | null = entry.reported_agent_id
      ? {
          id: String(entry.reported_agent_id),
          label: "Agent · " + String(entry.reported_agent_id).slice(0, 8),
        }
      : null;
    return {
      label: "Assignment mismatch",
      tone: "bad",
      lines: [
        { heading: "Platform", agent: assigned, fallback: "No active assignment", suffix: "" },
        { heading: "Heartbeat", agent: reported, fallback: "No active agent", suffix: "" },
      ],
    };
  }
  if (entry.assignment_state === "assigning") {
    return {
      label: "Assigning",
      tone: "progress",
      lines: [
        {
          heading: "Platform assignment",
          agent: assigned,
          fallback: "Agent",
          suffix: " · handing off",
        },
      ],
    };
  }
  if (entry.assignment_state === "heartbeat_stale") {
    return {
      label: "Heartbeat stale",
      tone: "warn",
      lines: [{ heading: "Platform assignment", agent: assigned, fallback: "Unknown", suffix: "" }],
    };
  }
  return null;
}

/** True when ANY slot is reporting a stage, not just the lowest one — keying
 * off a single slot hid live progress whenever slot zero was idle
 * (8521–8527). */
export function anyBenchmarkStage(entry: FleetEntryExt): boolean {
  const benchmarks = entry.active_benchmarks || [];
  for (let index = 0; index < benchmarks.length; index += 1) {
    if (benchmarks[index] && benchmarks[index]?.stage) return true;
  }
  return !!(entry.active_benchmark && entry.active_benchmark.stage);
}

// ── Validator stack identity, capabilities and per-component health
// (STACK_COMPONENT_LABELS 8881–8901, yesNo 8927–8931, identityRows 8933–8940,
// identityComparisonNote 8942–8957, renderScorerBenchmarks 8992–9011,
// renderScorerProbe 9013–9035) ──────────────────────────────────────────────

/** Human labels for the components a validator's stack reports (8881–8888). */
export const STACK_COMPONENT_LABELS: Record<string, string> = {
  ditto_subnet: "Validator worker",
  dittobench_api: "Scorer · dittobench-api",
  sandbox_docker: "Sandbox Docker",
  model_relay: "Model relay",
  pylon: "Pylon",
  ollama: "Ollama",
};

/** Render order (8889), fixed rather than payload order: a component one
 * heartbeat omits must not shuffle the rows of the next. */
export const STACK_COMPONENT_ORDER: readonly string[] = [
  "ditto_subnet",
  "dittobench_api",
  "sandbox_docker",
  "model_relay",
  "pylon",
  "ollama",
];

const COMPONENT_HEALTH_CHIPS: Record<string, [string, string]> = {
  healthy: ["Healthy", "good"],
  degraded: ["Degraded", "warn"],
  unreachable: ["Unreachable", "bad"],
  identity_mismatch: ["Identity mismatch", "bad"],
  unknown: ["Not observed", "unknown"],
};

/** Per-component health chip (8890–8901). A component the validator reported
 * as `unknown` reads "Not observed" — it answered, it just has not probed;
 * one the heartbeat omits entirely reads "Unknown". The two are not the same
 * claim and are deliberately not collapsed. */
export function componentHealthChip(state: string | null | undefined): [string, string] {
  return (state != null && COMPONENT_HEALTH_CHIPS[state]) || ["Unknown", "unknown"];
}

/** Tri-state boolean copy (8927–8931): a capability the heartbeat omits is
 * "Not reported", never a silent "No". */
export function yesNo(value: boolean | null | undefined): string {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return "Not reported";
}

/** True when an identity pins at least one field worth a row (8933–8940). */
export function hasIdentityRows(identity: StackIdentity | null | undefined): boolean {
  return Boolean(
    identity && (identity.image_digest || identity.source_revision || identity.version),
  );
}

export interface IdentityNote {
  tone: "good" | "warn";
  text: string;
}

/** The first identity field both sides pin, compared (8942–8957). Null when
 * either side has nothing comparable: an absent pin is not a mismatch, and
 * claiming one would be the dashboard inventing a fault. */
export function identityComparisonNote(
  configured: StackIdentity | null | undefined,
  observed: StackIdentity | null | undefined,
): IdentityNote | null {
  if (!configured || !observed) return null;
  const checks: Array<[keyof StackIdentity, string]> = [
    ["image_digest", "image digest"],
    ["source_revision", "source revision"],
  ];
  for (const [key, label] of checks) {
    if (configured[key] && observed[key]) {
      return configured[key] === observed[key]
        ? { tone: "good", text: "Observed " + label + " matches the configured pin." }
        : { tone: "warn", text: "Observed " + label + " differs from the configured pin." };
    }
  }
  return null;
}

/** Stack provenance in one phrase (9119). Anything that is not the signed
 * managed release is a source build. */
export function stackModeLabel(mode: string | null | undefined): string {
  return mode === "managed" ? "Managed (signed GHCR release)" : "Source build";
}

/** The validator's verdict on its own scorer (8992–9000). */
export function scorerStatusChip(status: string | null | undefined): [string, string] {
  const chips: Record<string, [string, string]> = {
    fresh_verified: ["Fresh, identity-verified", "good"],
    legacy_v2: ["Legacy scorer (v2 only)", "warn"],
    unreachable: ["Scorer unreachable", "bad"],
    identity_mismatch: ["Scorer identity mismatch", "bad"],
  };
  return (status != null && chips[status]) || [String(status), "unknown"];
}

/** What the probe saw, which is a different question from the verdict above
 * (9014–9022): "answered with something unusable" is not "never answered". */
export function scorerProbeChip(outcome: string | null | undefined): [string, string] {
  const chips: Record<string, [string, string]> = {
    served: ["Serving", "good"],
    served_degraded: ["Serving, reply partly rejected", "warn"],
    http_error: ["No usable answer", "bad"],
    unreadable: ["Answered, reply unusable", "bad"],
    timeout: ["Probe timed out", "bad"],
    connect_error: ["Connection failed", "bad"],
    not_probed: ["Not probed", "unknown"],
  };
  return (outcome != null && chips[outcome]) || [String(outcome), "unknown"];
}

/** The " · HTTP 404 · 3284 in a row" tail appended after the probe chip
 * (9023–9026). Empty when the probe carries no detail. */
export function scorerProbeDetail(probe: ScorerProbe): string {
  let detail = "";
  if (probe.http_status != null) detail += " · HTTP " + probe.http_status;
  if (probe.reason) detail += " · " + probe.reason;
  if (probe.consecutive_failures) detail += " · " + probe.consecutive_failures + " in a row";
  return detail;
}

// ── Transient validator telemetry grace (weekend drift; monolith 3155–3164,
// preserveTransientValidatorTelemetry 8458–8532) ────────────────────────────

/** A validator heartbeat and the platform lease projection are separate
 * writes: preserve the last signed per-slot progress through a few fast
 * operations polls so a one-frame gap reads as delayed telemetry rather than
 * a fleet-wide assignment failure. Persistent mismatches still turn red when
 * this short grace expires. */
export const VALIDATOR_TELEMETRY_GRACE_MS = 20000;

interface SlotTelemetryPrior {
  progress: BenchmarkProgress;
  seenAt: number;
}
export type ValidatorTelemetryCache = Record<string, Record<string, SlotTelemetryPrior>>;

const defaultTelemetryCache: ValidatorTelemetryCache = Object.create(null) as never;

export function hasBenchmarkTelemetry(progress: BenchmarkProgress | null | undefined): boolean {
  return Boolean(
    progress && (progress.stage || progress.percent != null || progress.completed_checks != null),
  );
}

function sameBenchmarkAssignment(
  left: BenchmarkProgress | null | undefined,
  right: BenchmarkProgress | null | undefined,
): boolean {
  return String(left?.agent_id || "") === String(right?.agent_id || "");
}

/** The slice of a fleet entry the grace logic reads and writes. */
interface TelemetryCarrier {
  validator_hotkey?: string | null;
  active_benchmarks?: BenchmarkProgress[];
  assigned_benchmarks?: BenchmarkProgress[];
  _telemetry_grace?: boolean;
}

/** Mutates the report's validator entries the way the monolith did: empty
 * slots whose prior signed progress is fresher than the grace window get that
 * progress back, stamped `_telemetry_delayed`. The cache self-evicts slots
 * whose assignment changed, aged out, or whose validator left the report. */
export function preserveTransientValidatorTelemetry<
  R extends { validators?: TelemetryCarrier[] } | null | undefined,
>(report: R, nowMs: number, cache: ValidatorTelemetryCache = defaultTelemetryCache): R {
  if (!report || !Array.isArray(report.validators)) return report;
  const presentValidators: Record<string, boolean> = Object.create(null) as never;
  report.validators.forEach((entry: TelemetryCarrier) => {
    const hotkey = String(entry.validator_hotkey || "");
    if (!hotkey) return;
    presentValidators[hotkey] = true;
    const slots = cache[hotkey] ?? (Object.create(null) as Record<string, SlotTelemetryPrior>);
    cache[hotkey] = slots;
    const active = Array.isArray(entry.active_benchmarks) ? entry.active_benchmarks : [];
    const assignments = Array.isArray(entry.assigned_benchmarks) ? entry.assigned_benchmarks : [];
    const expectedBySlot: Record<string, BenchmarkProgress> = Object.create(null) as never;
    assignments.concat(active).forEach((progress) => {
      if (progress) expectedBySlot[String(progress.slot_id || "slot-0")] = progress;
    });
    const normalized: BenchmarkProgress[] = [];
    const renderedSlots: Record<string, boolean> = Object.create(null) as never;
    let injected = false;

    active.forEach((progress) => {
      const slotId = String(progress.slot_id || "slot-0");
      const prior = slots[slotId];
      renderedSlots[slotId] = true;
      if (hasBenchmarkTelemetry(progress)) {
        slots[slotId] = { progress: { ...progress }, seenAt: nowMs };
        normalized.push(progress);
      } else if (
        prior &&
        sameBenchmarkAssignment(prior.progress, progress) &&
        nowMs - prior.seenAt <= VALIDATOR_TELEMETRY_GRACE_MS
      ) {
        normalized.push({
          ...prior.progress,
          _telemetry_delayed: true,
          _telemetry_delayed_at: new Date(prior.seenAt).toISOString(),
        });
        injected = true;
      } else {
        normalized.push(progress);
      }
    });

    assignments.forEach((assignment) => {
      const slotId = String(assignment.slot_id || "slot-0");
      if (renderedSlots[slotId]) return;
      const prior = slots[slotId];
      if (
        prior &&
        sameBenchmarkAssignment(prior.progress, assignment) &&
        nowMs - prior.seenAt <= VALIDATOR_TELEMETRY_GRACE_MS
      ) {
        normalized.push({
          ...prior.progress,
          _telemetry_delayed: true,
          _telemetry_delayed_at: new Date(prior.seenAt).toISOString(),
        });
        renderedSlots[slotId] = true;
        injected = true;
      }
    });

    Object.keys(slots).forEach((slotId) => {
      const prior = slots[slotId] as SlotTelemetryPrior;
      const expected = expectedBySlot[slotId];
      if (
        !expected ||
        !sameBenchmarkAssignment(prior.progress, expected) ||
        nowMs - prior.seenAt > VALIDATOR_TELEMETRY_GRACE_MS
      ) {
        delete slots[slotId];
      }
    });
    entry.active_benchmarks = normalized;
    entry._telemetry_grace = injected;
  });
  Object.keys(cache).forEach((hotkey) => {
    if (!presentValidators[hotkey]) delete cache[hotkey];
  });
  return report;
}
