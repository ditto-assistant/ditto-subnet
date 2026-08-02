// The operations page (monolith markup 2742–2862 + loadOperations 9434–9462,
// loadValidatorNames 9464–9484, loadScreeners 9486–9500, renderFleet
// 8922–9114, resolveEntityRoute fleet targets 9365–9398). Every panel — the
// pipeline board, the rescreen notice, the fleet ledger and both fleet
// tables — consumes exactly ONE /public/operations snapshot per tick, and the
// snapshot note states the reconciliation plus its age (skew is visible, not
// papered over). Validator display names arrive on a separate feed and are
// optional untrusted decoration: reset on every refetch, rendered as inert
// text, never a substitute for the hotkey identity.
import { For, createEffect, createMemo, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { FleetLedger, FleetRow, RetiredFleetRow } from "../components/operations/FleetTable";
import {
  IntegrityReviewBranch,
  PipelineBoard,
  RescreenNotice,
} from "../components/operations/PipelineBoard";
import {
  fleetLedgerCounts,
  fleetWindowLabel,
  isInoperativeFleetEntry,
  preserveTransientValidatorTelemetry,
  sortFleetEntries,
} from "../components/operations/fleet";
import type {
  FleetEntryExt,
  FleetLedgerKey,
  FleetReportExt,
  FleetSingular,
  SlotPolicy,
} from "../components/operations/fleet";
import type { PipelineEntryExt } from "../components/operations/pipeline";
import { EmptyRow } from "../components/ui/States";
import { useEndpoint } from "../data/useEndpoint";
import type { ResourceState } from "../data/useEndpoint";
import { OPS_REFRESH_MS, REFRESH_MS } from "../lib/config";
import { relTime } from "../lib/format";
import { entityRoute } from "../stores/routeStore";
import type { FleetReport, OperationsPayload, ValidatorNamesPayload } from "../types/fleet";

/** Errored resources read as absent — stated absence, never stale-as-fresh. */
function latest<T>(resource: ResourceState<T>): T | undefined {
  if (resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

/** The operations activity slice as actually served — the shared PipelineFeed
 * type declares only `entries`; the feed also carries the authoritative
 * status counts and the visible/total window the snapshot note reads. */
interface OpsActivityFeed {
  entries?: PipelineEntryExt[];
  status_counts?: Record<string, number>;
  count?: number;
  total?: number;
}

interface FleetView {
  kind: "validators" | "screeners";
  singular: FleetSingular;
  Kind: "Validator" | "Screener";
  unavailable: boolean;
  loading: boolean;
  entries: FleetEntryExt[];
  retired: FleetEntryExt[];
  counts: Record<FleetLedgerKey, number> | null;
  slotPolicy: SlotPolicy | null;
  staleWindowSeconds: number | null;
  generatedAt: string | null;
}

export function OperationsPage(): JSX.Element {
  const operations = useEndpoint<OperationsPayload>("/public/operations", {
    pollMs: OPS_REFRESH_MS,
  });
  const validatorNames = useEndpoint<ValidatorNamesPayload>("/public/validator-names", {
    pollMs: REFRESH_MS,
  });
  const screeners = useEndpoint<FleetReport>("/public/screeners", { pollMs: REFRESH_MS });

  // A refresh failure does not invalidate the last reconciled snapshot: every
  // panel keeps rendering it while the next poll retries, and only the note
  // changes. Only a cold start with no trustworthy data renders the
  // unavailable placeholders (loadOperations catch, weekend drift 9816–9834).
  const ops = createMemo<OperationsPayload | undefined>((prev) => latest(operations) ?? prev);
  const refreshDelayed = () => Boolean(operations.error()) && Boolean(ops());
  const opsUnavailable = () => Boolean(operations.error()) && !ops();
  const opsLoading = () => !ops() && !opsUnavailable();

  const [showScreeners, setShowScreeners] = createSignal(false);

  // Applied once per payload, like the monolith's single mutation at load
  // time: empty slots inherit their prior signed progress through the
  // 20-second grace, stamped _telemetry_delayed.
  const preservedValidators = createMemo(() => {
    const report = ops()?.validators as FleetReportExt | undefined;
    if (!report) return undefined;
    return preserveTransientValidatorTelemetry(report, Date.now()) as FleetReportExt;
  });

  // Display names / stake weights: rebuilt from scratch on every payload —
  // decoration resets on refetch, and a failed feed clears rather than
  // freezes it (loadValidatorNames 9464–9484).
  const nameData = createMemo(() => {
    const names: Record<string, string> = {};
    const stakes: Record<string, number> = {};
    (latest(validatorNames)?.validators ?? []).forEach((entry) => {
      if (!entry || typeof entry.validator_hotkey !== "string") return;
      if (typeof entry.display_name === "string")
        names[entry.validator_hotkey] = entry.display_name;
      if (Number.isFinite(entry.stake_weight)) {
        stakes[entry.validator_hotkey] = entry.stake_weight as number;
      }
    });
    return { names, stakes };
  });

  // The benchmark the fleet is actually scoring, as reported alongside the
  // verdicts computed against it; the snapshot-level version is the fallback
  // (activeBenchVersion 8295–8298). Never a literal.
  const benchVersion = createMemo(() => {
    const report = preservedValidators();
    return Number(report?.active_bench_version) || Number(ops()?.active_bench_version) || null;
  });

  const activity = (): OpsActivityFeed | undefined =>
    ops()?.activity as OpsActivityFeed | undefined;
  const pipelineEntries = createMemo<PipelineEntryExt[]>(() => activity()?.entries ?? []);
  const statusCounts = () => activity()?.status_counts ?? {};

  // The one shared snapshot's provenance note (loadOperations 9448–9460).
  const snapshotNote = createMemo(() => {
    if (opsUnavailable()) return "Shared operations snapshot unavailable";
    if (refreshDelayed()) {
      const at = ops()?.generated_at;
      return "Refresh delayed · showing last reconciled snapshot" + (at ? " · " + relTime(at) : "");
    }
    const data = ops();
    if (!data) return "Loading one shared operations snapshot…";
    const visibleHistory =
      Number(activity()?.count ?? NaN) < Number(activity()?.total ?? NaN)
        ? " · recent history shown; full history in Activity"
        : "";
    return "Pipeline and fleet reconciled" + visibleHistory + " · " + relTime(data.generated_at);
  });

  const fleet = createMemo<FleetView>(() => {
    const screenersShown = showScreeners();
    const kind = screenersShown ? ("screeners" as const) : ("validators" as const);
    const singular: FleetSingular = screenersShown ? "screener" : "validator";
    const Kind = screenersShown ? ("Screener" as const) : ("Validator" as const);
    const report = screenersShown
      ? (latest(screeners) as FleetReportExt | undefined)
      : preservedValidators();
    const unavailable = screenersShown ? Boolean(screeners.error()) : opsUnavailable();
    const loading = !unavailable && !report;
    if (unavailable || loading) {
      return {
        kind,
        singular,
        Kind,
        unavailable,
        loading,
        entries: [],
        retired: [],
        counts: null,
        slotPolicy: null,
        staleWindowSeconds: null,
        generatedAt: null,
      };
    }
    const list = (report?.[kind] ?? []) as FleetEntryExt[];
    const allEntries = sortFleetEntries(list, singular, nameData().stakes);
    // Two ways to be inoperative, and both fold away; a CURRENT validator
    // whose scorer broke is deliberately not folded — that is a live
    // incident and belongs in the open table reading "Scorer down".
    const retired = allEntries.filter(isInoperativeFleetEntry);
    const entries = allEntries.filter((entry) => !isInoperativeFleetEntry(entry));
    return {
      kind,
      singular,
      Kind,
      unavailable,
      loading,
      entries,
      retired,
      counts: fleetLedgerCounts(allEntries),
      // Fleet-wide policy, carried on the snapshot rather than per row: it
      // turns "this slot is idle" into "this slot is capped".
      slotPolicy: singular === "validator" ? (report?.slot_policy ?? null) : null,
      staleWindowSeconds: (report?.stale_window_seconds as number | undefined) ?? null,
      generatedAt: report?.generated_at ?? null,
    };
  });

  const fleetSummary = createMemo(() => {
    const view = fleet();
    if (view.unavailable) return view.Kind + " status unavailable";
    if (view.loading) return "Loading " + view.singular + " status…";
    const available = view.entries.filter((entry) => entry.availability === "available").length;
    const snapshot = view.generatedAt ? " · snapshot " + relTime(view.generatedAt) : "";
    return (
      (view.entries.length
        ? available + " of " + view.entries.length + " active " + view.kind + " available"
        : "No active " + view.kind + " reporting") +
      (view.retired.length ? " · " + view.retired.length + " inoperative" : "") +
      snapshot
    );
  });

  const retiredSummary = createMemo(() => {
    const view = fleet();
    if (!view.retired.length) return "";
    // Name why, on the closed summary. The ledger counts these nodes as
    // Critical or Offline while the open table shows no such row, and a
    // count with nothing behind it is how a broken validator went invisible.
    const retiredObsolete = view.retired.filter(
      (entry) => entry.bench_serviceability === "software_obsolete",
    ).length;
    const retiredOffline = view.retired.filter((entry) => entry.availability === "offline").length;
    const retiredWhy: string[] = [];
    if (retiredOffline) retiredWhy.push(retiredOffline + " offline");
    if (retiredObsolete) {
      retiredWhy.push(retiredObsolete + " obsolete build" + (retiredObsolete === 1 ? "" : "s"));
    }
    return (
      view.retired.length +
      " " +
      view.singular +
      (view.retired.length === 1 ? "" : "s") +
      (retiredWhy.length ? " · " + retiredWhy.join(" · ") : "")
    );
  });

  const retiredNote = createMemo(() => {
    const view = fleet();
    if (view.singular === "screener") {
      return "Heartbeat history remains visible for 24 hours, then expires automatically.";
    }
    // "Offline" is a window, not a constant: read it from the snapshot the
    // rows were classified with, and name the bench gate from the snapshot's
    // version — never restate either in copy.
    const version = benchVersion();
    return (
      "No heartbeat for over " +
      fleetWindowLabel(view.staleWindowSeconds) +
      (version ? ", or software that cannot serve bench v" + version : "") +
      ". The platform leases work to neither. Last reports stay on record, and a row rejoins the fleet table as soon as it can take work again."
    );
  });

  // ── Entity deep links (resolveEntityRoute 9365–9398) ─────────────────────
  // Validator/screener routes normalize onto this page: flip the fleet
  // toggle if needed, highlight the row, unfold the collapsible when the
  // target lives inside it, and scroll/focus it once per route.
  const highlightId = createMemo(() => {
    const entity = entityRoute();
    return entity && (entity.kind === "validator" || entity.kind === "screener") ? entity.id : null;
  });

  createEffect(() => {
    const entity = entityRoute();
    if (!entity || (entity.kind !== "validator" && entity.kind !== "screener")) return;
    setShowScreeners(entity.kind === "screener");
  });

  let rootEl: HTMLElement | undefined = undefined;
  let focusedKey = "";
  createEffect(() => {
    const id = highlightId();
    fleet(); // re-resolve once the rows the target lives in have rendered
    if (!id) {
      focusedKey = "";
      return;
    }
    const rows = rootEl?.querySelectorAll(
      "#fleet-rows tr[data-entity-id], #fleet-retired-rows tr[data-entity-id]",
    );
    const target = Array.prototype.find.call(
      rows ?? [],
      (row: Element) => row.getAttribute("data-entity-id") === id,
    ) as HTMLElement | undefined;
    if (!target) return;
    // An inoperative validator's row lives inside the collapsible; unfold it
    // rather than scrolling to and focusing something the reader cannot see.
    const folded = target.closest("details");
    if (folded) (folded as HTMLDetailsElement).open = true;
    if (focusedKey !== id) {
      focusedKey = id;
      const bring = (): void => {
        const reduce =
          typeof window.matchMedia === "function" &&
          window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (typeof target.scrollIntoView === "function") {
          target.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
        }
        target.focus({ preventScroll: true });
      };
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(bring);
      else bring();
    }
  });

  return (
    <section
      class="page active"
      data-page="operations"
      ref={(el) => {
        rootEl = el;
      }}
    >
      <section class="operations" aria-labelledby="page-title">
        <div class="operations-head">
          <label class="fleet-toggle" for="show-screeners">
            <input
              id="show-screeners"
              type="checkbox"
              checked={showScreeners()}
              onChange={(ev) => setShowScreeners(ev.currentTarget.checked)}
            />
            <span class="fleet-option fleet-option-validators">Validators</span>
            <span class="fleet-option fleet-option-screeners">Screeners</span>
            <span class="sr-only">Show screeners</span>
          </label>
        </div>

        <div class="fleet-atlas">
          <div class="pipeline-map">
            <div class="atlas-label">
              <div>
                <h2 class="atlas-title">Submission pipeline</h2>
                <span class="atlas-note">
                  Mechanical admission builds a verified image before validators. Source integrity
                  review happens later only for qualifying or anomalous results.
                </span>
                <span class="atlas-note" id="operations-snapshot" aria-live="polite">
                  {snapshotNote()}
                </span>
              </div>
            </div>
            <RescreenNotice entries={pipelineEntries()} unavailable={opsUnavailable()} />
            <PipelineBoard
              entries={pipelineEntries()}
              statusCounts={statusCounts()}
              unavailable={opsUnavailable()}
              loading={opsLoading()}
              screeners={latest(screeners) ?? null}
              activeVersion={benchVersion()}
            />
            <IntegrityReviewBranch
              entries={pipelineEntries()}
              statusCounts={statusCounts()}
              unavailable={opsUnavailable()}
              loading={opsLoading()}
            />
          </div>
          <FleetLedger counts={fleet().counts} />
        </div>

        <div class="fleet-table-head">
          <div>
            <h2>Fleet health</h2>
            <span class="hint" id="fleet-summary" aria-live="polite">
              {fleetSummary()}
            </span>
          </div>
        </div>
        <div
          class="board validators"
          tabindex="0"
          role="region"
          aria-label="Fleet health table, horizontally scrollable on small screens"
        >
          <table class="fleet-table" aria-label={fleet().Kind + " fleet health"} id="fleet-table">
            <thead>
              <tr>
                <th scope="col" id="fleet-node-heading" style="width:240px">
                  {fleet().Kind}
                </th>
                <th scope="col" style="width:100px">
                  Status
                </th>
                <th scope="col" style="width:88px">
                  First seen
                </th>
                <th scope="col" style="width:96px">
                  Last heartbeat
                </th>
                <th scope="col" class="fleet-work-col" style="width:248px">
                  Current work
                </th>
                <th scope="col" style="width:108px">
                  Version
                </th>
                <th scope="col" style="width:88px">
                  CPU
                </th>
                <th scope="col" style="width:88px">
                  Memory
                </th>
                <th scope="col" style="width:88px">
                  Disk
                </th>
                <th scope="col" style="width:78px">
                  Containers
                </th>
              </tr>
            </thead>
            <tbody id="fleet-rows">
              {fleet().unavailable ? (
                <EmptyRow colspan={10}>
                  {fleet().Kind + " status is temporarily unavailable."}
                </EmptyRow>
              ) : fleet().loading ? (
                <EmptyRow colspan={10}>{"Loading " + fleet().kind + "…"}</EmptyRow>
              ) : !fleet().entries.length ? (
                <EmptyRow colspan={10}>
                  {"No active " + fleet().singular + " software reports."}
                </EmptyRow>
              ) : (
                <For each={fleet().entries}>
                  {(entry) => (
                    <FleetRow
                      entry={entry}
                      singular={fleet().singular}
                      names={nameData().names}
                      slotPolicy={fleet().slotPolicy}
                      benchVersion={benchVersion()}
                      highlightId={highlightId()}
                    />
                  )}
                </For>
              )}
            </tbody>
          </table>
        </div>
        <details class="fleet-retired" id="fleet-retired" hidden={!fleet().retired.length}>
          <summary>
            <strong id="fleet-retired-title">
              {fleet().singular === "screener" ? "Recently offline" : "Inoperative validators"}
            </strong>
            <span id="fleet-retired-summary">{retiredSummary()}</span>
          </summary>
          <p class="fleet-retired-note" id="fleet-retired-note">
            {retiredNote()}
          </p>
          <div
            class="board validators"
            tabindex="0"
            role="region"
            aria-label="Offline fleet nodes, horizontally scrollable on small screens"
          >
            <table
              class="fleet-table fleet-retired-table"
              id="fleet-retired-table"
              aria-label={
                fleet().singular === "screener" ? "Offline screeners" : "Offline validators"
              }
            >
              <thead>
                <tr>
                  <th scope="col" id="fleet-retired-node-heading" style="width:240px">
                    {fleet().Kind}
                  </th>
                  <th scope="col" style="width:100px">
                    Status
                  </th>
                  <th scope="col" style="width:130px">
                    Last heartbeat
                  </th>
                  <th scope="col" style="width:180px">
                    Last reported state
                  </th>
                  <th scope="col" style="width:108px">
                    Version
                  </th>
                </tr>
              </thead>
              <tbody id="fleet-retired-rows">
                <For each={fleet().retired}>
                  {(entry) => (
                    <RetiredFleetRow
                      entry={entry}
                      singular={fleet().singular}
                      names={nameData().names}
                      benchVersion={benchVersion()}
                      highlightId={highlightId()}
                    />
                  )}
                </For>
              </tbody>
            </table>
          </div>
        </details>
      </section>
    </section>
  );
}
