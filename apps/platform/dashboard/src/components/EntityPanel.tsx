// The entity modal shell (monolith 3013–3037) and its open/close/focus logic
// (showModal 5980–6004, trapFocus 6008–6017, closeModal 6338–6367), driven by
// the routeStore's entityRoute. Three tenants share the one dialog:
//   miner   — leaderboard run summary (openModal 5845–5978, summarized);
//   validator — signed heartbeat report (renderValidatorDetail 8687–8814,
//               summarized; the operations port supplies the deep body);
//   agent   — the submission drawer; the deep-evidence body is the
//             submissions/reviews port's AgentEvidence component.
// Screener routes never open the modal: the monolith highlights the fleet
// row on the operations page instead (resolveEntityRoute 9303–9399), which
// is that page's job.
import { createQuery } from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createSignal,
  onCleanup,
  onMount,
  untrack,
} from "solid-js";
import type { JSX } from "solid-js";

import { publicQueryKeys, queryClient } from "../data/queryClient";
import { getJSON } from "../lib/api";
import { entityActions } from "../lib/entity-links";
import {
  agentName,
  agentVersionLabel,
  fx,
  monoDisplay,
  pct,
  publicDisplayName,
  relTime,
  relTimeUntil,
} from "../lib/format";
import { entityHref } from "../lib/router";
import type { EntityKind, EntityRoute } from "../lib/router";
import type { NameHandle } from "../types/leaderboard";
import {
  COMPOSITE_CALC_NOTE,
  compositeCalculationHeading,
  compositeCalculationRows,
  displayComposite,
  isEligible,
  isFinalized,
  unrankedKind,
} from "../lib/scoring";
import type { ContinualAggregate } from "../lib/scoring";
import { closeEntityRoute, currentPage, entityRoute, syncFromLocation } from "../stores/routeStore";
import type {
  FleetEntry,
  OperationsPayload,
  ScorerBenchmarks,
  ScorerProbe,
  StackComponentHealth,
  StackIdentity,
} from "../types/fleet";
import type { LeaderboardEntry } from "../types/leaderboard";
import type { AgentSummaryPayload, BenchmarkProgress } from "../types/pipeline";
import { activityStage } from "./pipeline/status";
import { AgentEvidence } from "./evidence/AgentEvidence";
import type { AgentEvidenceEntry, PipelineDetailPayload } from "./evidence/AgentEvidence";
import { Consensus } from "./evidence/Consensus";
// Pure fleet logic the validator body reads: the numeric slot order the
// per-slot rows sort on, plus the stack-identity/component-health vocabulary.
// (fleet.ts imports the status ladder back out of this module; both edges are
// plain declarations read at render time, never at module init.)
import {
  STACK_COMPONENT_LABELS,
  STACK_COMPONENT_ORDER,
  componentHealthChip,
  hasIdentityRows,
  identityComparisonNote,
  orphanedSlotView,
  orphanedSlotsInOrder,
  scorerProbeChip,
  scorerProbeDetail,
  scorerStatusChip,
  slotOrdinal,
  stackModeLabel,
  validatorAssignmentView,
  yesNo,
} from "./operations/fleet";
import type { OrphanedSlot } from "./operations/fleet";
import { AssignmentDetail } from "./operations/FleetTable";
import { BenchmarkProgressView } from "./operations/progress";
import { CopyButton } from "./shell/CopyButton";
import { EntityButton } from "./ui/EntityButton";
import { HandleBadge } from "./ui/HandleBadge";
import { StatusChip } from "./ui/StatusChip";

type RankedEntry = LeaderboardEntry & { rank?: number | null };

// Heartbeat fields the fleet status reads that are beyond the base wire type.
interface FleetStatusFields {
  bench_serviceability?: string | null;
  scorer_liveness?: string | null;
  allowed_slots?: number | null;
  issuance_paused?: boolean;
  /** Set by preserveTransientValidatorTelemetry when a slot's signed progress
   * was carried through the grace window (see fleet.ts). */
  _telemetry_grace?: boolean;
}
type ValidatorEntry = FleetEntry & FleetStatusFields;

// ── Fleet status (port of fleetStatus 8305–8328 + offlineAware 8334–8338) ──
// Exported so the operations page port can share one verdict per validator
// instead of growing a second copy.

export function fleetStatus(entry: ValidatorEntry): [string, string] {
  // An operator pause is the fleet's current dispatch state and must remain
  // visible even when the validator also reports a fault. The detail view
  // preserves scorer, stack and host health separately; the fleet verdict
  // answers the first routing question: can this validator receive new work?
  if (entry.issuance_paused || entry.availability === "paused") return ["Paused", "paused"];
  // Software that cannot describe the benchmark being scored comes first;
  // then the scorer that is down (the cause) before the bench gate (the
  // consequence). All three mean no lease completes.
  if (entry.bench_serviceability === "software_obsolete") return ["Obsolete build", "bad"];
  if (entry.scorer_liveness === "not_serving") return ["Scorer down", "bad"];
  if (entry.bench_serviceability === "scorer_unverified") return ["Bench unsupported", "bad"];
  if (entry.health === "critical") return ["Critical", "bad"];
  // A one-poll telemetry gap inside the grace window is a delayed update, not
  // a fleet-wide assignment failure — the whole point of preserving the prior
  // signed progress. Persistent mismatch still reads red below.
  if (entry.assignment_state === "assignment_mismatch" && entry._telemetry_grace) {
    return ["Telemetry delayed", "warn"];
  }
  if (entry.assignment_state === "assignment_mismatch") return ["Mismatch", "bad"];
  if (entry.assignment_state === "heartbeat_stale") return ["Heartbeat stale", "warn"];
  if (entry.availability === "stale") return ["Stale", "warn"];
  if (entry.availability === "offline") return ["Offline", "bad"];
  if (entry.health === "warning") return ["Warning", "warn"];
  if (entry.health === "healthy") return ["Healthy", "good"];
  return ["Not reported", "unknown"];
}

