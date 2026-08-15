// Fleet table rendering (monolith renderFleet 8922–9114, renderRetiredFleetRow
// 8886–8920, renderFleetMetricCells 8346–8361, fleetMeter 8340–8344, slot cell
// 9030–9070, updateFleetLedger/resetFleetLedger 8845–8869, row activation
// 9116–9137). Every number here comes off the one shared operations snapshot
// (or the screener feed) handed down by the page — no panel refetches.
import { For, Show, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import { relTime, shortKey } from "../../lib/format";
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
  return <span class="fleet-protocol">{props.slotId}</span>;
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

function protocolLine(entry: FleetEntryExt, singular: FleetSingular): string {
  if (singular === "screener") {
    return "Protocol " + entry.protocol_version + " · Policy " + entry.policy_version;
  }
  // Slots read "funded of advertised", not "healthy of advertised": the
  // operator cap is the number that decides whether a slot can be given work.
  return (
    "Protocol " +
    entry.protocol_version +
    " · " +
    String(fundedSlotCount(entry)) +
    " of " +
    String(entry.configured_slots || 1) +
    " slots · " +
    String(entry.admission || "accepting")
  );
}

/** Retired rows keep the plain protocol line — the slot economy is meaningless
 * on a host the platform leases nothing to (8902–8904). */
function retiredProtocolLine(entry: FleetEntryExt, singular: FleetSingular): string {
  return singular === "screener"
    ? "Protocol " + entry.protocol_version + " · Policy " + entry.policy_version
    : "Protocol " + entry.protocol_version;
}

/** The identity cell: optional display-name decoration above the hotkey
 * anchor + copy control; the hotkey stays the anchor identity either way. */
function FleetIdentity(props: {
  entry: FleetEntryExt;
  singular: FleetSingular;
  names: Record<string, string>;
  /** Retired screeners name themselves "Unknown screener" (8890). */
  screenerFallback?: string;
}): JSX.Element {
  const hotkey = () => hotkeyOf(props.entry, props.singular);
  // Validators are keyed by a distinct hotkey; the screener fleet shares one
  // hotkey, so each worker is distinguished by its instance_id.
  const displayName = () =>
    props.singular === "validator"
      ? props.names[hotkey()] || ""
      : props.entry.instance_id || props.screenerFallback || "";
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
      <Show
        when={displayName()}
        fallback={
          <span class="fleet-node copyable" title={hotkey()}>
            {keyNode()}
          </span>
        }
      >
        {(name) => (
          <span class="fleet-node-identity">
            <span class="fleet-node-name" title={name()}>
              <Show when={props.singular === "validator"} fallback={name()}>
                <EntityButton kind="validator" id={hotkey()} label={name()} />
              </Show>
            </span>
            <span class="fleet-node fleet-node-key copyable" title={hotkey()}>
              {keyNode()}
            </span>
          </span>
        )}
      </Show>
    </span>
  );
}

function FleetMeter(props: { value: number; tone: string }): JSX.Element {
  return (
    <span class={"fleet-meter" + (props.tone ? " " + props.tone : "")}>
      <span class="fleet-meter-track" aria-hidden="true">
        <i style={{ "--value": String(props.value) }} />
      </span>
      <span class="fleet-meter-value">{props.value}%</span>
    </span>
  );
}

/** The four telemetry cells; a missing report is one explicit statement, not
 * four fabricated zeros. Only memory ≥ 90 and disk ≥ 95 warn — CPU saturation
 * during a benchmark is normal work, not an incident (8346–8361). */
