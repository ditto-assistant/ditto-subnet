// Composition root: the shell (sidebar, header with global search + status
// pill, unavailable banner, entity panel, tooltips) around the six hash-
// routed pages, plus the app-level data layer — the endpoints the monolith's
// load() tick owns (leaderboard, weights, rollout, health, operations,
// validator-names, screeners, timeline, bench-config, glossary), refreshed
// every REFRESH_MS while visible with a catch-up on return, an
// OPS_REFRESH_MS fast tick for the operations snapshot while that page is
// open, bench-config at most every 5 minutes, and the glossary once ever.
import {
  Match,
  Switch,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import type { JSX } from "solid-js";

import { EntityPanel } from "./components/EntityPanel";
import { GlobalSearch } from "./components/shell/GlobalSearch";
import { Sidebar } from "./components/shell/Sidebar";
import { UnavailableBanner } from "./components/ui/States";
import { installTooltips } from "./components/ui/Tooltip";
import {
  agentCardOpen,
  hydrateOnAgentCardClose,
  refreshAllEndpoints,
  useEndpoint,
} from "./data/useEndpoint";
import { operationsResource } from "./data/operations";
import { weightsResource } from "./data/weights";
import { invalidateSharedOperations } from "./data/operations-cache";
import type { ResourceState } from "./data/useEndpoint";
import { OPS_REFRESH_MS, REFRESH_MS } from "./lib/config";
import { relTime } from "./lib/format";
import { PAGES } from "./lib/router";
import { installScrollMemory } from "./lib/scroll";
import { benchmarkDisplayVersion, leaderboardBenchState } from "./lib/bench-state";
import { isFinalized, isOlderRun, rankEntries, rolloutSettledView } from "./lib/scoring";
import { currentPage, initRouteListeners, syncFromLocation } from "./stores/routeStore";
import type { BenchConfigPayload, GlossaryPayload, TimelinePayload } from "./types/bench";
import type { FleetReport, HealthPayload, ValidatorNamesPayload } from "./types/fleet";
import type { LeaderboardEntry, LeaderboardPayload, RolloutState } from "./types/leaderboard";
import type { PipelineEntry } from "./types/pipeline";

import { BenchmarkPage } from "./pages/BenchmarkPage";
import { LeaderboardPage } from "./pages/LeaderboardPage";
import { OperationsPage } from "./pages/OperationsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PipelinePage } from "./pages/PipelinePage";
import { AthPage } from "./pages/AthPage";
import { ReviewsPage } from "./pages/ReviewsPage";
import { SubmissionsPage } from "./pages/SubmissionsPage";

/** Read a resource's latest value, treating the errored state as absent —
 * the API-failure rule renders stated absence, never stale-as-fresh data. */
function latest<T>(resource: ResourceState<T>): T | undefined {
  if (resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

export default function App(): JSX.Element {
  onMount(() => {
    syncFromLocation();
    initRouteListeners(() => undefined);
    onCleanup(installTooltips());
    // Native scroll restoration fires before the async reads paint, so it
    // always clamped to the top; this restores once the rows exist.
    onCleanup(installScrollMemory());
  });

  // ── App-level endpoint resources ──────────────────────────────────────────
  const leaderboard = useEndpoint<LeaderboardPayload>("/public/leaderboard");
  const weights = weightsResource();
  const rollout = useEndpoint<RolloutState>("/public/bench/rollout");
  const health = useEndpoint<HealthPayload>("/public/health");
  const operations = operationsResource();
  const validatorNames = useEndpoint<ValidatorNamesPayload>("/public/validator-names");
  const screeners = useEndpoint<FleetReport>("/public/screeners");
  const timeline = useEndpoint<TimelinePayload>("/public/bench/timeline");
  const benchConfig = useEndpoint<BenchConfigPayload>("/public/bench/config");
  // The glossary is fetched once, ever (loadGlossary, monolith 9513–9529).
  useEndpoint<GlossaryPayload>("/public/bench/glossary");

  let benchConfigAt = Date.now();

  const [manualRefresh, setManualRefresh] = createSignal(false);

  function refreshTick(manual: boolean): void {
    leaderboard.refresh();
    weights.refresh();
    rollout.refresh();
    health.refresh();
    timeline.refresh();
    // The operations trio backs the search corpus and the bench badge; keep
    // it fresh on its page and keep retrying after a failure so the corpus
    // completes (the monolith's wantOps rule).
    if (manual) invalidateSharedOperations();
    // One tab-local resource owns this feed. The broker underneath elects a
    // single same-origin network caller and fans the snapshot out to peers.
    operations.refresh();
    if (
      manual ||
      currentPage() === "operations" ||
      currentPage() === "pipeline" ||
      Boolean(validatorNames.error()) ||
      Boolean(screeners.error())
    ) {
      validatorNames.refresh();
      screeners.refresh();
    }
    // bench/config is effectively static (max-age 300); at most every 5 min.
    if (manual || Date.now() - benchConfigAt >= 300_000) {
      benchConfigAt = Date.now();
      benchConfig.refresh();
    }
  }

  function refreshAll(): void {
    setManualRefresh(true);
    refreshTick(true);
    // Data owned by pages and module-scope stores registers itself; the
    // shell's refresh reaches all of it, like the monolith's single load().
    refreshAllEndpoints();
  }

  createEffect(() => {
    if (manualRefresh() && !leaderboard.loading()) setManualRefresh(false);
  });

  // An agent deep link is an entity-first surface: while its card is open the
  // global reads pause (agentCardOpen, useEndpoint.ts), and closing it hydrates
  // the dashboard exactly once (#648). One pass over the registry, not
  // refreshTick: it reaches the shell's own endpoints as well as the ones pages
  // and module-scope stores own, and every section is stale by now — the same
  // seed-everything shape the monolith's close-hydrate had (load() with
  // bootComplete still false).
  hydrateOnAgentCardClose(() => {
    operations.refresh();
    refreshAllEndpoints();
  });

  onMount(() => {
    // Background tabs skip network refreshes entirely and catch up once on
    // return, so idle dashboards stop polling the API. Every unattended read
    // goes through backgroundTick, so the entity-first pause is one rule
    // rather than one per timer.
    let refreshStale = false;
    const backgroundTick = (): void => {
      if (agentCardOpen()) return;
      refreshTick(false);
    };
    const master = setInterval(() => {
      if (document.hidden) {
        refreshStale = true;
        return;
      }
      backgroundTick();
    }, REFRESH_MS);
    // The pipeline and fleet pages are the live ones: poll just the shared
    // operations snapshot on a tighter cadence while a viewer is on either.
    const ops = setInterval(() => {
      if (document.hidden) return;
      if (agentCardOpen()) return;
      if (currentPage() === "operations" || currentPage() === "pipeline") operations.refresh();
    }, OPS_REFRESH_MS);
    const onVisibility = (): void => {
      if (!document.hidden && refreshStale) {
        refreshStale = false;
        backgroundTick();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    onCleanup(() => {
      clearInterval(master);
      clearInterval(ops);
      document.removeEventListener("visibilitychange", onVisibility);
    });
  });

  // ── Derived shell state ───────────────────────────────────────────────────
  const lb = () => latest(leaderboard);
  const ops = () => latest(operations);

  const settledView = createMemo(() => {
    const d = lb();
    return d ? rolloutSettledView(d) : false;
  });

  // Last-good board entries (kept across a failed tick so the search corpus
  // and the miner drilldown do not evaporate; the board itself renders its
  // explicit unavailable state from the resource error).
  const entries = createMemo<(LeaderboardEntry & { rank: number | null })[]>(
    (prev) => {
      const d = lb();
      if (!d) return prev;
      return rankEntries(d.entries ?? [], settledView());
    },
    [] as (LeaderboardEntry & { rank: number | null })[],
  );

  const pipelineEntries = createMemo<PipelineEntry[]>((prev) => {
    const d = ops();
    if (!d) return prev;
    return d.activity?.entries ?? [];
  }, [] as PipelineEntry[]);

  const names = createMemo<Record<string, string>>(() => {
    // Rebuilt from scratch on every payload: display names are optional
    // untrusted decoration and reset on refetch.
    const out: Record<string, string> = {};
    (latest(validatorNames)?.validators ?? []).forEach((entry) => {
      if (entry.validator_hotkey && entry.display_name) {
        out[entry.validator_hotkey] = entry.display_name;
      }
    });
    return out;
  });

  // Benchmark authority fold (monolith render() 4835–4853 + loadOperations
  // 9434–9441): the leaderboard payload and the operations snapshot feed one
  // active/desired/current triple; nothing is ever a literal.
  const bench = createMemo(() => {
    const d = lb();
    const o = ops();
    const maxBv = (d?.entries ?? [])
      .map((e) => e.bench_version)
      .filter((v): v is number => v != null)
      .reduce((a, b) => Math.max(a, b), 0);
    const state = d
      ? leaderboardBenchState(
          d.selection_mode,
          d.current_bench_version,
          d.active_bench_version,
          d.desired_bench_version,
          maxBv || o?.active_bench_version,
        )
      : null;
    const active = state?.active || Number(o?.active_bench_version) || null;
    const desired = state?.desired || Number(o?.desired_bench_version) || active;
    const current =
      state?.selected ||
      Number(o?.active_bench_version) ||
      Number(latest(benchConfig)?.bench_version) ||
      null;
    const status = o?.benchmark_rollout_status || "inactive";
    return { active, desired, current, status };
  });

  const settledVersion = () => bench().active || bench().current;
  const benchHasOlderRuns = createMemo(() =>
    entries().some((e) => isFinalized(e) && isOlderRun(e, settledVersion())),
  );
  const displayVersion = () => benchmarkDisplayVersion(bench().active, bench().current);

  // ── Status pill (setStatus 4113–4118 + load() 9699/9704 + render 4858) ────
  const status = createMemo<{ mode: "" | "live" | "error"; text: string }>(() => {
    if (manualRefresh()) return { mode: "", text: "Refreshing…" };
    if (leaderboard.error()) return { mode: "error", text: "Data unavailable" };
    const d = lb();
    if (!d) return { mode: "", text: "Connecting…" };
    return { mode: "live", text: "Live · " + (d.generated_at ? relTime(d.generated_at) : "–") };
  });

  // ── Titles / subtitles ────────────────────────────────────────────────────
  const pageSub = () => {
    if (currentPage() === "benchmark") {
      const v = displayVersion();
      if (v) return "What DittoBench v" + v + " measures and the frozen scoring setup";
    }
    return PAGES[currentPage()].sub;
  };

  createEffect(() => {
    // PAGES is deliberately mutable: keep the shared record in step for
    // non-reactive consumers (monolith applyBenchVersion 9745).
    const v = displayVersion();
    if (v) PAGES.benchmark.sub = "What DittoBench v" + v + " measures and the frozen scoring setup";
  });

  createEffect(() => {
    document.title = "Ditto SN118 · " + PAGES[currentPage()].title;
  });

  // These app-level resources exist for the pages that consume them; the
  // shell reads none of their payloads directly — except `weights`, whose
  // `epoch` drives the rail's payout clock.
  void rollout;
  void health;
  void timeline;
  void screeners;

  return (
    <>
      <a class="skip-link" href="#main-content">
        Skip to content
      </a>
      <div
        id="copy-status"
        class="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      />
      <div class="layout">
        <Sidebar
          bench={{
            active: bench().active || bench().current,
            desired: bench().desired,
            status: bench().status,
            hasOlderRuns: benchHasOlderRuns(),
          }}
          displayVersion={displayVersion()}
          epoch={() => latest(weights)?.epoch ?? null}
          onRefresh={refreshAll}
        />
        <main class="main" id="main-content">
          <div class="wrap">
            <header class="top">
              <div class="page-title">
                <h1 id="page-title">{PAGES[currentPage()].title}</h1>
                <div class="sub" id="page-sub">
                  {pageSub()}
                </div>
              </div>
              <div class="spacer" />
              <GlobalSearch miners={entries} submissions={pipelineEntries} />
              <span
                id="status"
                class={"pill" + (status().mode ? " " + status().mode : "")}
                role="status"
                aria-live="polite"
              >
                <span class="dot" aria-hidden="true" />
                <span id="status-text">{status().text}</span>
              </span>
            </header>
            <UnavailableBanner show={status().mode === "error"} />
            <Switch>
              <Match when={currentPage() === "overview"}>
                <OverviewPage operations={operations} />
              </Match>
              <Match when={currentPage() === "leaderboard"}>
                <LeaderboardPage />
              </Match>
              <Match when={currentPage() === "pipeline"}>
                <PipelinePage operations={operations} />
              </Match>
              <Match when={currentPage() === "operations"}>
                <OperationsPage operations={operations} />
              </Match>
              <Match when={currentPage() === "submissions"}>
                <SubmissionsPage />
              </Match>
              <Match when={currentPage() === "reviews"}>
                <ReviewsPage />
              </Match>
              <Match when={currentPage() === "ath"}>
                <AthPage />
              </Match>
              <Match when={currentPage() === "benchmark"}>
                <BenchmarkPage />
              </Match>
            </Switch>
          </div>
        </main>
      </div>
      <EntityPanel
        entries={entries}
        operations={ops}
        validatorNames={names}
        currentBench={() => bench().current}
        settledView={settledView}
      />
    </>
  );
}