/** For a node that is already offline a liveness badge only restates that it
 * is gone in the gentler warn tone; name it offline. A real fault keeps its
 * own label. */
export function offlineAwareFleetStatus(entry: ValidatorEntry): [string, string] {
  const status = fleetStatus(entry);
  if (entry.availability === "offline" && status[1] !== "bad") return ["Offline", "bad"];
  return status;
}

// ── Shared row / section helpers ────────────────────────────────────────────

/** One label/value row of a stat group (vstat 8903–8906). Exported because
 * the consensus block's canonical-median row is the same row (stat 6109). */
export function Stat(props: { k: string; v: JSX.Element; mono?: boolean }): JSX.Element {
  return (
    <div class="stat-row">
      <span class="k">{props.k}</span>
      <span class={"v" + (props.mono ? " mono" : "")}>{props.v}</span>
    </div>
  );
}

function FleetTime(props: { iso: string | null | undefined }): JSX.Element {
  return (
    <Show when={props.iso} fallback={<>–</>}>
      {(iso) => (
        <span class="fleet-time" title={iso()}>
          {relTime(iso())}
        </span>
      )}
    </Show>
  );
}

/** Unix-seconds twin of FleetTime (unixTimeHtml 8916–8920): per-component
 * probe times arrive as epoch seconds, not ISO strings. Nothing renders for a
 * missing timestamp, exactly as the original returned "". */
function UnixTime(props: { seconds: number | null | undefined }): JSX.Element {
  const iso = (): string | null =>
    props.seconds == null ? null : new Date(props.seconds * 1000).toISOString();
  return (
    <Show when={iso()}>
      {(value) => (
        <span class="fleet-time" title={value()}>
          {relTime(value())}
        </span>
      )}
    </Show>
  );
}

/** The mono/copy treatment for a digest or revision (monoValue 8908–8914):
 * elided display, full value in the title, and a copy control beside it. */
function MonoValue(props: { value: string; label: string }): JSX.Element {
  return (
    <span class="copyable" title={props.value}>
      <span>{monoDisplay(props.value)}</span>
      <CopyButton value={props.value} label={props.label} />
    </span>
  );
}

/** One collapsible section of the validator body (section() 9152–9155). */
function Section(props: { title: string; open?: boolean; children: JSX.Element }): JSX.Element {
  return (
    <details class="cgroup" open={props.open}>
      <summary class="cgsum">{props.title}</summary>
      <div style={{ padding: "2px 0 10px 22px" }}>{props.children}</div>
    </details>
  );
}

// Stage labels for the agent tenant's header chip (the submissions port owns
// the full vocabulary; the shell needs the [label, tone] pair only).

// ── The panel itself ─────────────────────────────────────────────────────────

type PanelView =
  | { tenant: "miner"; key: string; entry: RankedEntry }
  | { tenant: "validator"; key: string; hotkey: string; entry: ValidatorEntry }
  | { tenant: "agent"; key: string; entry: AgentEvidenceEntry }
  | { tenant: "agent-state"; key: string; id: string; message: string; state: "loading" | "error" };

export interface EntityPanelProps {
  /** Ranked leaderboard entries (last successful payload, display order). */
  entries: () => RankedEntry[];
  operations: () => OperationsPayload | undefined;
  /** Optional display names keyed by validator hotkey — untrusted decoration
   * from a separate feed; the hotkey stays the anchor identity. */
  validatorNames: () => Record<string, string>;
  /** The settled/current bench version, for the miner bench chip. */
  currentBench: () => number | null;
  /** Mid-rollout settled view (affects the displayed composite). */
  settledView?: () => boolean;
}

// The URL half of closing the overlay. Dedicated entity pages (/agent/{id})
// own their URL, so closing is a no-op there (closeModal 6341–6342).
function close(): void {
  const entity = entityRoute();
  if (entity && entity.full) return;
  closeEntityRoute();
}

