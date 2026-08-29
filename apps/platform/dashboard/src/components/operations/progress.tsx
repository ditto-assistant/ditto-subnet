// Live benchmark / screening progress (monolith benchmarkStageLabel
// 6914–6926, benchmarkProgressText 6928–6950, renderBenchmarkProgress
// 6952–6996, screenerStageLabel 8221–8240, renderScreenerProgress 8252–8261,
// renderPipelineScreenerProgress 8276–8282, updateElapsedTimes 8284–8291).
// The original re-stamped every [data-started-at] node from one 1 s
// interval; here each ElapsedTime node carries its own per-second signal,
// which preserves the observable contract (a timer that visibly counts) with
// no document-wide scan.
import { For, Show, createSignal, onCleanup, onMount } from "solid-js";
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

// ── Source review: four stages, one public progress band ────────────────────

/** One stage of the screener's source review. The pipeline runs four (L1
 * broad review → L2 cause analysis → L3 safety review → L4 final
 * adjudication) and the heartbeat carries them in a single 0–100 band, so
 * the mapping between bucket and stage lives here rather than being
 * inferred per call site. */
export interface ReviewStage {
  key: string;
  /** Ladder tick label; the card is ~280px wide. */
  short: string;
  /** The sentence form used in the state line and accessible names. */
  label: string;
}

/** L1 owns 0–50 and reports its own percentage inside that half; each
 * escalation stage owns one bucket above it (`LayeredSourceReviewAgent`
 * reports 6/8/9/10 tenths). A screener build that predates per-stage
 * reporting only ever emits 50 and 100, which still lands on a real stage. */
const REVIEW_STAGES: readonly ReviewStage[] = [
  { key: "l1", short: "L1", label: "L1 source review" },
  { key: "l2", short: "L2", label: "L2 cause analysis" },
  { key: "l3", short: "L3", label: "L3 safety review" },
  { key: "l4", short: "L4", label: "L4 final adjudication" },
];

/** The four source-review stages in order (the ladder's own domain). */
export function reviewStages(): ReviewStage[] {
  return REVIEW_STAGES.slice();
}

/** The heartbeat's 0–100 review bucket, or null for a non-review stage. */
export function sourceReviewBucket(stage?: string | null): number | null {
  const match = /^source_review_(\d+)$/.exec(stage || "");
  if (!match) return null;
  return Math.max(0, Math.min(100, Number(match[1]) || 0));
}

/**
 * Which of the four review stages a bucket is in: 0–3 for a stage that is
 * running, 4 once every stage has finished, null when the screener is not
 * in source review at all. Never a guess — an unrecognized stage is absence.
 */
export function reviewStageIndex(stage?: string | null): number | null {
  const bucket = sourceReviewBucket(stage);
  if (bucket == null) return null;
  if (bucket <= 50) return 0;
  if (bucket < 80) return 1;
  if (bucket < 90) return 2;
  if (bucket < 100) return 3;
  return REVIEW_STAGES.length;
}

/** Screener stage → label. Source review names the stage it is actually in;
 * reporting the whole escalation as one band left a card reading "L1 · 100%"
 * for the minutes L2 and L3 were running, which looks like a stall. */
