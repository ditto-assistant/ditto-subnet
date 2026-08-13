// The memory timeline section leading the overview rail (markup 2610–2627 +
// renderMemoryTimeline/loadMemoryTimeline, monolith 4292–4731). The chart is
// one innerHTML write built by memory-timeline.ts; this component owns the
// section chrome (title, lead, method disclosure, evidence links), the
// /public/bench/timeline resource with its last-good fallback, the field
// loading, the measured-width re-render, and the frame tooltips.
import { For, createEffect, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { REFRESH_MS } from "../../lib/config";
import type { TimelinePayload } from "../../types";
import { Tip } from "../ui/Tooltip";
import type { LeaderboardStore } from "../board/leaderboard-data";
import {
  THIRD_PARTY_HARNESSES,
  TIMELINE_MAX_ERAS,
  bindTimelineTooltips,
  harnessMethodText,
  loadMemoryField,
  memoryFieldRevision,
  memoryFieldSnapshot,
  memoryTimelineHtml,
} from "./memory-timeline";

export function HarnessComparison(props: { store: LeaderboardStore }): JSX.Element {
  const store = props.store;
  const timeline = useEndpoint<TimelinePayload>("/public/bench/timeline", { pollMs: REFRESH_MS });

  // Last-good data survives a failed tick; only a failure with nothing to
  // show renders the explicit unavailable state (load() catch, 4728–4730).
  const [lastData, setLastData] = createSignal<TimelinePayload | null>(null);
  const failed = (): boolean => Boolean(timeline.error());
  createEffect(() => {
    if (timeline.error()) return;
    let data: TimelinePayload | undefined;
    try {
      data = timeline.data();
    } catch {
      return;
    }
    if (!data) return;
    setLastData(data);
    const versions = (data.releases || []).map((release) => Number(release.bench_version));
    void loadMemoryField(versions, Math.max(...versions.concat([0])));
  });

  const shownVersions = createMemo<Record<number, boolean>>(() => {
    const data = lastData();
    const releases = ((data && data.releases) || [])
      .filter((release) => Number(release.bench_version) >= 2)
      .sort((a, b) => Number(a.bench_version) - Number(b.bench_version))
      .slice(-TIMELINE_MAX_ERAS);
    const shown: Record<number, boolean> = {};
    releases.forEach((release) => {
      shown[Number(release.bench_version)] = true;
    });
    return shown;
  });

  let section: HTMLElement | undefined;
  let chart: HTMLDivElement | undefined;
  // A poll returns a fresh object even when its chart-visible content is
  // unchanged. Keep the existing DOM in that case: replacing innerHTML would
  // restart every entrance animation, discard focus, and make the graph flash
  // as though it had new data.
  let renderedResult: string | null = null;
  // The chart lays itself out to its measured width, so a resize (or a phone
  // rotating) has to re-render rather than stretch. Only a material width
  // change re-renders, so dragging a window edge does not thrash the DOM.
  let renderedWidth = 0;
  const [widthTick, setWidthTick] = createSignal(0);
  onMount(() => {
    if (!chart || typeof ResizeObserver === "undefined") return;
    let pending: ReturnType<typeof setTimeout> | undefined;
    const observer = new ResizeObserver((entries) => {
      const next = Math.round((entries[0] as ResizeObserverEntry).contentRect.width);
      if (!next || Math.abs(next - renderedWidth) < 12) return;
      clearTimeout(pending);
      pending = setTimeout(() => {
        setWidthTick((tick) => tick + 1);
      }, 120);
    });
    observer.observe(chart);
    onCleanup(() => {
      clearTimeout(pending);
      observer.disconnect();
    });
  });

  createEffect(() => {
    widthTick();
    memoryFieldRevision();
    const data = lastData();
    const rollout = store.rollout();
    const championHotkey =
      store.emissions()?.allocation_mode === "score_ceiling_pool"
        ? null
        : (store.emissions()?.champion_miner_hotkey ?? null);
    const isFailed = failed();
    const target = chart;
    if (!target) return;
    if (!data && !isFailed) return; // keep the static loading state
    // The viewBox is measured, not fixed: roughly one unit per CSS pixel
    // keeps type at its real size and re-flows the plot instead of
    // squashing it (renderMemoryTimeline 4370–4376).
    const measured = Math.round((target.clientWidth || 960) - 2);
    renderedWidth = measured;
    // A sub-560px measure can mean a phone viewport or the desktop split's
    // rail; only the phone deserves the tall portrait aspect.
    const phoneViewport =
      typeof window.matchMedia === "function"
        ? !window.matchMedia("(min-width: 1181px)").matches
        : false;
    const snapshot = memoryFieldSnapshot();
    const result = memoryTimelineHtml(data, {
      width: measured,
      phoneViewport,
      rollout,
      championHotkey,
      fieldByVersion: snapshot.fieldByVersion,
      pendingByVersion: snapshot.pendingByVersion,
    });
    if (result.kind === "state") {
      const resultKey = "state:" + result.text;
      if (renderedResult === resultKey) return;
      renderedResult = resultKey;
      target.className = "harness-comparison-state";
      target.textContent = result.text;
      return;
    }
    const resultKey = "chart:" + result.html;
    if (renderedResult === resultKey) return;
    renderedResult = resultKey;
    target.className = "";
    target.innerHTML = result.html;
    if (section) bindTimelineTooltips(section);
  });

  return (
    <section
      class="harness-comparison"
      aria-labelledby="harness-comparison-title"
      ref={(el) => {
        section = el;
      }}
    >
      <div class="harness-comparison-head">
        <div class="harness-comparison-title">
          <h2 id="harness-comparison-title">How far miners have taken memory</h2>
          <p class="harness-comparison-lead">
            <Tip text="This isolates memory performance. The public leaderboard ranks agents by the full composite, not this subscore alone.">
              Memory subscores only
            </Tip>
            . Follow the best finalized miner as each benchmark generation unfolds, with Hermes
            Agent and OpenClaw measured retrospectively where reference runs are available.
          </p>
        </div>
      </div>
      <div class="harness-comparison-body">
        <div
          id="harness-comparison-chart"
          class="harness-comparison-state"
          ref={(el) => {
            chart = el;
          }}
        >
          Loading the benchmark memory timeline…
        </div>
        <details class="harness-comparison-method">
          <summary>Method and comparability caveats</summary>
          <div class="harness-comparison-method-body">
            <p id="harness-comparison-method">{harnessMethodText(shownVersions())}</p>
            <div id="harness-comparison-evidence" class="harness-comparison-evidence-links">
              <For each={THIRD_PARTY_HARNESSES}>
                {(evidence) => (
                  <a href={evidence.evidenceUrl} target="_blank" rel="noopener">
                    {evidence.label + " evidence ↗"}
                  </a>
                )}
              </For>
            </div>
          </div>
        </details>
      </div>
    </section>
  );
}
