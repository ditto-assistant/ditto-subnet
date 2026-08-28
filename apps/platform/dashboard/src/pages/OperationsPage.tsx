// The fleet page (monolith markup 2742–2862 + loadOperations 9434–9462,
// loadValidatorNames 9464–9484, loadScreeners 9486–9500, renderFleet
// 8922–9114, resolveEntityRoute fleet targets 9365–9398): validator and
// screener capacity plus Targon build provenance. The submission-pipeline
// atlas lives on its own page (PipelinePage); both consume exactly ONE
// /public/operations snapshot per tick through operations-shared. Validator
// display names arrive on a separate feed and are optional untrusted
// decoration: reset on every refetch, rendered as inert text, never a
// substitute for the hotkey identity.
import { For, Show, createEffect, createMemo, createSignal, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { reconciledList } from "../data/reconciled";
import { FleetLedger, FleetRow, RetiredFleetRow } from "../components/operations/FleetTable";
import { SubmissionBuildLane } from "../components/operations/SubmissionBuildLane";
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
import { EmptyRow } from "../components/ui/States";
import { weightsResource } from "../data/weights";
import type { WeightsSnapshot } from "../types/leaderboard";
import { operationsResource } from "../data/operations";
import { useEndpoint } from "../data/useEndpoint";
import type { ResourceState } from "../data/useEndpoint";
import { REFRESH_MS } from "../lib/config";
import { validatorWeightViews } from "../lib/scoring";
import type { ValidatorWeightView } from "../lib/scoring";
import { entityRoute } from "../stores/routeStore";
import type { FleetReport, OperationsPayload, ValidatorNamesPayload } from "../types/fleet";
import { latest, useOperationsSnapshot } from "./operations-shared";

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

type OperationsView = "validators" | "screeners" | "builds";

const OPERATIONS_VIEWS: readonly OperationsView[] = ["validators", "screeners", "builds"];

const OPERATIONS_VIEW_LABELS: Record<OperationsView, string> = {
  validators: "Validators",
  screeners: "Screeners",
  builds: "Targon builds",
};

export function OperationsPage(
  props: {
    operations?: ResourceState<OperationsPayload>;
  } = {},
): JSX.Element {
  const operations = props.operations ?? operationsResource();
  if (!props.operations) onMount(() => operations.refresh());
  const validatorNames = useEndpoint<ValidatorNamesPayload>("/public/validator-names", {
    pollMs: REFRESH_MS,
  });
  const screeners = useEndpoint<FleetReport>("/public/screeners", { pollMs: REFRESH_MS });
  const weights = weightsResource();

  // Revealed on-chain weight vectors, keyed by validator hotkey. A failed
  // weights tick keeps the previous matrix (the leaderboard store's
  // anti-flicker rule); the API also serves last-known-good with `stale` set.
  const chainWeights = createMemo<WeightsSnapshot | null>(
    (prev) => latest(weights) ?? prev ?? null,
    null,
  );
  const chainVectors = createMemo<{
    byValidator: Record<string, ValidatorWeightView>;
    block: number | null;
    stale: boolean;
  } | null>(() => {
    const snapshot = chainWeights();
    const views = validatorWeightViews(snapshot);
    if (!views) return null;
    const byValidator: Record<string, ValidatorWeightView> = {};
    views.forEach((view) => {
      if (view.validatorHotkey) byValidator[view.validatorHotkey] = view;
    });
    return { byValidator, block: snapshot?.block ?? null, stale: Boolean(snapshot?.stale) };
  });

  const snap = useOperationsSnapshot(operations);
  const ops = snap.ops;
  const opsUnavailable = snap.opsUnavailable;

  const [operationsView, setOperationsView] = createSignal<OperationsView>("validators");

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

  const fleet = createMemo<FleetView>(() => {
    const screenersShown = operationsView() === "screeners";
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

  // A fleet node's identity, for keeping its row across a poll. None of the
  // three addressing fields is required by the wire type, so the key is
  // derived rather than named; position is the last resort and is no worse
  // than the positional identity <For> fell back to before.
  const fleetKey = (entry: FleetEntryExt, index: number): string =>
    String(entry.validator_hotkey || entry.screener_hotkey || entry.instance_id || "#" + index);

  // The operations snapshot re-reports the whole fleet every 5s with fresh
  // objects. Keyed by reference, <For> rebuilt every row on every tick, which
  // shut any open "inactive slots" disclosure and dropped focus and hover.
  const fleetEntries = reconciledList<FleetEntryExt>(() => fleet().entries, fleetKey);
  const fleetRetired = reconciledList<FleetEntryExt>(() => fleet().retired, fleetKey);

  /** Exception-only: the head speaks when the reader cannot trust the table,
   * and is silent otherwise. Everything the old summary line stated is on the
   * page already — each node's own verdict in its identity cell, the
   * inoperative count on the fold that holds those rows, and the snapshot age
   * in the header pill, which reads the same generated_at this did. */
  const fleetSummary = createMemo(() => {
    const view = fleet();
    if (view.unavailable) return view.Kind + " status unavailable";
    if (view.loading) return "Loading " + view.singular + " status…";
    if (!view.entries.length) return "No active " + view.kind + " reporting";
    return "";
  });

  /** The healthy provenance note restates the header pill; only its degraded
   * branches carry something the pill cannot say, since the pill tracks the
   * leaderboard feed rather than this snapshot. */
  const snapshotAlert = () =>
    snap.opsUnavailable() || snap.refreshDelayed() || snap.opsLoading() ? snap.snapshotNote() : "";

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
    setOperationsView(entity.kind === "screener" ? "screeners" : "validators");
  });

  function selectOperationsView(view: OperationsView, focus = false): void {
    setOperationsView(view);
    if (focus) document.getElementById("operations-tab-" + view)?.focus();
  }

  function onOperationsTabKeyDown(ev: KeyboardEvent, view: OperationsView): void {
    const index = OPERATIONS_VIEWS.indexOf(view);
    let next = index;
    if (ev.key === "ArrowRight") next = (index + 1) % OPERATIONS_VIEWS.length;
    else if (ev.key === "ArrowLeft")
      next = (index - 1 + OPERATIONS_VIEWS.length) % OPERATIONS_VIEWS.length;
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = OPERATIONS_VIEWS.length - 1;
    else return;
    ev.preventDefault();
    selectOperationsView(OPERATIONS_VIEWS[next]!, true);
  }

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
        <div class="operations-workspace">
          {/* The page title and its subtitle name this surface one line above;
              a second heading and a second explainer only said it again. The
              tab bar is the head now, and the snapshot note joins it only when
              the shared feed is degraded (it stays mounted so its live region
              can announce into it). */}
          <div class="operations-head">
            <div class="operations-tabs" role="tablist" aria-label="Operational capacity views">
              <For each={OPERATIONS_VIEWS}>
                {(view) => (
                  <button
                    type="button"
                    id={"operations-tab-" + view}
                    class="operations-tab"
                    role="tab"
                    aria-selected={operationsView() === view ? "true" : "false"}
                    aria-controls={"operations-panel-" + view}
                    tabindex={operationsView() === view ? 0 : -1}
                    onClick={() => selectOperationsView(view)}
                    onKeyDown={(ev) => onOperationsTabKeyDown(ev, view)}
                  >
                    {OPERATIONS_VIEW_LABELS[view]}
                  </button>
                )}
              </For>
            </div>
            <p class="hint" id="operations-snapshot" aria-live="polite">
              {snapshotAlert()}
            </p>
          </div>

          <Show
            when={operationsView() !== "builds"}
            fallback={
              <section
                id="operations-panel-builds"
                class="operations-panel"
                role="tabpanel"
                aria-labelledby="operations-tab-builds"
                tabindex="0"
              >
                <SubmissionBuildLane
                  snapshot={ops()?.submission_builds}
                  unavailable={opsUnavailable()}
                  loading={snap.opsLoading()}
                />
              </section>
            }
          >
            <section
              id={"operations-panel-" + operationsView()}
              class="operations-panel"
              role="tabpanel"
              aria-labelledby={"operations-tab-" + operationsView()}
              tabindex="0"
            >
              {/* The selected tab names the panel and the table names itself,
                  so the head holds no heading — only the two exception
                  channels, and the rule that separates tabs from table. */}
              <div class="fleet-table-head">
                <span class="hint" id="fleet-summary" aria-live="polite">
                  {fleetSummary()}
                </span>
                <FleetLedger counts={fleet().counts} />
              </div>
              <div
                class="board validators"
                tabindex="0"
                role="region"
                aria-label="Fleet health table, shown as stacked cards on small screens"
              >
                <table
                  class="fleet-table"
                  aria-label={fleet().Kind + " fleet health"}
                  id="fleet-table"
                >
                  <thead>
                    <tr>
                      <th scope="col" id="fleet-node-heading" style="width:214px">
                        {fleet().Kind}
                      </th>
                      <th scope="col" class="fleet-work-col">
                        Current work
                      </th>
                      <th scope="col" style="width:176px">
                        Host
                      </th>
                    </tr>
                  </thead>
                  <tbody id="fleet-rows">
                    {fleet().unavailable ? (
                      <EmptyRow colspan={3}>
                        {fleet().Kind + " status is temporarily unavailable."}
                      </EmptyRow>
                    ) : fleet().loading ? (
                      <EmptyRow colspan={3}>{"Loading " + fleet().kind + "…"}</EmptyRow>
                    ) : !fleet().entries.length ? (
                      <EmptyRow colspan={3}>
                        {"No active " + fleet().singular + " software reports."}
                      </EmptyRow>
                    ) : (
                      <For each={fleetEntries()}>
                        {(entry) => (
                          <FleetRow
                            entry={entry}
                            singular={fleet().singular}
                            names={nameData().names}
                            slotPolicy={fleet().slotPolicy}
                            benchVersion={benchVersion()}
                            highlightId={highlightId()}
                            chainVectors={fleet().singular === "validator" ? chainVectors() : null}
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
                    {fleet().singular === "screener"
                      ? "Recently offline"
                      : "Inoperative validators"}
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
                  aria-label="Offline fleet nodes, shown as stacked cards on small screens"
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
                        <th scope="col" id="fleet-retired-node-heading" style="width:280px">
                          {fleet().Kind}
                        </th>
                        <th scope="col">Last reported state</th>
                        <th scope="col" style="width:150px">
                          Host
                        </th>
                      </tr>
                    </thead>
                    <tbody id="fleet-retired-rows">
                      <For each={fleetRetired()}>
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
          </Show>
        </div>
      </section>
    </section>
  );
}