export function EntityPanel(props: EntityPanelProps): JSX.Element {
  const [view, setView] = createSignal<PanelView | null>(null);
  const [full, setFull] = createSignal(false);

  let modalEl: HTMLElement | undefined;
  let lastFocused: Element | null = null;

  const isOpen = () => view() !== null;
  const settled = () => (props.settledView ? props.settledView() : false);

  const agentId = (): string => {
    const route = entityRoute();
    return route?.kind === "agent" ? route.id : "";
  };
  // Summary and evidence start together when the route selects an agent. The
  // smaller summary usually wins and paints the shell first; Solid Query keeps
  // the larger record in its own cache row, deduplicates remounts, aborts a
  // stale route, and lets the evidence sections settle independently.
  const agentSummary = createQuery<AgentSummaryPayload>(
    () => {
      const id = agentId();
      return {
        queryKey: publicQueryKeys.agentSummary(id),
        queryFn: ({ signal }) =>
          getJSON<AgentSummaryPayload>(
            "/public/agent/" + encodeURIComponent(id) + "/summary",
            signal,
          ),
        enabled: Boolean(id),
      };
    },
    () => queryClient,
  );
  const agentPipeline = createQuery<PipelineDetailPayload>(
    () => {
      const id = agentId();
      return {
        queryKey: publicQueryKeys.agentPipeline(id),
        queryFn: ({ signal }) =>
          getJSON<PipelineDetailPayload>(
            "/public/agent/" + encodeURIComponent(id) + "/pipeline",
            signal,
          ),
        enabled: Boolean(id),
      };
    },
    () => queryClient,
  );

  // A cold agent link resolves through /public/agent/{id}/summary (#648): one
  // addressed submission, not the first page of the global activity feed
  // filtered down to it. The loading state now shows for an overlay route too
  // — the fetch is the same wait either way — and there is no "could not be
  // found" branch: the endpoint 404s for an unknown id, which is the same
  // "temporarily unavailable, try again" answer as any other failure, and the
  // old branch could not tell them apart anyway.
  function resolveAgent(route: EntityRoute): void {
    const key = route.key;
    const current = untrack(view);
    if (current && current.key === key && current.tenant === "agent") return;
    if (agentSummary.isError) {
      setView({
        tenant: "agent-state",
        key,
        id: route.id,
        message: "Submission details are temporarily unavailable. Try refreshing in a moment.",
        state: "error",
      });
      return;
    }
    if (!agentSummary.isPending && agentSummary.data) {
      setView({ tenant: "agent", key, entry: agentSummary.data });
      return;
    }
    setView({
      tenant: "agent-state",
      key,
      id: route.id,
      message: "Loading submission details…",
      state: "loading",
    });
  }

  createEffect(() => {
    const route = entityRoute();
    if (!route) {
      setView(null);
      setFull(false);
      return;
    }
    // Legacy URL forms (real-query params, plural hash/path routes) are
    // recognized once and normalized to the canonical hash-query form.
    if (route.legacy) {
      history.replaceState((history.state as unknown) ?? {}, "", entityHref(route.kind, route.id));
      syncFromLocation();
      return;
    }
    setFull(route.full);
    if (route.kind === "validator" || route.kind === "screener") {
      // Fleet-row targets live on the operations page only; normalize there.
      // currentPage is deliberately tracked: the store updates the entity
      // and page signals in sequence, so this effect may observe the entity
      // before the page has caught up and must re-run when it does.
      if (currentPage() !== "operations") {
        history.replaceState(
          (history.state as unknown) ?? {},
          "",
          entityHref(route.kind, route.id, "operations"),
        );
        syncFromLocation();
        return;
      }
      if (route.kind === "validator") {
        const report = props.operations()?.validators;
        const entry = (report?.validators || []).find(
          (item) => item.validator_hotkey === route.id,
        ) as ValidatorEntry | undefined;
        if (entry) setView({ tenant: "validator", key: route.key, hotkey: route.id, entry });
      }
      // Screeners: the operations page highlights the fleet row; no modal.
      return;
    }
    if (route.kind === "miner") {
      const entry = props.entries().find((item) => item.miner_hotkey === route.id);
      if (entry) setView({ tenant: "miner", key: route.key, entry });
      return;
    }
    resolveAgent(route);
  });

  // Open/close side effects: focus capture + restore, background inert, the
  // full-page body mode, scroll reset.
  createEffect(() => {
    const current = view();
    const fullPage = full();
    const wrap = document.querySelector(".wrap") as (HTMLElement & { inert?: boolean }) | null;
    if (current) {
      document.body.classList.toggle("entity-page", fullPage);
      if (!lastFocused) lastFocused = document.activeElement;
      if (wrap) wrap.inert = !fullPage;
      if (fullPage) {
        try {
          window.scrollTo(0, 0);
        } catch {
          // jsdom has no layout; scroll reset is cosmetic there.
        }
      }
      if (modalEl) modalEl.scrollTop = 0;
      const target = fullPage
        ? document.getElementById("d-back-dashboard")
        : document.getElementById("modal-close");
      target?.focus();
    } else {
      document.body.classList.remove("entity-page");
      if (wrap) wrap.inert = false;
      if (lastFocused instanceof HTMLElement) lastFocused.focus();
      lastFocused = null;
    }
  });

  // Keep Tab focus inside the open dialog (a lightweight focus trap).
  function trapFocus(ev: KeyboardEvent): void {
    if (ev.key !== "Tab" || !modalEl || !isOpen()) return;
    const nodes = modalEl.querySelectorAll<HTMLElement>(
      'a[href], button, [tabindex]:not([tabindex="-1"]), summary, input, textarea, [contenteditable]',
    );
    const focusable = Array.from(nodes).filter(
      (el) => el.offsetParent !== null || el === document.activeElement,
    );
    if (!focusable.length) return;
    const first = focusable[0] as HTMLElement;
    const last = focusable[focusable.length - 1] as HTMLElement;
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  onMount(() => {
    const onKeyDown = (ev: KeyboardEvent): void => {
      if (ev.key === "Escape") close();
      else trapFocus(ev);
    };
    document.addEventListener("keydown", onKeyDown);
    onCleanup(() => document.removeEventListener("keydown", onKeyDown));
  });

  // ── Header derivations per tenant ─────────────────────────────────────────

  const actionKind = (): EntityKind | null => {
    const current = view();
    if (!current) return null;
    if (current.tenant === "miner") return "miner";
    if (current.tenant === "validator") return "validator";
    return "agent";
  };
  const actionId = (): string => {
    const current = view();
    if (!current) return "";
    if (current.tenant === "miner") return current.entry.miner_hotkey;
    if (current.tenant === "validator") return current.hotkey;
    if (current.tenant === "agent") return String(current.entry.agent_id || "");
    return current.id;
  };
  const actions = () => {
    const kind = actionKind();
    return kind ? entityActions(kind, actionId()) : null;
  };

  interface HeaderState {
    title: string;
    handle?: NameHandle | null;
    chip: { text: string; class: string; title: string };
    dkLabel: string;
    hotkey: string | null;
    hotkeyKind: EntityKind;
  }

  const header = (): HeaderState | null => {
    const current = view();
    if (!current) return null;
    if (current.tenant === "miner") {
      const e = current.entry;
      const title = isFinalized(e)
        ? "Raw score rank #" +
          e.rank +
          (e._emission && e._emission.role === "champion" ? " · KOTH champion" : "")
        : "Provisional rank P" +
          e.rank +
          " · " +
          (e.score_count || 0) +
          " of " +
          (e.score_quorum || 3) +
          " scores";
      return {
        title,
        handle: e.name_handle,
        chip: minerBenchChip(e, props.currentBench()),
        dkLabel: "Miner",
        hotkey: e.miner_hotkey,
        hotkeyKind: "miner",
      };
    }
    if (current.tenant === "validator") {
      const status = offlineAwareFleetStatus(current.entry);
      // Display names are optional untrusted decoration from a separate
      // feed; the hotkey below stays the anchor identity.
      return {
        title: props.validatorNames()[current.hotkey] || "Validator",
        chip: { text: status[0], class: status[1], title: "Current fleet status" },
        dkLabel: "Validator",
        hotkey: current.hotkey,
        hotkeyKind: "validator",
      };
    }
    if (current.tenant === "agent") {
      const e = current.entry;
      const stage = activityStage(e.status);
      return {
        title: publicDisplayName(e.name, e.name_handle),
        handle: e.name_handle,
        chip: {
          text: stage[0] as string,
          class: stage[1] as string,
          title: "Current submission stage",
        },
        dkLabel: "Miner",
        hotkey: e.miner_hotkey || null,
        hotkeyKind: "miner",
      };
    }
    return {
      title: "Agent submission",
      chip:
        current.state === "error"
          ? { text: "Unavailable", class: "error", title: "Submission unavailable" }
          : { text: "Loading", class: "loading", title: "Loading submission" },
      dkLabel: "Miner",
      hotkey: null,
      hotkeyKind: "miner",
    };
  };

  const minerEntry = () => {
    const current = view();
    return current && current.tenant === "miner" ? current.entry : null;
  };
  const validatorView = () => {
    const current = view();
    return current && current.tenant === "validator" ? current : null;
  };
  const agentView = () => {
    const current = view();
    return current && current.tenant === "agent" ? current : null;
  };
  const agentStateView = () => {
    const current = view();
    return current && current.tenant === "agent-state" ? current : null;
  };
  const toolMean = () => minerEntry()?.tool_mean ?? 0;
  const memoryMean = () => minerEntry()?.memory_mean ?? 0;
  const toolShare = () => {
    const e = minerEntry();
    if (!e) return 50;
    const sum = e.tool_mean + e.memory_mean || 1;
    return (e.tool_mean / sum) * 100;
  };

  return (
    <>
      <div
        id="modal-back"
        class="modal-back"
        classList={{ open: isOpen() && !full() }}
        onClick={() => close()}
      />
      <aside
        id="modal"
        class="modal"
        classList={{ open: isOpen(), "full-page": full() }}
        role={full() ? "main" : "dialog"}
        aria-modal={full() ? "false" : "true"}
        aria-hidden={isOpen() ? "false" : "true"}
        aria-labelledby="d-title"
        tabindex="-1"
        ref={(el) => {
          modalEl = el;
        }}
      >
        <div class="modal-actions">
          <a
            class="btn ghost back-dashboard"
            id="d-back-dashboard"
            href={actions()?.backHref ?? "/#/overview"}
          >
            ← Dashboard
          </a>
          <a
            class="btn ghost open-full"
            id="d-open-full"
            href={actions()?.openFullHref ?? "/"}
            style={{ display: actions()?.openFullHref ? "" : "none" }}
          >
            Open full page
          </a>
          <button
            class="btn ghost close"
            id="modal-close"
            aria-label="Close detail"
            onClick={() => close()}
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        <div class="dhead">
          <h3 id="d-title">{header()?.title ?? "Miner"}</h3>
          <HandleBadge handle={header()?.handle} />
          <span id="d-bench" class={header()?.chip.class ?? ""} title={header()?.chip.title ?? ""}>
            {header()?.chip.text ?? ""}
          </span>
        </div>
        <div class="dk">
          <span class="dk-label">{header()?.dkLabel ?? "Miner"}</span>
          <span class="copyable">
            <span id="d-hotkey">
              <Show when={header()?.hotkey}>
                {(hotkey) => (
                  <EntityButton
                    kind={header()?.hotkeyKind ?? "miner"}
                    id={hotkey()}
                    label={hotkey()}
                  />
                )}
              </Show>
            </span>
            <CopyButton id="d-hotkey-copy" value={header()?.hotkey || null} label="miner hotkey" />
          </span>
        </div>
        <div class="split" style={{ display: minerEntry() ? "" : "none" }}>
          <div class="seg">
            <div id="d-tool-seg" style={{ background: "var(--tool)", width: toolShare() + "%" }}>
              {toolShare() > 14 ? fx(toolMean()) : ""}
            </div>
            <div
              id="d-mem-seg"
              style={{ background: "var(--memory)", width: 100 - toolShare() + "%" }}
            >
              {100 - toolShare() > 14 ? fx(memoryMean()) : ""}
            </div>
          </div>
          <div class="legend">
            <span>
              <i style={{ background: "var(--tool)" }} />
              Tool{" "}
              <span id="d-tool-pct" class="muted">
                {minerEntry() ? fx(toolMean()) : ""}
              </span>
            </span>
            <span>
              <i style={{ background: "var(--memory)" }} />
              Memory{" "}
              <span id="d-mem-pct" class="muted">
                {minerEntry() ? fx(memoryMean()) : ""}
              </span>
            </span>
          </div>
        </div>
        <div id="d-stats" classList={{ "pipeline-mode": view()?.tenant !== "miner" }}>
          <Switch>
            <Match when={minerEntry()}>
              {(entry) => (
                <MinerSummary
                  entry={entry()}
                  settled={settled()}
                  total={props.entries().filter(isEligible).length}
                />
              )}
            </Match>
            <Match when={validatorView()}>
              {(v) => <ValidatorSummary entry={v().entry} activeBench={props.currentBench()} />}
            </Match>
            <Match when={agentView()}>
              {(v) => (
                <AgentEvidence
                  entry={v().entry}
                  entries={props.entries}
                  settledView={props.settledView}
                  pipeline={() => (agentPipeline.isPending ? undefined : agentPipeline.data)}
                  pipelineLoading={() => agentPipeline.isPending}
                  pipelineFetching={() => agentPipeline.isFetching}
                  pipelineError={() => agentPipeline.error}
                  retryPipeline={() => void agentPipeline.refetch()}
                />
              )}
            </Match>
            <Match when={agentStateView()}>
              {(v) => (
                <div class="pipeline-detail">
                  <p class={"pipeline-detail-state " + v().state} role="status">
                    {v().message}
                  </p>
                </div>
              )}
            </Match>
          </Switch>
        </div>
      </aside>
    </>
  );
}

// ── Miner tenant summary ─────────────────────────────────────────────────────

function minerBenchChip(
  e: RankedEntry,
  currentBench: number | null,
): { text: string; class: string; title: string } {
  if (e.bench_version == null) {
    return isFinalized(e)
      ? {
          text: "legacy",
          class: "prev",
          title:
            "Scored before benchmark versioning. A legacy run, not comparable to " +
            (currentBench ? "the current DittoBench v" + currentBench : "the current benchmark") +
            ".",
        }
      : {
          text: "pending quorum",
          class: "",
          title: "Run provenance appears after the three-validator aggregate is final.",
        };
  }
  const settledVersion = currentBench;
  const old = settledVersion !== null && e.bench_version < settledVersion;
  return {
    text: "DittoBench v" + e.bench_version + (old ? " · old" : ""),
    class: old ? "prev" : "",
    title: old
      ? "Scored on DittoBench v" +
        e.bench_version +
        ", a previous benchmark. Not directly comparable to the settled v" +
        settledVersion +
        "."
      : "Scored on DittoBench v" + e.bench_version + ".",
  };
}

function MinerSummary(props: { entry: RankedEntry; settled: boolean; total: number }): JSX.Element {
  const e = () => props.entry;
  const agg = () => e() as RankedEntry & ContinualAggregate;
  const official = () => displayComposite(e(), props.settled);
  const rolling = () => agg().aggregate_method === "continual_mean";
  const kind = () => unrankedKind(e());
  const calcRows = () => compositeCalculationRows(e());
  return (
    <>
      <div class="stat-cols">
        <div class="stat-group">
          <div class="stat-head">Overview</div>
          <Stat k="Best-scoring agent" v={agentName(e().agent_name)} />
          <Stat k="Submission" v={agentVersionLabel(e().agent_version)} />
          <Stat
            k="Current leaderboard score"
            v={
              fx(official()) +
              (rolling()
                ? "  · mean of " + agg().aggregate_sample_count + " scores"
                : (e().composite_stderr != null
                    ? "  ± " + fx(e().composite_stderr as number) + " SE"
                    : "") + "  · canonical quorum median")
            }
          />
          <Stat k="Tool mean" v={fx(e().tool_mean) + "  (" + pct(e().tool_mean) + ")"} />
          <Stat k="Memory mean" v={fx(e().memory_mean) + "  (" + pct(e().memory_mean) + ")"} />
          <Show when={e().first_seen}>
            {(seen) => <Stat k="First seen" v={new Date(seen()).toLocaleString()} />}
          </Show>
          <Stat
            k="Rank"
            v={
              isEligible(e())
                ? "#" + e().rank + " of " + props.total
                : kind() === "zero"
                  ? "unranked (scored 0.000)"
                  : "unranked (provisional)"
            }
          />
        </div>
        <Show when={calcRows()}>
          {(rows) => (
            <div class="stat-group">
              <div class="stat-head">{compositeCalculationHeading(e())}</div>
              {rows().map((row) => (
                <Stat k={row.k} v={row.v} />
              ))}
              <p class="calc-note">{COMPOSITE_CALC_NOTE}</p>
            </div>
          )}
        </Show>
      </div>
      <div id="d-consensus">
        <Show when={e().agent_id}>{(id) => <Consensus agentId={id()} />}</Show>
      </div>
      <div class="gloss-link">
        <a href="#/benchmark">What each category and metric means →</a>
      </div>
    </>
  );
}

// ── Validator stack identity + component health ─────────────────────────────

/** Digest / revision / version rows for one identity (identityRows
 * 8933–8940). Renders nothing when the identity pins none of the three —
 * callers that need a placeholder supply their own. */
function IdentityRows(props: { identity: StackIdentity; copyPrefix: string }): JSX.Element {
  const identity = () => props.identity;
  return (
    <>
      <Show when={identity().image_digest}>
        {(digest) => (
          <Stat
            k="Image digest"
            mono
            v={<MonoValue value={digest()} label={props.copyPrefix + " image digest"} />}
          />
        )}
      </Show>
      <Show when={identity().source_revision}>
        {(revision) => (
          <Stat
            k="Source revision"
            mono
            v={<MonoValue value={revision()} label={props.copyPrefix + " source revision"} />}
          />
        )}
      </Show>
      <Show when={identity().version}>{(version) => <Stat k="Version" v={version()} />}</Show>
    </>
  );
}

/** One stack component (renderStackComponent 8959–8990). Configured identity
 * is what Compose intends to run and observed identity is what a live probe
 * verified; the two are rendered side by side, never merged, because the whole
 * point of the section is the difference between them. */
function StackComponent(props: {
  name: string;
  configured: StackIdentity | null | undefined;
  observed: StackComponentHealth | null | undefined;
}): JSX.Element {
  const label = () => STACK_COMPONENT_LABELS[props.name] || props.name;
  const chip = () => componentHealthChip(props.observed ? props.observed.health : null);
  const note = () => identityComparisonNote(props.configured, props.observed?.observed_identity);
  return (
    <details class="cgroup">
      <summary class="cgsum">
        <span>{label()}</span>
        <Show when={props.observed?.observed_at != null}>
          <span class="probe-time">
            probed <UnixTime seconds={props.observed?.observed_at} />
          </span>
        </Show>
        <StatusChip label={chip()[0]} tone={chip()[1]} />
      </summary>
      <div class="crow-body" style={{ padding: "2px 0 10px 22px" }}>
        <Show
          when={props.observed}
          fallback={
            <Stat
              k="Health"
              v={<span class="muted">Not reported (requires heartbeat protocol 9)</span>}
            />
          }
        >
          {(observed) => (
            <>
              <Stat k="Health" v={<StatusChip label={chip()[0]} tone={chip()[1]} />} />
              <Stat k="Required component" v={yesNo(observed().required)} />
              <Show when={observed().ready != null}>
                <Stat k="Endpoint ready" v={yesNo(observed().ready)} />
              </Show>
              <Show when={observed().model_ready != null}>
                <Stat
                  k={props.name === "ollama" ? "Embedding model ready" : "Model route ready"}
                  v={yesNo(observed().model_ready)}
                />
              </Show>
              <Show when={observed().observed_at != null}>
                <Stat k="Last probe" v={<UnixTime seconds={observed().observed_at} />} />
              </Show>
              <div class="subhead">Observed identity</div>
              <Show
                when={observed().observed_identity}
                fallback={
                  <Stat
                    k="Observed identity"
                    v={<span class="muted">Not independently observed</span>}
                  />
                }
              >
                {(identity) => (
                  <IdentityRows identity={identity()} copyPrefix={label() + " observed"} />
                )}
              </Show>
            </>
          )}
        </Show>
        <Show when={props.configured}>
          {(configured) => (
            <>
              <div class="subhead">Configured identity</div>
              <Stat k="Provenance" v={configured().provenance || "unknown"} />
              <Show
                when={hasIdentityRows(configured())}
                fallback={<Stat k="Identity" v={<span class="muted">None pinned</span>} />}
              >
                <IdentityRows identity={configured()} copyPrefix={label() + " configured"} />
              </Show>
            </>
          )}
        </Show>
        <Show when={note()}>
          {(value) => <span class={"identity-note " + value().tone}>{value().text}</span>}
        </Show>
      </div>
    </details>
  );
}

/** What the validator's probe of its own scorer saw (renderScorerProbe
 * 9013–9035). The status row above is the conclusion; this is the evidence,
 * and it is what separates a scorer that never answered from one that
 * answered with something unusable. */
function ScorerProbeRows(props: { probe: ScorerProbe | null | undefined }): JSX.Element {
  return (
    <Show
      when={props.probe}
      fallback={
        <Stat
          k="Scorer probe"
          v={<span class="muted">Not reported (requires heartbeat protocol 15)</span>}
        />
      }
    >
      {(probe) => {
        const chip = () => scorerProbeChip(probe().outcome);
        return (
          <>
            <Stat
              k="Scorer probe"
              v={
                <>
                  <StatusChip label={chip()[0]} tone={chip()[1]} />
                  {scorerProbeDetail(probe())}
                </>
              }
            />
            <Stat k="Probe observed" v={<UnixTime seconds={probe().observed_at} />} />
            <Stat
              k="Last served"
              v={
                probe().last_served_at != null ? (
                  <UnixTime seconds={probe().last_served_at} />
                ) : (
                  <span class="muted">Not since this validator started</span>
                )
              }
            />
          </>
        );
      }}
    </Show>
  );
}

/** Scorer capability rows inside Capabilities (renderScorerBenchmarks
 * 8992–9011). A validator whose heartbeat predates protocol 8 says so rather
 * than reading as a scorer with no benchmarks. */
function ScorerBenchmarkRows(props: { scorer: ScorerBenchmarks | null | undefined }): JSX.Element {
  return (
    <Show
      when={props.scorer}
      fallback={
        <Stat
          k="Scorer benchmarks"
          v={<span class="muted">Not reported (requires heartbeat protocol 8)</span>}
        />
      }
    >
      {(scorer) => {
        const chip = () => scorerStatusChip(scorer().status);
        return (
          <>
            <Stat k="Scorer status" v={<StatusChip label={chip()[0]} tone={chip()[1]} />} />
            <ScorerProbeRows probe={scorer().probe} />
            <Stat
              k="Supported benchmarks"
              v={
                (scorer().supported_bench_versions || [])
                  .map((version) => "v" + version)
                  .join(", ") || "–"
              }
            />
            <Show when={scorer().observed_at != null}>
              <Stat k="Capability observed" v={<UnixTime seconds={scorer().observed_at} />} />
            </Show>
            <Show when={scorer().software_version}>
              {(version) => <Stat k="Scorer version" v={version()} />}
            </Show>
            <Show when={scorer().source_revision}>
              {(revision) => (
                <Stat
                  k="Scorer revision"
                  mono
                  v={<MonoValue value={revision()} label="scorer source revision" />}
                />
              )}
            </Show>
          </>
        );
      }}
    </Show>
  );
}

/**
 * One evicted slot's row value (9075–9086). The chip and the agent anchor come
 * from orphanedSlotView; the detail line adds when the platform let go of the
 * lease, when the container is expected to stop on its own, and which bench
 * version it is burning. The deadline is usually still ahead, and relTime
 * floors a future instant to "0s ago" — which reads as "already over" for the
 * one number that says when this stops costing the host CPU — so it gets
 * relTimeUntil instead.
 */
function EvictedLease(props: { orphan: OrphanedSlot }): JSX.Element {
  const view = () => orphanedSlotView(props.orphan);
  return (
    <>
      <span class="stage warn" title={view().detail}>
        {view().label}
      </span>
      <span class="current-agent" title={view().agentId}>
        <EntityButton kind="agent" id={view().agentId} label={view().agentLabel} />
      </span>
      <span class="assignment-detail">
        <b>Lease released</b> <FleetTime iso={props.orphan.evicted_at} />
        <Show when={props.orphan.original_deadline}>
          {(deadline) => (
            <>
              {" · "}
              <b>Self-terminates by</b>{" "}
              <span class="fleet-time" title={deadline()}>
                {relTimeUntil(deadline())}
              </span>
            </>
          )}
        </Show>
        {" · bench v" + String(props.orphan.bench_version)}
      </span>
    </>
  );
}

// ── Validator tenant summary ────────────────────────────────────────────────

function ValidatorSummary(props: {
  entry: ValidatorEntry;
  activeBench: number | null;
}): JSX.Element {
  const e = () => props.entry;
  const status = () => offlineAwareFleetStatus(e());
  const scoredLabel = () =>
    props.activeBench ? "bench v" + props.activeBench : "the scored benchmark";
  const slotSummary = () => {
    // Advertised, healthy and funded are three different numbers, and the
    // one that decides whether a slot gets work is the last of them.
    let summary =
      String((e().healthy_slots || []).length) + " healthy of " + String(e().configured_slots || 1);
    if (isFinite(Number(e().allowed_slots))) {
      summary += " · " + String(e().allowed_slots) + " funded by the operator cap";
    }
    return summary + " · " + String(e().admission || "accepting");
  };
  // Present only while the platform's assignment and the validator's own
  // heartbeat disagree, or a hand-off is in flight (9073–9074). A reconciled
  // assignment renders no row at all.
  const assignment = () => validatorAssignmentView(e());
  const orphanedSlots = () => orphanedSlotsInOrder(e());
  // One row per running job, ordered by slot ordinal (9088–9096). The modal
  // used to show only the lowest slot, so a second concurrent benchmark was
  // invisible here — the #540 regression the slot-fan-out suite exists for.
  const activeSlots = () =>
    (e().active_benchmarks || [])
      .slice()
      .sort((left, right) => slotOrdinal(left.slot_id) - slotOrdinal(right.slot_id));
  // A payload from before the slot fan-out carries one `active_benchmark`
  // instead of the per-slot list (9097–9098). Without this the running work on
  // such a validator is simply absent from the modal. Suppressed once the
  // assignment row is already describing the same hand-off.
  const legacyBenchmark = (): BenchmarkProgress | null => {
    if (activeSlots().length || assignment()) return null;
    return e().active_benchmark || null;
  };
  return (
    <div class="vdetail">
      <Section title="Signed report" open>
        <>
          <Stat k="Fleet status" v={<StatusChip label={status()[0]} tone={status()[1]} />} />
          <Show when={e().bench_serviceability && e().bench_serviceability !== "serving"}>
            <Stat
              k="Benchmark eligibility"
              v={
                <StatusChip
                  tone="bad"
                  label={
                    e().bench_serviceability === "software_obsolete"
                      ? "Cannot serve " + scoredLabel() + " · needs a software upgrade"
                      : "Scorer identity is not eligible for " + scoredLabel()
                  }
                />
              }
            />
          </Show>
          <Stat k="Worker state" v={e().state || "unknown"} />
          <Stat k="Software version" v={e().software_version || "Unknown"} />
          <Stat k="Heartbeat protocol" v={String(e().protocol_version)} />
          <Stat k="First seen" v={<FleetTime iso={e().first_seen_at} />} />
          <Stat k="Validator reported" v={<FleetTime iso={e().reported_at} />} />
          <Stat k="Platform received" v={<FleetTime iso={e().seen_at} />} />
          <Show when={assignment()}>
            <Stat k="Assignment" v={<AssignmentDetail entry={e()} />} />
          </Show>
          <Stat k="Slots" v={slotSummary()} />
          {/* Above the running slots deliberately: an operator opening this
              modal is usually asking "does this host have room", and the
              answer is no while any of these are listed (9065–9087). */}
          <For each={orphanedSlots()}>
            {(orphan) => (
              <Stat
                k={(orphan.slot_id || "slot-0") + " · evicted lease"}
                v={<EvictedLease orphan={orphan} />}
              />
            )}
          </For>
          <For each={activeSlots()}>
            {(benchmark) => (
              <Stat
                k={benchmark.slot_id || "slot-0"}
                v={<BenchmarkProgressView progress={benchmark} showAgent={true} />}
              />
            )}
          </For>
          <Show when={legacyBenchmark()}>
            {(benchmark) => (
              <Stat
                k="Active benchmark"
                v={<BenchmarkProgressView progress={benchmark()} showAgent={true} />}
              />
            )}
          </Show>
        </>
      </Section>
      <Section title="Capabilities">
        <Show
          when={e().capabilities}
          fallback={
            <Stat
              k="Capabilities"
              v={<span class="muted">Not reported (requires heartbeat protocol 7)</span>}
            />
          }
        >
          {(caps) => (
            <>
              <Stat k="Screened images" v={yesNo(caps().screened_images)} />
              <Stat k="Requires screened image" v={yesNo(caps().require_screened_image)} />
              <Stat k="Source-build fallback" v={yesNo(caps().source_build_fallback)} />
              <Stat k="Managed full stack" v={yesNo(caps().full_stack_managed)} />
              <Stat k="Stack auto-updater" v={yesNo(caps().stack_updater)} />
              <Stat k="Sandbox egress restricted" v={yesNo(caps().sandbox_egress_restricted)} />
              <Stat k="Executor isolation" v={caps().executor_isolation || "unknown"} />
              <ScorerBenchmarkRows scorer={caps().scorer_benchmarks} />
            </>
          )}
        </Show>
      </Section>
      <Section title="Stack identity">
        <Show
          when={e().stack}
          fallback={
            <Stat
              k="Stack identity"
              v={<span class="muted">Not reported (requires heartbeat protocol 7)</span>}
            />
          }
        >
          {(stack) => (
            <>
              <Stat k="Stack mode" v={stackModeLabel(stack().mode)} />
              <Stat k="Compose schema" v={String(stack().compose_schema)} />
              <Show when={stack().release_descriptor_digest}>
                {(digest) => (
                  <Stat
                    k="Release descriptor"
                    mono
                    v={<MonoValue value={digest()} label="release descriptor digest" />}
                  />
                )}
              </Show>
            </>
          )}
        </Show>
        {/* Release ownership in full. The fleet table carries only the mode —
            who updates this host — because that is the part that changes how
            an operator acts on it; channel, attempt history and the last
            successful replacement settle questions asked here. */}
        <Show when={e().updater_status}>
          {(updater) => (
            <>
              <Stat k="Updater channel" v={updater().channel || "Not reported"} />
              <Stat k="Updater state" v={updater().state.replace(/_/g, " ")} />
              <Stat
                k="Last successful update"
                v={<UnixTime seconds={updater().last_success_at} />}
              />
              <Show when={updater().failed_candidate_count}>
                {(count) => <Stat k="Failed attempts" v={String(count())} />}
              </Show>
              <Show when={updater().last_failure_reason}>
                {(reason) => <Stat k="Last failure" v={reason()} />}
              </Show>
            </>
          )}
        </Show>
      </Section>
      {/* Open by default: an operator who reaches this modal is usually asking
          which part of the stack is broken, and the answer is one click deep. */}
      <Section title="Component health" open>
        <>
          <p class="muted" style={{ margin: "4px 0 8px", "font-size": "12px" }}>
            {e().stack_health
              ? "Configured identity is what Compose intends to run; observed identity is what a " +
                "live probe independently verified; readiness is a real request answered just " +
                "now. Per-component probe times are independent of heartbeat freshness."
              : "This validator reports heartbeat protocol " +
                String(e().protocol_version) +
                ". Per-component runtime health arrives with protocol 9."}
          </p>
          <For each={STACK_COMPONENT_ORDER}>
            {(name) => (
              <StackComponent
                name={name}
                configured={e().stack?.components?.[name] ?? null}
                observed={e().stack_health?.[name] ?? null}
              />
            )}
          </For>
        </>
      </Section>
      <Section title="Host metrics">
        <Show
          when={e().system_metrics}
          fallback={<Stat k="Host metrics" v={<span class="muted">Not reported</span>} />}
        >
          {(m) => (
            <>
              <Stat k="CPU" v={m().cpu_percent + "%"} />
              <Stat k="Memory" v={m().memory_percent + "%"} />
              <Stat k="Disk" v={m().disk_percent + "%"} />
              <Stat
                k="Docker"
                v={
                  m().docker_status +
                  " · " +
                  m().running_containers +
                  " running, " +
                  m().unhealthy_containers +
                  " unhealthy"
                }
              />
            </>
          )}
        </Show>
      </Section>
    </div>
  );
}
