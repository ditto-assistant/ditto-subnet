// Fleet table rendering (monolith renderFleet 8922–9114, renderRetiredFleetRow
// 8886–8920, host telemetry 8346–8361, slot cell 9030–9070,
// updateFleetLedger/resetFleetLedger 8845–8869, row activation 9116–9137).
// Every number here comes off the one shared operations snapshot (or the
// screener feed) handed down by the page — no panel refetches.
//
// Three columns, not ten: who the node is and whether it can take work; what
// it is working on; what it is running on. Everything the fleet view does not
// route on — first seen, heartbeat protocol, container counts, updater
// history — lives one click deep in the validator drill-down.
import { For, Show, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import { pct, relTime, shortKey } from "../../lib/format";
import type { ValidatorWeightView } from "../../lib/scoring";
import { pushEntityRoute } from "../../stores/routeStore";
import type { ConfirmationProgress, FleetEntry, SystemMetrics } from "../../types/fleet";
import type { BenchmarkProgress } from "../../types/pipeline";
import { CopyButton } from "../shell/CopyButton";
import { EntityButton } from "../ui/EntityButton";
import {
  FLEET_LEDGER_KEYS,
  anyBenchmarkStage,
  cappedSlotIds,
  fleetStatusFor,
  fleetWork,
  fundedSlotCount,
  hasVisibleSlotWork,
  offlineAwareFleetStatusFor,
  orphanedSlotView,
  slotCapacityTitle,
  validatorAssignmentView,
  validatorSlotIds,
} from "./fleet";
import type { FleetEntryExt, FleetLedgerKey, FleetSingular, SlotPolicy } from "./fleet";
import {
  ElapsedTime,
  ScreenerProgressView,
  benchmarkProgressText,
  benchmarkStageLabel,
} from "./progress";
import { updaterModeLine, updaterView } from "./updater";

const LEDGER_LABELS: Record<FleetLedgerKey, string> = {
  healthy: "Healthy",
  critical: "Critical",
  warning: "Warning",
  stale: "Stale",
  offline: "Offline",
  paused: "Paused",
  unknown: "Not reported",
};

/** Fleet status summary rail. Null counts render dashes (resetFleetLedger). */
export function FleetLedger(props: { counts: Record<FleetLedgerKey, number> | null }): JSX.Element {
  return (
    <aside class="fleet-ledger" aria-label="Fleet status summary">
      <For each={FLEET_LEDGER_KEYS as readonly FleetLedgerKey[]}>
        {(key) => (
          <div class={"fleet-ledger-row " + key}>
            <span class="fleet-ledger-dot" aria-hidden="true" />
            <strong id={"fleet-count-" + key}>
              {props.counts ? String(props.counts[key]) : "–"}
            </strong>
            <span>{LEDGER_LABELS[key]}</span>
          </div>
        )}
      </For>
      <p>
        Missing optional telemetry is not an outage. Software that cannot serve the scored benchmark
        counts as critical: the platform leases it no work.
      </p>
    </aside>
  );
}

function stageClass(tone: string): string {
  return "stage" + (tone ? " " + tone : "");
}

function SlotLabel(props: { slotId: string }): JSX.Element {
  return (
    <span class="fleet-slot-id" title={props.slotId}>
      {slotIndexLabel(props.slotId)}
    </span>
  );
}

function confirmationSubjectLabel(work: ConfirmationProgress): string {
  return work.subjects.length === 1 ? "1 subject" : String(work.subjects.length) + " subjects";
}

function confirmationStageLabel(work: ConfirmationProgress): string {
  switch (work.stage) {
    case "preparing":
      return "Preparing";
    case "running_confirmation":
      return "Running LongMemEval";
    case "finalizing":
      return "Finalizing evidence";
    case "submitting_result":
      return "Submitting result";
    case "failed_retrying":
      return "Returning for retry";
    default:
      return "Progress not reported";
  }
}

function hotkeyOf(entry: FleetEntryExt, singular: FleetSingular): string {
  return (singular === "validator" ? entry.validator_hotkey : entry.screener_hotkey) || "Unknown";
}

function versionLabel(entry: FleetEntryExt): string {
  const version = String(entry.software_version || "Unknown");
  if (version !== "Unknown" && version.charAt(0).toLowerCase() !== "v") return "v" + version;
  return version;
}

/** What the node can be given, in the terms that decide it: slots read
 * "funded of advertised", not "healthy of advertised", because the operator
 * cap is the number that gates a lease. Heartbeat protocol lives in the
 * drill-down — it settles a support question, never a dispatch one. */
function capacityLine(entry: FleetEntryExt, singular: FleetSingular): string {
  if (singular === "screener") return "Policy " + entry.policy_version;
  return (
    String(fundedSlotCount(entry)) +
    " of " +
    String(entry.configured_slots || 1) +
    " slots · " +
    String(entry.admission || "accepting")
  );
}

/** The identity cell, which now also carries the fleet verdict: a toned dot
 * beside the name answers "can this node take work" at a glance, and the
 * spelled status keeps that answer readable without color. Heartbeat age
 * rides the same line — it qualifies the verdict rather than standing alone
 * in a column of its own. */
function FleetIdentity(props: {
  entry: FleetEntryExt;
  singular: FleetSingular;
  names: Record<string, string>;
  status: [string, string];
  /** Retired screeners name themselves "Unknown screener" (8890). */
  screenerFallback?: string;
}): JSX.Element {
  const hotkey = () => hotkeyOf(props.entry, props.singular);
  const reportedAt = () => props.entry.seen_at || props.entry.reported_at;
  // Validators are keyed by a distinct hotkey; the screener fleet shares one
  // hotkey, so each worker is distinguished by its instance_id.
  const displayName = () =>
    props.singular === "validator"
      ? props.names[hotkey()] || ""
      : props.entry.instance_id || props.screenerFallback || "";
  const tone = () => props.status[1] || "unknown";
  const keyNode = (): JSX.Element => (
    <>
      <span>
        <EntityButton kind={props.singular} id={hotkey()} label={shortKey(hotkey())} />
      </span>
      <CopyButton value={hotkey()} label={props.singular + " hotkey"} />
    </>
  );
  return (
    <span class="fleet-node-wrap">
      <span class={"fleet-node-dot " + tone()} aria-hidden="true" />
      <span class="fleet-node-identity">
        <Show when={displayName()}>
          {(name) => (
            <span class="fleet-node-name" title={name()}>
              <Show when={props.singular === "validator"} fallback={name()}>
                <EntityButton kind="validator" id={hotkey()} label={name()} />
              </Show>
            </span>
          )}
        </Show>
        <span class="fleet-node fleet-node-key copyable" title={hotkey()}>
          {keyNode()}
        </span>
        <span class="fleet-node-state">
          <span class={"fleet-node-status " + tone()}>{props.status[0]}</span>
          <span class="fleet-node-state-sep" aria-hidden="true">
            ·
          </span>
          <span class="fleet-time" title={reportedAt() || ""}>
            {relTime(reportedAt())}
          </span>
        </span>
      </span>
    </span>
  );
}

/**
 * Host load as one small column chart rather than three columns of the
 * table: CPU, memory and disk share a baseline and a ceiling, so the tallest
 * bar is the answer to "what is this host short of" without reading a single
 * number. Only memory ≥ 90 and disk ≥ 95 warn — CPU saturation during a
 * benchmark is the machine doing its job, not an incident (8346–8361).
 */
function ResourceChart(props: { metrics: SystemMetrics }): JSX.Element {
  const clamp = (value: number): number =>
    Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  const bars = createMemo(() => [
    { label: "CPU", value: clamp(props.metrics.cpu_percent), warn: false },
    {
      label: "MEM",
      value: clamp(props.metrics.memory_percent),
      warn: props.metrics.memory_percent >= 90,
    },
    {
      label: "DISK",
      value: clamp(props.metrics.disk_percent),
      warn: props.metrics.disk_percent >= 95,
    },
  ]);
  const sentence = () =>
    "Host load · " +
    bars()
      .map((bar) => bar.label + " " + bar.value + "%")
      .join(" · ");
  return (
    <div class="fleet-resources" role="img" aria-label={sentence()} title={sentence()}>
      <For each={bars()}>
        {(bar) => (
          <div class={"fleet-resource" + (bar.warn ? " warn" : "")}>
            <span class="fleet-resource-value">{bar.value}%</span>
            <span class="fleet-resource-track" aria-hidden="true">
              <i style={{ "--value": String(bar.value) }} />
            </span>
            <span class="fleet-resource-label">{bar.label}</span>
          </div>
        )}
      </For>
    </div>
  );
}

/** The host cell: what this node is running, what it may be given, and what
 * it has left. Container counts and heartbeat protocol are in the drill-down
 * — neither changes what the fleet does next. */
export function FleetHostCell(props: {
  entry: FleetEntryExt;
  singular: FleetSingular;
  slotPolicy: SlotPolicy | null;
}): JSX.Element {
  return (
    <td class="fleet-host-cell">
      <div class="fleet-host">
        <span class="fleet-version">{versionLabel(props.entry)}</span>
        <span
          class="fleet-protocol"
          title={
            props.singular === "validator"
              ? slotCapacityTitle(props.entry, props.slotPolicy)
              : "Screening policy version"
          }
        >
          {capacityLine(props.entry, props.singular)}
        </span>
        <Show when={props.singular === "validator" ? updaterModeLine(props.entry) : null}>
          {(mode) => <span class="fleet-updater-mode">{mode()}</span>}
        </Show>
        <Show
          when={props.entry.system_metrics}
          fallback={<span class="fleet-unreported">Host load not reported</span>}
        >
          {(metrics) => <ResourceChart metrics={metrics()} />}
        </Show>
      </div>
    </td>
  );
}

/** Platform/heartbeat skew detail (renderValidatorAssignment 8363–8389). */
export function AssignmentDetail(props: { entry: FleetEntryExt }): JSX.Element {
  const view = () => validatorAssignmentView(props.entry);
  return (
    <Show when={view()}>
      {(assignment) => (
        <>
          <span class={stageClass(assignment().tone)}>{assignment().label}</span>
          <span class="assignment-detail">
            <For each={assignment().lines}>
              {(line, index) => (
                <>
                  <Show when={index() > 0}>
                    <br />
                  </Show>
                  <Show when={line.heading}>
                    <b>{line.heading}</b>{" "}
                  </Show>
                  <Show when={line.agent} fallback={line.fallback}>
                    {(agent) => <EntityButton kind="agent" id={agent().id} label={agent().label} />}
                  </Show>
                  {line.suffix}
                </>
              )}
            </For>
          </span>
        </>
      )}
    </Show>
  );
}

/** Compact stage vocabulary for the one-line slot rows. running_benchmark is
 * deliberately absent: once a percentage exists, the bar and the numbers say
 * it, and the full label stays in the tooltip and the bar's aria-label. */
const SLOT_STAGE_SHORT: Record<string, string> = {
  preparing: "preparing",
  building_harness: "loading image",
  generating_dataset: "generating dataset",
  starting_harness: "starting harness",
  running_benchmark: "benchmark",
  finalizing: "finalizing",
  submitting_result: "submitting",
  waiting_for_relay: "waiting for relay",
};

/** `slot-3` → `3`. The ordinal is the only part an operator reads; the full
 * id stays on the tooltip and on data-slot for anything that keys off it. */
function slotIndexLabel(slotId: string): string {
  const ordinal = /(\d+)\s*$/.exec(slotId);
  return ordinal ? ordinal[1]! : slotId;
}

/**
 * One running slot as a single ledger line: ordinal, bench chip, agent, live
 * bar, then the numbers. Every line shares one set of grid tracks with its
 * siblings, so a long agent name lengthens no bar and shifts no column — the
 * eight bars of a busy validator read as one chart, not eight. Nothing is
 * dropped: the full progress sentence (stage label, stall reason,
 * delayed-telemetry note) rides on the line's tooltip and the bar's
 * accessible label, and the agent's identifier on the agent's own tooltip.
 */
function SlotLine(props: { slotId: string; progress: BenchmarkProgress }): JSX.Element {
  const p = () => props.progress;
  const stalled = () => Boolean(p().stalled);
  const failed = () => p().stage === "failed_retrying";
  const delayed = () => Boolean(p()._telemetry_delayed);
  const determinate = () => p().percent != null && !stalled() && !failed();
  const counts = () =>
    p().completed_checks != null && p().total_checks != null
      ? String(p().completed_checks) + "/" + String(p().total_checks)
      : "";
  const value = () => Math.max(0, Math.min(100, Number(p().percent) || 0));
  const sentence = () =>
    props.slotId +
    ". " +
    (p().bench_version ? "Bench v" + p().bench_version + ". " : "") +
    benchmarkStageLabel(p().stage) +
    ". " +
    benchmarkProgressText(p());
  // Only what the bar itself cannot say. A running benchmark is silent here:
  // the fill and the counts already state it.
  const note = createMemo(() => {
    if (delayed()) return "update delayed";
    if (failed()) return "failed · retrying";
    if (stalled()) return "stalled";
    if (p().stage && p().stage !== "running_benchmark") {
      return SLOT_STAGE_SHORT[p().stage as string] || "working";
    }
    if (!p().stage) return "awaiting progress";
    return "";
  });
  return (
    <div
      class="fleet-slot-line"
      classList={{ warn: stalled() || failed() }}
      title={sentence()}
      data-slot={props.slotId}
    >
      <span class="fleet-slot-id" title={props.slotId}>
        {slotIndexLabel(props.slotId)}
      </span>
      <span class="fleet-slot-bench">
        <Show when={p().bench_version}>
          {(version) => (
            <span class="benchmark-version-chip" title={"Bench v" + version()}>
              v{version()}
            </span>
          )}
        </Show>
      </span>
      <span class="fleet-slot-agent" title={String(p().agent_id || "")}>
        <EntityButton kind="agent" id={p().agent_id} label={p().agent_name || "Unnamed agent"} />
      </span>
      <span class="fleet-slot-bar">
        <Show
          when={determinate()}
          fallback={
            <span
              class="bench-bar"
              classList={{ indeterminate: !stalled() && !failed() && !delayed() }}
              role="img"
              aria-label={sentence()}
            >
              <i />
            </span>
          }
        >
          <progress max="100" value={value()} aria-label={sentence()} />
        </Show>
      </span>
      <span class="fleet-slot-note">{note()}</span>
      <span class="fleet-slot-pct">{p().percent != null ? String(p().percent) + "%" : ""}</span>
      <span class="fleet-slot-count">{counts()}</span>
      <span class="fleet-slot-elapsed-cell">
        <Show when={p().started_at}>
          {(startedAt) => <ElapsedTime class="fleet-slot-elapsed" startedAt={startedAt()} />}
        </Show>
      </span>
    </div>
  );
}

/** One benchmark slot's state line. Order matters: real leased work outranks
 * an orphan record, and an orphan outranks every free-slot reading below it —
 * "Idle" and "Unavailable" are the two claims the eviction window makes
 * false, so nothing may reach them while an orphan is on the slot (9049–9067). */
function SlotRows(props: { entry: FleetEntryExt; slotPolicy: SlotPolicy | null }): JSX.Element {
  const bySlot = createMemo(() => {
    const active: Record<string, BenchmarkProgress> = {};
    (props.entry.active_benchmarks || []).forEach((benchmark) => {
      active[benchmark.slot_id || "slot-0"] = benchmark;
    });
    const assigned: Record<string, BenchmarkProgress> = {};
    (props.entry.assigned_benchmarks || []).forEach((benchmark) => {
      assigned[benchmark.slot_id || "slot-0"] = benchmark;
    });
    const orphans: Record<string, ReturnType<typeof orphanedSlotView>> = {};
    (props.entry.orphaned_slots || []).forEach((orphan) => {
      if (orphan && orphan.slot_id) orphans[orphan.slot_id] = orphanedSlotView(orphan);
    });
    return { active, assigned, orphans };
  });
  const capped = createMemo(() => cappedSlotIds(props.entry));
  const cappedTitle = () =>
    props.slotPolicy && isFinite(Number(props.slotPolicy.max_concurrent_slots))
      ? "Operator cap: " +
        String(props.slotPolicy.max_concurrent_slots) +
        " concurrent slots per validator. No ticket is issued here."
      : "Held back by the operator slot cap. No ticket is issued here.";

  const slots = createMemo(() => validatorSlotIds(props.entry));
  const benchmarkFor = (slotId: string) => bySlot().active[slotId] || bySlot().assigned[slotId];
  const orphanFor = (slotId: string) => bySlot().orphans[slotId];
  const runningSlots = createMemo(() => slots().filter((slotId) => Boolean(benchmarkFor(slotId))));
  const orphanedSlots = createMemo(() =>
    slots().filter((slotId) => !benchmarkFor(slotId) && Boolean(orphanFor(slotId))),
  );
  const inactiveSlots = createMemo(() =>
    slots().filter((slotId) => !benchmarkFor(slotId) && !orphanFor(slotId)),
  );
  const slotCounts = createMemo(() => {
    let idle = 0;
    let cappedCount = 0;
    let unavailable = 0;
    inactiveSlots().forEach((slotId) => {
      if (props.entry.admission !== "accepting") unavailable += 1;
      else if ((props.entry.healthy_slots || []).indexOf(slotId) < 0) unavailable += 1;
      else if (capped()[slotId]) cappedCount += 1;
      else idle += 1;
    });
    return {
      total: slots().length,
      running: runningSlots().length,
      attention: orphanedSlots().length,
      idle,
      capped: cappedCount,
      unavailable,
    };
  });
  const summary = () => {
    const counts = slotCounts();
    const parts = [counts.total + " slots", counts.running + " running"];
    if (counts.attention) parts.push(counts.attention + " attention");
    if (counts.idle) parts.push(counts.idle + " idle");
    if (counts.capped) parts.push(counts.capped + " capped");
    if (counts.unavailable) parts.push(counts.unavailable + " unavailable");
    return parts.join(" · ");
  };

  const inactiveState = (slotId: string): { label: string; tone: string; title?: string } => {
    if (props.entry.admission !== "accepting") {
      return { label: String(props.entry.admission), tone: "warn" };
    }
    if ((props.entry.healthy_slots || []).indexOf(slotId) < 0) {
      return { label: "Unavailable", tone: "warn" };
    }
    if (capped()[slotId]) return { label: "Capped", tone: "capped", title: cappedTitle() };
    return { label: "Idle", tone: "success" };
  };

  return (
    <div class="fleet-slot-overview">
      <For each={runningSlots()}>
        {(slotId) => <SlotLine slotId={slotId} progress={benchmarkFor(slotId)!} />}
      </For>
      <For each={orphanedSlots()}>
        {(slotId) => {
          const view = () => orphanFor(slotId);
          return (
            <div class="fleet-slot fleet-slot-attention" title={slotId}>
              <SlotLabel slotId={slotId} />
              <span class="stage warn" title={view()?.detail}>
                {view()?.label}
              </span>
              <span class="current-agent" title={view()?.agentId}>
                <EntityButton
                  kind="agent"
                  id={view()?.agentId || ""}
                  label={view()?.agentLabel || "Agent"}
                />
              </span>
            </div>
          );
        }}
      </For>
      <Show
        when={inactiveSlots().length > 0}
        fallback={<div class="fleet-slot-summary">{summary()}</div>}
      >
        <details
          class="fleet-slot-disclosure"
          onClick={(ev) => ev.stopPropagation()}
          onKeyDown={(ev) => ev.stopPropagation()}
        >
          <summary>{summary()}</summary>
          <div class="fleet-slot-details" aria-label="Inactive validator slot states">
            <For each={inactiveSlots()}>
              {(slotId) => {
                const state = () => inactiveState(slotId);
                return (
                  <div class="fleet-slot fleet-slot-inactive" title={slotId}>
                    <SlotLabel slotId={slotId} />
                    <span class={stageClass(state().tone)} title={state().title}>
                      {state().label}
                    </span>
                  </div>
                );
              }}
            </For>
          </div>
        </details>
      </Show>
    </div>
  );
}

function ConfirmationRows(props: { entry: FleetEntryExt }): JSX.Element {
  const rows = () => props.entry.confirmation_benchmarks || [];
  return (
    <Show when={rows().length > 0}>
      <section class="fleet-confirmation-lane" aria-label="LongMem confirmation work">
        <div class="fleet-confirmation-heading">
          <strong>LongMemEval</strong>
          <span>Independent confirmation lane · ablations included</span>
        </div>
        <For each={rows()}>
          {(work) => (
            <div class="fleet-confirmation-work" title={work.profile_revision}>
              <SlotLabel slotId={work.slot_id} />
              <span class={stageClass(work.mode === "enforce" ? "warn" : "success")}>
                {work.mode === "enforce" ? "Enforce" : "Shadow"}
              </span>
              <span class="fleet-time" title={work.issued_at}>
                Running {relTime(work.issued_at)}
              </span>
              <span
                class={stageClass(work.stage === "failed_retrying" ? "warn" : "")}
                title={work.progress_reported_at || "Validator heartbeat has not reported progress"}
              >
                {confirmationStageLabel(work)}
              </span>
              <Show when={work.completed !== null && work.completed !== undefined && work.total}>
                <span class="fleet-confirmation-progress-view">
                  <span class="fleet-confirmation-progress-count">
                    {work.completed || 0}/{work.total || 1}
                  </span>
                  <progress
                    class="fleet-confirmation-progress"
                    value={work.completed || 0}
                    max={work.total || 1}
                    aria-label={`${confirmationStageLabel(work)}: ${work.completed || 0} of ${work.total || 1}`}
                  />
                </span>
              </Show>
              <span class="fleet-confirmation-subjects">
                {confirmationSubjectLabel(work)}
                <Show when={work.subjects.length === 1 ? work.subjects[0] : undefined}>
                  {(subject) => (
                    <>
                      <span> · </span>
                      <EntityButton
                        kind="agent"
                        id={subject().agent_id}
                        label={subject().agent_name}
                      />
                    </>
                  )}
                </Show>
              </span>
            </div>
          )}
        </For>
      </section>
    </Show>
  );
}

/** The revealed weight matrix, per validator hotkey (OperationsPage folds
 * /public/weights once for the whole table). */
export interface FleetChainVectors {
  byValidator: Record<string, ValidatorWeightView>;
  block: number | null;
  stale: boolean;
}

/**
 * The miner UIDs this exact validator's most recently revealed on-chain
 * vector points at, heaviest first with per-vector shares — gold for its top
 * choice, magenta for the rest, sharing the leaderboard's chip vocabulary. A
 * validator the snapshot carries no vector for says so explicitly: on a
 * weight-setting fleet, "none revealed" is a finding, not missing data.
 */
function FleetChainWeights(props: {
  hotkey: string;
  chainVectors: FleetChainVectors;
}): JSX.Element {
  const vector = (): ValidatorWeightView | null =>
    props.chainVectors.byValidator[props.hotkey] ?? null;
  const blockNote = (): string =>
    (props.chainVectors.block == null
      ? ""
      : "Revealed at block " + Number(props.chainVectors.block).toLocaleString() + ". ") +
    (props.chainVectors.stale ? "Chain re-read is failing; this is the last good matrix. " : "") +
    "Commit-reveal can lag active commitments.";
  return (
    <div class="fleet-chain-weights" title={blockNote()}>
      <span class="fleet-chain-weights-label">
        On-chain weights
        <Show when={props.chainVectors.stale}>
          <span class="chain-weights-stale">stale</span>
        </Show>
      </span>
      <Show when={vector()} fallback={<span class="fleet-chain-weights-none">none revealed</span>}>
        {(view) => (
          <span class="fleet-chain-weights-set">
            <For each={view().entries}>
              {(entry) => (
                <span
                  class={"chain-vector-chip " + (entry.top ? "top-choice" : "support")}
                  title={
                    entry.hotkey +
                    " · raw u16 " +
                    entry.value +
                    " · " +
                    pct(entry.share) +
                    " of this vector's miner weight" +
                    (entry.top ? " · top choice" : "")
                  }
                >
                  <span class="chain-vector-chip-uid">UID {entry.uid}</span>
                  <span class="chain-vector-chip-share">{pct(entry.share)}</span>
                </span>
              )}
            </For>
          </span>
        )}
      </Show>
    </div>
  );
}

function UpdaterNotice(props: { entry: FleetEntryExt }): JSX.Element {
  const view = () => updaterView(props.entry);
  return (
    <Show when={view()}>
      {(status) => (
        <aside
          class="fleet-updater-notice"
          aria-label="Managed updater status"
          title={status().title}
        >
          <span class={stageClass(status().tone)}>{status().label}</span>
          <Show when={status().target}>
            {(target) => (
              <span class="fleet-updater-target" title={status().targetTitle || target()}>
                {target()}
              </span>
            )}
          </Show>
          <span class="fleet-updater-summary">{status().summary}</span>
        </aside>
      )}
    </Show>
  );
}

function rowActivation(
  singular: FleetSingular,
  hotkey: () => string,
): {
  onClick: (ev: MouseEvent) => void;
  onKeyDown: (ev: KeyboardEvent) => void;
} {
  function activate(ev: Event): void {
    const target = ev.target as HTMLElement;
    if (target.closest(".copy") || target.closest("[data-entity-link]")) return;
    // Only validators open the drill-down (activateFleetRow 9116–9120).
    if (singular !== "validator") return;
    pushEntityRoute("validator", hotkey());
  }
  return {
    onClick: activate,
    onKeyDown: (ev: KeyboardEvent) => {
      if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
      const target = ev.target as HTMLElement;
      if (target.closest(".copy") || target.closest("[data-entity-link]")) return;
      ev.preventDefault();
      activate(ev);
    },
  };
}

export interface FleetRowProps {
  entry: FleetEntryExt;
  singular: FleetSingular;
  names: Record<string, string>;
  slotPolicy: SlotPolicy | null;
  benchVersion: number | null;
  highlightId: string | null;
  /** Null while no weight snapshot has loaded (nothing renders — absence of
   * data is not "no weights"); validators only. */
  chainVectors?: FleetChainVectors | null;
}

/** One active fleet row (renderFleet 9017–9109). */
export function FleetRow(props: FleetRowProps): JSX.Element {
  const hotkey = () => hotkeyOf(props.entry, props.singular);
  const status = () => fleetStatusFor(props.entry, props.benchVersion);
  const work = createMemo<[string, string]>(() => {
    const base = fleetWork(props.entry.state, props.singular);
    return props.entry.availability === "stale" ? ["Last reported · " + base[0], "warn"] : base;
  });
  // A plain worker-state chip only when nothing more granular is available:
  // assignment skew, any slot's live benchmark stage, a screening stage.
  const hasGranularProgress = () =>
    Boolean(
      (props.singular === "validator" &&
        (validatorAssignmentView(props.entry) || anyBenchmarkStage(props.entry))) ||
      (props.singular === "screener" && props.entry.screening_progress),
    );
  // "No active work" belongs beside the worker state, not a line below it
  // inside the slot list: both answer the same question about the same cell.
  const idleSlots = () => props.singular === "validator" && !hasVisibleSlotWork(props.entry);
  const highlighted = () => props.highlightId != null && props.highlightId === hotkey();
  const activation = rowActivation(props.singular, hotkey);
  return (
    <tr
      tabindex="-1"
      data-entity-kind={props.singular}
      data-entity-id={hotkey()}
      classList={{ "entity-target": highlighted() }}
      aria-current={highlighted() ? "true" : undefined}
      onClick={activation.onClick}
      onKeyDown={activation.onKeyDown}
    >
      <td class="fleet-id-cell">
        <FleetIdentity
          entry={props.entry}
          singular={props.singular}
          names={props.names}
          status={status()}
        />
      </td>
      <td class="fleet-work-col">
        {/* One rail, one place: every chip that describes the state of this
            cell as a whole sits on this row, above the work it explains. */}
        <Show when={!hasGranularProgress() || idleSlots()}>
          <div class="fleet-work-status">
            <Show when={!hasGranularProgress()}>
              <span class={stageClass(work()[1])}>{work()[0]}</span>
            </Show>
            <Show when={idleSlots()}>
              <span class="stage unknown">No active work</span>
            </Show>
          </div>
        </Show>
        <Show
          when={props.singular === "validator"}
          fallback={
            <Show
              when={props.entry.screening_progress}
              fallback={
                <Show when={props.entry.active_agent_id}>
                  {(agentId) => (
                    <span class="current-agent" title={agentId()}>
                      <EntityButton
                        kind="agent"
                        id={agentId()}
                        label={
                          (props.entry.active_agent_name || "Agent") +
                          " · " +
                          String(agentId()).slice(0, 8)
                        }
                      />
                    </span>
                  )}
                </Show>
              }
            >
              <ScreenerProgressView entry={props.entry as FleetEntry} />
            </Show>
          }
        >
          {/* Above the slot ledger, with the other state chips: a drain is
              usually the reason those slots are empty. */}
          <UpdaterNotice entry={props.entry} />
          <SlotRows entry={props.entry} slotPolicy={props.slotPolicy} />
          <ConfirmationRows entry={props.entry} />
          <Show when={props.chainVectors}>
            {(chainVectors) => (
              <FleetChainWeights hotkey={hotkey()} chainVectors={chainVectors()} />
            )}
          </Show>
        </Show>
      </td>
      <FleetHostCell entry={props.entry} singular={props.singular} slotPolicy={props.slotPolicy} />
    </tr>
  );
}

export interface RetiredFleetRowProps {
  entry: FleetEntryExt;
  singular: FleetSingular;
  names: Record<string, string>;
  benchVersion: number | null;
  highlightId: string | null;
}

/**
 * One folded (inoperative) row (renderRetiredFleetRow 8886–8920). The status
 * badge survives the fold — a validator that went quiet AND whose scorer was
 * down is counted Critical in the ledger, so the reason has to be readable
 * here rather than flattened to "offline". Folded validator rows keep their
 * entity identity so a deep link still highlights, focuses and opens the
 * same drill-down.
 */
export function RetiredFleetRow(props: RetiredFleetRowProps): JSX.Element {
  const hotkey = () => hotkeyOf(props.entry, props.singular);
  const status = () => offlineAwareFleetStatusFor(props.entry, props.benchVersion);
  const work = () => fleetWork(props.entry.state, props.singular);
  const highlighted = () =>
    props.singular === "validator" && props.highlightId != null && props.highlightId === hotkey();
  const activation = rowActivation(props.singular, hotkey);
  const cells = (): JSX.Element => (
    <>
      <td class="fleet-id-cell">
        <FleetIdentity
          entry={props.entry}
          singular={props.singular}
          names={props.names}
          status={status()}
          screenerFallback="Unknown screener"
        />
      </td>
      <td class="fleet-state-cell">
        <span class="stage unknown">{work()[0]}</span>
      </td>
      <td class="fleet-host-cell">
        <div class="fleet-host">
          <span class="fleet-version">{versionLabel(props.entry)}</span>
          <Show when={props.singular === "screener"}>
            <span class="fleet-protocol">{"Policy " + props.entry.policy_version}</span>
          </Show>
        </div>
      </td>
    </>
  );
  return (
    <Show when={props.singular === "validator"} fallback={<tr>{cells()}</tr>}>
      <tr
        tabindex="-1"
        data-entity-kind="validator"
        data-entity-id={hotkey()}
        classList={{ "entity-target": highlighted() }}
        aria-current={highlighted() ? "true" : undefined}
        onClick={activation.onClick}
        onKeyDown={activation.onKeyDown}
      >
        {cells()}
      </tr>
    </Show>
  );
}
