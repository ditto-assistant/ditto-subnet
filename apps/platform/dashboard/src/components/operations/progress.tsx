// Live benchmark / screening progress (monolith benchmarkStageLabel
// 6914–6926, benchmarkProgressText 6928–6950, renderBenchmarkProgress
// 6952–6996, screenerStageLabel 8221–8240, renderScreenerProgress 8252–8261,
// renderPipelineScreenerProgress 8276–8282, updateElapsedTimes 8284–8291).
// The original re-stamped every [data-started-at] node from one 1 s
// interval; here each ElapsedTime node carries its own per-second signal,
// which preserves the observable contract (a timer that visibly counts) with
// no document-wide scan.
import { Show, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { agentName, elapsedDuration } from "../../lib/format";
import type { FleetEntry, ScreeningProgress } from "../../types/fleet";
import type { BenchmarkProgress } from "../../types/pipeline";
import { EntityButton } from "../ui/EntityButton";

/** Stage → human label; anything unknown is an explicit absence, never a
 * guess (6914–6926). */
export function benchmarkStageLabel(stage?: string | null): string {
  const labels: Record<string, string> = {
    preparing: "Preparing artifact",
    building_harness: "Loading harness image",
    generating_dataset: "Generating dataset",
    starting_harness: "Starting harness",
    running_benchmark: "Running benchmark",
    finalizing: "Scoring and finalizing",
    submitting_result: "Signing and submitting result",
    failed_retrying: "Failed, retrying",
  };
  return (stage != null && labels[stage]) || "Benchmark progress unavailable";
}

/** The one-line progress sentence (6928–6950). A stalled run says what it
 * has actually done ("3 of 281 checks after 45m"), not just how long it has
 * been going — "no progress for 45m" reads as a reporting gap and sends the
 * operator to the wrong place. The validator is still heartbeating here; a
 * quiet validator shows up separately as offline or stale. */
export function benchmarkProgressText(progress: BenchmarkProgress | null | undefined): string {
  if (progress?._telemetry_delayed) {
    let lastReported =
      progress.percent == null
        ? benchmarkStageLabel(progress.stage)
        : "last reported " + progress.percent + "%";
    if (progress.completed_checks != null && progress.total_checks != null) {
      lastReported +=
        " · " + progress.completed_checks + " of " + progress.total_checks + " checks";
    }
    return "Progress update delayed · " + lastReported;
  }
  if (!progress || !progress.stage) {
    // Ticket issuance and the validator's first signed slot heartbeat are
    // separate writes. During the platform's one-minute assignment handoff
    // grace this is an expected transition, not missing telemetry. Keep the
    // stronger warning for a lease that remains silent past that window.
    const started = progress?.started_at ? new Date(progress.started_at).getTime() : NaN;
    if (Number.isFinite(started) && Date.now() - started < 60000) {
      return "Starting benchmark · awaiting first progress";
    }
    return "Benchmark progress not reported";
  }
  if (progress.stage === "failed_retrying") return "Failed · a new attempt will start shortly";
  if (progress.stage === "waiting_for_relay") {
    let waitingText = "Waiting for relay";
    if (progress.percent != null) waitingText += " · benchmark " + progress.percent + "%";
    if (progress.completed_checks != null && progress.total_checks != null) {
      waitingText += " · " + progress.completed_checks + " of " + progress.total_checks + " checks";
    }
    return waitingText;
  }
  if (progress.stalled) {
    const stalledFor = progress.started_at ? " after " + elapsedDuration(progress.started_at) : "";
    if (progress.completed_checks != null && progress.total_checks != null) {
      return (
        benchmarkStageLabel(progress.stage) +
        " · stalled · " +
        progress.completed_checks +
        " of " +
        progress.total_checks +
        " checks" +
        stalledFor
      );
    }
    return benchmarkStageLabel(progress.stage) + " · stalled" + stalledFor;
  }
  if (progress.percent == null) return benchmarkStageLabel(progress.stage) + " · working…";
  let text = "Benchmark " + progress.percent + "%";
  if (progress.completed_checks != null && progress.total_checks != null) {
    text += " · " + progress.completed_checks + " of " + progress.total_checks + " checks";
  }
  return text;
}

/** Screener stage → label; the source-review percentage is split into the
 * L1 (0–50) and L2/L3 (50–100) bands (8221–8240). */
export function screenerStageLabel(stage?: string | null): string {
  const sourceReview = /^source_review_(\d+)$/.exec(stage || "");
  if (sourceReview) {
    const reviewProgress = Math.max(0, Math.min(100, Number(sourceReview[1]) || 0));
    if (reviewProgress <= 50) {
      return "L1 source review · " + Math.min(100, reviewProgress * 2) + "%";
    }
    return "L2/L3 deep review · " + Math.min(100, (reviewProgress - 50) * 2) + "%";
  }
  const labels: Record<string, string> = {
    preparing: "Preparing",
    downloading: "Downloading submission",
    validating: "Validating submission",
    building: "Building image",
    starting: "Starting service",
    health_check: "Checking health",
    submitting: "Submitting result",
  };
  return (stage != null && labels[stage]) || "Screening";
}

/** A per-second elapsed counter (`updateElapsedTimes`' visible contract). */
export function ElapsedTime(props: {
  class: string;
  startedAt: string | null | undefined;
}): JSX.Element {
  const [now, setNow] = createSignal(Date.now());
  onMount(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    onCleanup(() => clearInterval(timer));
  });
  const text = (): string => {
    now();
    return elapsedDuration(props.startedAt);
  };
  return (
    <span class={props.class} data-started-at={props.startedAt ?? ""}>
      {text()}
    </span>
  );
}