export function FleetMetricCells(props: {
  metrics: SystemMetrics | null | undefined;
}): JSX.Element {
  return (
    <Show
      when={props.metrics}
      fallback={
        <td class="fleet-unreported" colspan="4">
          System metrics unavailable
        </td>
      }
    >
      {(metrics) => {
        const docker = () => {
          if (metrics().docker_status === "healthy")
            return { value: metrics().running_containers + " running", tone: "" };
          if (metrics().docker_status === "degraded")
            return { value: metrics().unhealthy_containers + " unhealthy", tone: "bad" };
          return { value: "Not reported", tone: "" };
        };
        return (
          <>
            <td>
              <FleetMeter value={metrics().cpu_percent} tone="" />
            </td>
            <td>
              <FleetMeter
                value={metrics().memory_percent}
                tone={metrics().memory_percent >= 90 ? "warn" : ""}
              />
            </td>
            <td>
              <FleetMeter
                value={metrics().disk_percent}
                tone={metrics().disk_percent >= 95 ? "warn" : ""}
              />
            </td>
            <td>
              <span class={"fleet-container-health" + (docker().tone ? " " + docker().tone : "")}>
                {docker().value}
              </span>
            </td>
          </>
        );
      }}
    </Show>
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

/**
 * One running slot as a single dense line: slot id, bench chip, agent link,
 * live bar, and the numbers (`47% · 132/281 · 15m 0s`). Nothing is dropped —
 * the full progress sentence (stage label, stall reason, delayed-telemetry
 * note) rides on the line's tooltip and the bar's accessible label, so eight
 * busy slots read as eight rows of a ledger instead of eight stacked cards.
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
  const numbers = createMemo(() => {
    const parts: string[] = [];
    if (delayed()) parts.push("update delayed");
    if (failed()) parts.push("failed · retrying");
    else if (stalled()) parts.push("stalled");
    else if (p().stage && p().stage !== "running_benchmark") {
      parts.push(SLOT_STAGE_SHORT[p().stage as string] || "working");
    } else if (!p().stage && !delayed()) {
      parts.push("awaiting progress");
    }
    if (p().percent != null) parts.push(String(p().percent) + "%");
    if (counts()) parts.push(counts());
    return parts.join(" · ");
  });
  return (
    <div
      class="fleet-slot-line"
      classList={{ warn: stalled() || failed() }}
      title={sentence()}
      data-slot={props.slotId}
    >
      <span class="fleet-slot-id">{props.slotId}</span>
      <Show when={p().bench_version}>
        {(version) => (
          <span class="benchmark-version-chip" title={"Bench v" + version()}>
            v{version()}
          </span>
        )}
      </Show>
      <span class="fleet-slot-agent" title={String(p().agent_id || "")}>
        <EntityButton
          kind="agent"
          id={p().agent_id}
          label={
            (p().agent_name || "Unnamed agent") + " · " + String(p().agent_id || "").slice(0, 8)
          }
        />
      </span>
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
      <span class="fleet-slot-status">
        {numbers()}
        <Show when={p().started_at}>
          {(startedAt) => (
            <>
              {numbers() ? " · " : ""}
              <ElapsedTime class="fleet-slot-elapsed" startedAt={startedAt()} />
            </>
          )}
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
      <Show when={!runningSlots().length && !orphanedSlots().length}>
        <span class="stage unknown">No active work</span>
      </Show>
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
              <span class="fleet-protocol">{work.slot_id}</span>
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
}

/** One active fleet row (renderFleet 9017–9109). */
export function FleetRow(props: FleetRowProps): JSX.Element {
  const hotkey = () => hotkeyOf(props.entry, props.singular);
  const status = () => fleetStatusFor(props.entry, props.benchVersion);
  const work = createMemo<[string, string]>(() => {
    const base = fleetWork(props.entry.state, props.singular);
    return props.entry.availability === "stale" ? ["Last reported · " + base[0], "warn"] : base;
  });
  const reportedAt = () => props.entry.seen_at || props.entry.reported_at;
  // A plain worker-state chip only when nothing more granular is available:
  // assignment skew, any slot's live benchmark stage, a screening stage.
  const hasGranularProgress = () =>
    Boolean(
      (props.singular === "validator" &&
        (validatorAssignmentView(props.entry) || anyBenchmarkStage(props.entry))) ||
      (props.singular === "screener" && props.entry.screening_progress),
    );
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
      <td>
        <FleetIdentity entry={props.entry} singular={props.singular} names={props.names} />
      </td>
      <td>
        <span class={stageClass(status()[1])}>{status()[0]}</span>
      </td>
      <td>
        <span class="fleet-time" title={props.entry.first_seen_at || ""}>
          {props.entry.first_seen_at ? relTime(props.entry.first_seen_at) : "–"}
        </span>
      </td>
      <td>
        <span class="fleet-time" title={reportedAt() || ""}>
          {relTime(reportedAt())}
        </span>
      </td>
      <td class="fleet-work-col">
        <Show when={!hasGranularProgress()}>
          <span class={stageClass(work()[1])}>{work()[0]}</span>
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
          <SlotRows entry={props.entry} slotPolicy={props.slotPolicy} />
          <ConfirmationRows entry={props.entry} />
          <UpdaterNotice entry={props.entry} />
        </Show>
      </td>
      <td>
        <span class="fleet-version">{versionLabel(props.entry)}</span>
        <span
          class="fleet-protocol"
          title={
            props.singular === "validator" ? slotCapacityTitle(props.entry, props.slotPolicy) : ""
          }
        >
          {protocolLine(props.entry, props.singular)}
        </span>
        <Show when={props.singular === "validator" ? updaterModeLine(props.entry) : null}>
          {(mode) => <span class="fleet-updater-mode">{mode()}</span>}
        </Show>
        <Show when={props.singular === "validator" && props.entry.updater_status?.last_success_at}>
          <span
            class="fleet-updater-success"
            title={new Date(
              (props.entry.updater_status?.last_success_at || 0) * 1000,
            ).toISOString()}
          >
            Updated{" "}
            {relTime(
              new Date((props.entry.updater_status?.last_success_at || 0) * 1000).toISOString(),
            )}
          </span>
        </Show>
      </td>
      <FleetMetricCells metrics={props.entry.system_metrics} />
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
  const reportedAt = () => props.entry.seen_at || props.entry.reported_at;
  const highlighted = () =>
    props.singular === "validator" && props.highlightId != null && props.highlightId === hotkey();
  const activation = rowActivation(props.singular, hotkey);
  const cells = (): JSX.Element => (
    <>
      <td>
        <FleetIdentity
          entry={props.entry}
          singular={props.singular}
          names={props.names}
          screenerFallback="Unknown screener"
        />
      </td>
      <td>
        <span class={stageClass(status()[1])}>{status()[0]}</span>
      </td>
      <td>
        <span class="fleet-time" title={reportedAt() || ""}>
          {relTime(reportedAt())}
        </span>
      </td>
      <td>
        <span class="stage unknown">{work()[0]}</span>
      </td>
      <td>
        <span class="fleet-version">{versionLabel(props.entry)}</span>
        <span class="fleet-protocol">{retiredProtocolLine(props.entry, props.singular)}</span>
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