export function screenerStageLabel(stage?: string | null): string {
  const bucket = sourceReviewBucket(stage);
  if (bucket != null) {
    // L1 is long enough to deserve a percentage; the escalation stages are
    // one bucket each, so a percentage there would be a fabricated 100%.
    if (bucket <= 50) return "L1 source review · " + bucket * 2 + "%";
    const index = reviewStageIndex(stage);
    if (index == null || index >= REVIEW_STAGES.length) return "Source review complete";
    return REVIEW_STAGES[index]!.label;
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

// ── Admission step track ────────────────────────────────────────────────────

/** One segment of the admission step track. */
export interface AdmissionStep {
  key: string;
  label: string;
}

/** The admission pipeline in heartbeat order (ScreenerProgressStage):
 * preparing/downloading/validating → building → starting/health_check →
 * source_review_* → submitting, folded into the five segments the track
 * renders. */
const ADMISSION_STEP_DEFS: readonly AdmissionStep[] = [
  { key: "fetch", label: "Fetch & validate" },
  { key: "build", label: "Build image" },
  { key: "boot", label: "Start & health check" },
  { key: "review", label: "Source review" },
  { key: "submit", label: "Submit result" },
];

const STAGE_STEP_KEYS: Record<string, string> = {
  preparing: "fetch",
  downloading: "fetch",
  validating: "fetch",
  building: "build",
  starting: "boot",
  health_check: "boot",
  submitting: "submit",
};

/** Screener stage → step key; an unrecognized stage is an explicit absence
 * (the track shows "in progress, stage not reported"), never a guess. */
export function admissionStepKey(stage?: string | null): string | null {
  if (/^source_review_\d+$/.test(stage || "")) return "review";
  return (stage != null && STAGE_STEP_KEYS[stage]) || null;
}

/** A build-only screening never enters source review, so its track must not
 * carry a review segment that would render "done" on submit. */
export function admissionSteps(buildOnly?: boolean | null): AdmissionStep[] {
  return buildOnly === true
    ? ADMISSION_STEP_DEFS.filter((step) => step.key !== "review")
    : ADMISSION_STEP_DEFS.slice();
}

/**
 * The segmented admission progress track (GitHub-Actions style): done
 * segments filled, the current one pulsing, queued ones hollow. A waiting
 * card renders the all-hollow track — the at-a-glance difference between
 * "not started" and "building". An active card whose screener has not
 * reported a stage shimmers the whole track rather than guessing a segment.
 */
export function AdmissionStepTrack(props: {
  steps: AdmissionStep[];
  /** Live screener stage; null when active work has not reported one. */
  stage: string | null;
  /** True for a queued submission no screener has claimed. */
  waiting: boolean;
}): JSX.Element {
  const currentIndex = (): number => {
    if (props.waiting) return -1;
    const key = admissionStepKey(props.stage);
    return key == null ? -1 : props.steps.findIndex((step) => step.key === key);
  };
  const label = (): string => {
    if (props.waiting) return "Admission not started · " + props.steps.length + " steps queued";
    const index = currentIndex();
    if (index < 0) return "Admission in progress · stage not reported";
    return (
      "Admission step " +
      (index + 1) +
      " of " +
      props.steps.length +
      ": " +
      props.steps[index]!.label
    );
  };
  const segmentClass = (index: number): string => {
    if (props.waiting) return "admission-step";
    const current = currentIndex();
    if (current < 0) return "admission-step unknown";
    if (index < current) return "admission-step done";
    if (index === current) return "admission-step current";
    return "admission-step";
  };
  return (
    <span
      class={"admission-steps" + (props.waiting ? " waiting" : "")}
      role="img"
      aria-label={label()}
      title={label()}
    >
      <For each={props.steps}>{(_, index) => <i class={segmentClass(index())} />}</For>
    </span>
  );
}

/**
 * The four-stage source-review ladder, nested under the admission track.
 * The track above folds all of source review into one segment — true to the
 * admission pipeline, but it leaves the longest and most opaque part of
 * screening as a single pulsing tick. This renders the stages inside it, so
 * "L2 for six minutes" is legible as progress rather than as a stall.
 *
 * It renders only while the screener is in source review; a build-only
 * screening never enters it and never shows one.
 */
export function ReviewStageLadder(props: { stage: string | null }): JSX.Element {
  const stages = reviewStages();
  const current = (): number | null => reviewStageIndex(props.stage);
  const label = (): string => {
    const index = current();
    if (index == null) return "";
    if (index >= stages.length) {
      return "Source review complete · all " + stages.length + " stages done";
    }
    return (
      "Source review stage " + (index + 1) + " of " + stages.length + ": " + stages[index]!.label
    );
  };
  const rungClass = (index: number): string => {
    const active = current();
    if (active == null) return "review-rung";
    if (index < active) return "review-rung done";
    if (index === active) return "review-rung current";
    return "review-rung";
  };
  return (
    <Show when={current() != null}>
      <span class="review-ladder" role="img" aria-label={label()} title={label()}>
        <For each={stages}>
          {(stage, index) => (
            <span class={rungClass(index())}>
              <i />
              <b>{stage.short}</b>
            </span>
          )}
        </For>
      </span>
    </Show>
  );
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