function AgentLink(props: { progress: BenchmarkProgress }): JSX.Element {
  return (
    <span class="benchmark-agent" title={String(props.progress.agent_id || "")}>
      <EntityButton
        kind="agent"
        id={props.progress.agent_id}
        label={
          (props.progress.agent_name || "Unnamed agent") +
          " · " +
          String(props.progress.agent_id || "").slice(0, 8)
        }
      />
    </span>
  );
}

/**
 * One benchmark run's live progress (renderBenchmarkProgress 6952–6996).
 * running_benchmark reports a real percentage; the quick pre-run stages
 * report none, so those get an animated indeterminate bar so they read as
 * live. A stalled or failed run is pinned to a static warning bar, never
 * animated.
 */
export function BenchmarkProgressView(props: {
  progress: BenchmarkProgress;
  showAgent?: boolean;
}): JSX.Element {
  const p = () => props.progress;
  const label = () => benchmarkStageLabel(p().stage);
  const text = () => benchmarkProgressText(p());
  const stalled = () => !!p().stalled;
  const failed = () => p().stage === "failed_retrying";
  const versionLabel = () => (p().bench_version ? "Bench v" + p().bench_version : "");
  // The server already holds percent below 100 until the run reaches a
  // terminal stage, so clamp only to the bar's own domain. Clamping to 95
  // here would re-flatten the exact percentage the API publishes.
  const determinate = () => p().percent != null && !stalled();
  const cls = () =>
    "benchmark-progress" +
    (failed() ? " failed" : "") +
    (stalled() ? " stalled" : determinate() || failed() ? "" : " indeterminate");
  const ariaText = () => (versionLabel() ? versionLabel() + ". " : "") + label() + ". " + text();
  const value = () => Math.max(0, Math.min(100, Number(p().percent) || 0));
  const versionChip = (): JSX.Element => (
    <Show when={versionLabel()}>
      <span class="benchmark-version-chip">{versionLabel()}</span>
    </Show>
  );
  return (
    <Show
      when={p().stage}
      fallback={
        <span class="benchmark-progress unknown">
          <Show when={props.showAgent}>
            <AgentLink progress={p()} />
          </Show>
          <span class="benchmark-stage-label">
            {versionChip()}
            {text()}
          </span>
        </span>
      }
    >
      <span class={cls()}>
        <span class="benchmark-stage-label">
          {versionChip()}
          <span>
            {label()}
            <Show when={p().started_at}>
              {(startedAt) => (
                <>
                  {" · "}
                  <ElapsedTime class="benchmark-progress-time" startedAt={startedAt()} />
                </>
              )}
            </Show>
          </span>
        </span>
        <Show when={props.showAgent}>
          <AgentLink progress={p()} />
        </Show>
        <Show
          when={determinate()}
          fallback={
            <span class="bench-bar" role="img" aria-label={ariaText()}>
              <i />
            </span>
          }
        >
          <progress max="100" value={value()} aria-label={ariaText()} />
        </Show>
        <span class="benchmark-progress-text">{text()}</span>
      </span>
    </Show>
  );
}

/** A screener's live stage inside the fleet table, naming the agent under
 * screening (renderScreenerProgress 8252–8261). */
export function ScreenerProgressView(props: { entry: FleetEntry }): JSX.Element {
  const progress = () => props.entry.screening_progress as ScreeningProgress;
  return (
    <span class="screener-progress">
      <span class="screener-progress-stage">
        {screenerStageLabel(progress().stage)}
        {" · "}
        <ElapsedTime class="screener-progress-time" startedAt={progress().started_at} />
      </span>
      <span class="current-agent" title={String(props.entry.active_agent_id || "")}>
        <EntityButton
          kind="agent"
          id={String(props.entry.active_agent_id || "")}
          label={
            agentName(props.entry.active_agent_name) +
            (props.entry.active_agent_id
              ? " · " + String(props.entry.active_agent_id).slice(0, 8)
              : "")
          }
        />
      </span>
    </span>
  );
}

/** The pipeline-card variant: stage + elapsed only, no agent line — the card
 * itself names the agent (renderPipelineScreenerProgress 8276–8282). */
export function PipelineScreenerProgressView(props: { progress: ScreeningProgress }): JSX.Element {
  return (
    <span class="screener-progress">
      <span class="screener-progress-stage">
        {screenerStageLabel(props.progress.stage)}
        {" · "}
        <ElapsedTime class="screener-progress-time" startedAt={props.progress.started_at} />
      </span>
    </span>
  );
}
