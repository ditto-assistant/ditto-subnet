import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BenchmarkProgress } from "../../types/pipeline";
import {
  AdmissionStepTrack,
  BenchmarkProgressView,
  ElapsedTime,
  ReviewStageLadder,
  admissionStepKey,
  admissionSteps,
  benchmarkProgressText,
  benchmarkStageLabel,
  reviewStageIndex,
  reviewStages,
  screenerStageLabel,
} from "./progress";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("benchmarkStageLabel / benchmarkProgressText", () => {
  it("names every stage, with an explicit fallback", () => {
    expect(benchmarkStageLabel("finalizing")).toBe("Scoring and finalizing");
    expect(benchmarkStageLabel("submitting_result")).toBe("Signing and submitting result");
    expect(benchmarkStageLabel("building_harness")).toBe("Loading harness image");
    expect(benchmarkStageLabel("bogus")).toBe("Benchmark progress unavailable");
  });

  it("states absence when nothing is reported", () => {
    expect(benchmarkProgressText(null)).toBe("Benchmark progress not reported");
    expect(benchmarkProgressText({})).toBe("Benchmark progress not reported");
  });

  it("says what a stalled run has actually done, not just how long", () => {
    // "3 of 281 checks after 45m" is diagnosable; "no progress for 45m"
    // reads as a reporting gap (monolith comment, kept).
    expect(
      benchmarkProgressText({
        stage: "running_benchmark",
        stalled: true,
        completed_checks: 3,
        total_checks: 281,
        started_at: "2026-07-31T13:15:00Z",
      }),
    ).toBe("Running benchmark · stalled · 3 of 281 checks after 45m 0s");
  });

  it("reports the exact per-check percentage and the retry state", () => {
    expect(
      benchmarkProgressText({
        stage: "running_benchmark",
        percent: 47,
        completed_checks: 132,
        total_checks: 281,
      }),
    ).toBe("Benchmark 47% · 132 of 281 checks");
    expect(benchmarkProgressText({ stage: "failed_retrying" })).toBe(
      "Failed · a new attempt will start shortly",
    );
    expect(benchmarkProgressText({ stage: "preparing" })).toBe("Preparing artifact · working…");
  });
});

describe("BenchmarkProgressView", () => {
  function view(progress: BenchmarkProgress, showAgent = false): HTMLElement {
    const { container } = render(() => (
      <BenchmarkProgressView progress={progress} showAgent={showAgent} />
    ));
    return container;
  }

  it("renders a native <progress> with an accessible label when determinate", () => {
    const container = view({
      stage: "running_benchmark",
      percent: 47,
      completed_checks: 132,
      total_checks: 281,
      bench_version: 7,
      started_at: "2026-07-31T13:00:00Z",
    });
    const bar = container.querySelector("progress") as HTMLProgressElement;
    expect(bar).toHaveAttribute("max", "100");
    expect(bar).toHaveAttribute("value", "47");
    expect(bar.getAttribute("aria-label")).toBe(
      "Bench v7. Running benchmark. Benchmark 47% · 132 of 281 checks",
    );
    expect(container.querySelector(".benchmark-version-chip")?.textContent).toBe("Bench v7");
    expect(container.querySelector(".benchmark-progress-text")?.textContent).toBe(
      "Benchmark 47% · 132 of 281 checks",
    );
  });

  it("clamps only to the bar's own domain, never re-flattening to 95", () => {
    const bar = view({ stage: "running_benchmark", percent: 99 }).querySelector("progress");
    expect(bar).toHaveAttribute("value", "99");
  });

  it("animates an indeterminate sliver for quick pre-run stages", () => {
    const container = view({ stage: "generating_dataset" });
    const wrap = container.querySelector(".benchmark-progress") as HTMLElement;
    expect(wrap.classList.contains("indeterminate")).toBe(true);
    expect(container.querySelector("progress")).toBeNull();
    const bar = container.querySelector(".bench-bar") as HTMLElement;
    expect(bar).toHaveAttribute("role", "img");
    expect(bar.getAttribute("aria-label")).toContain("Generating dataset");
  });

  it("pins a stalled or failed run to a static warning bar", () => {
    const stalled = view({
      stage: "running_benchmark",
      percent: 12,
      stalled: true,
      started_at: "2026-07-31T13:15:00Z",
    }).querySelector(".benchmark-progress") as HTMLElement;
    expect(stalled.classList.contains("stalled")).toBe(true);
    expect(stalled.classList.contains("indeterminate")).toBe(false);
    expect(stalled.querySelector("progress")).toBeNull();

    const failed = view({ stage: "failed_retrying" }).querySelector(
      ".benchmark-progress",
    ) as HTMLElement;
    expect(failed.classList.contains("failed")).toBe(true);
    expect(failed.classList.contains("indeterminate")).toBe(false);
  });

  it("links the agent under evaluation when asked to", () => {
    const container = view(
      { stage: "running_benchmark", percent: 5, agent_id: "agent-1", agent_name: "Runner" },
      true,
    );
    const agent = container.querySelector(".benchmark-agent") as HTMLElement;
    expect(agent).toHaveAttribute("title", "agent-1");
    expect(agent.querySelector('[data-entity-link="agent"]')?.textContent).toBe("Runner · agent-1");
  });
});

describe("screenerStageLabel", () => {
  it("gives the scan the 0-50 band with its own percentage", () => {
    expect(screenerStageLabel("source_review_0")).toBe("Broad source scan · 0%");
    expect(screenerStageLabel("source_review_20")).toBe("Broad source scan · 40%");
    expect(screenerStageLabel("source_review_50")).toBe("Broad source scan · 100%");
  });

  it("names each stage for what it does, never for its layer number", () => {
    // A card sat at "L1 · 100%" for the whole escalation before this, and
    // "L2" told a reader nothing about why it had been there six minutes.
    expect(screenerStageLabel("source_review_60")).toBe("Causal analysis");
    expect(screenerStageLabel("source_review_70")).toBe("Causal analysis");
    expect(screenerStageLabel("source_review_80")).toBe("Independent safety review");
    expect(screenerStageLabel("source_review_90")).toBe("Final adjudication");
    expect(screenerStageLabel("source_review_100")).toBe("Source review complete");
  });

  it("names the container stages with a generic fallback", () => {
    expect(screenerStageLabel("building")).toBe("Building image");
    expect(screenerStageLabel("health_check")).toBe("Checking health");
    expect(screenerStageLabel("anything-else")).toBe("Screening");
  });
});

describe("source review ladder", () => {
  it("places every review bucket on one of the four stages", () => {
    expect(reviewStageIndex("source_review_0")).toBe(0);
    expect(reviewStageIndex("source_review_50")).toBe(0);
    expect(reviewStageIndex("source_review_60")).toBe(1);
    expect(reviewStageIndex("source_review_70")).toBe(1);
    expect(reviewStageIndex("source_review_80")).toBe(2);
    expect(reviewStageIndex("source_review_90")).toBe(3);
    // Past the last stage: every rung done, none current.
    expect(reviewStageIndex("source_review_100")).toBe(4);
    // Not in source review at all is an absence, never stage 1.
    expect(reviewStageIndex("building")).toBeNull();
    expect(reviewStageIndex(null)).toBeNull();
  });

  it("marks done, current and pending rungs for a live review stage", () => {
    const { container } = render(() => <ReviewStageLadder stage="source_review_80" />);
    const ladder = container.querySelector(".review-ladder") as HTMLElement;
    // The layer number survives in the tooltip so an operator can still match
    // a card against Backroom review material, which speaks in L1/L2/L3/L4.
    expect(ladder).toHaveAttribute(
      "aria-label",
      "Source review stage 3 of 4: Independent safety review (L3)",
    );
    expect(Array.from(ladder.querySelectorAll(".review-rung"), (el) => el.className)).toEqual([
      "review-rung done",
      "review-rung done",
      "review-rung current",
      "review-rung",
    ]);
    expect(Array.from(ladder.querySelectorAll(".review-rung b"), (el) => el.textContent)).toEqual([
      "Scan",
      "Trace",
      "Safety",
      "Verdict",
    ]);
  });

  it("keeps every rung label short enough for a ~62px column", () => {
    for (const stage of reviewStages()) {
      expect(stage.short.length).toBeLessThanOrEqual(7);
    }
  });

  it("fills every rung once review completes, with none current", () => {
    const { container } = render(() => <ReviewStageLadder stage="source_review_100" />);
    const ladder = container.querySelector(".review-ladder") as HTMLElement;
    expect(ladder).toHaveAttribute("aria-label", "Source review complete · all 4 stages done");
    expect(ladder.querySelectorAll(".review-rung.done")).toHaveLength(4);
    expect(ladder.querySelector(".review-rung.current")).toBeNull();
  });

  it("renders nothing outside source review", () => {
    const { container } = render(() => <ReviewStageLadder stage="building" />);
    expect(container.querySelector(".review-ladder")).toBeNull();
  });
});

describe("admission step track", () => {
  it("folds every heartbeat stage into its segment, unknown as absence", () => {
    expect(admissionStepKey("preparing")).toBe("fetch");
    expect(admissionStepKey("downloading")).toBe("fetch");
    expect(admissionStepKey("validating")).toBe("fetch");
    expect(admissionStepKey("building")).toBe("build");
    expect(admissionStepKey("starting")).toBe("boot");
    expect(admissionStepKey("health_check")).toBe("boot");
    expect(admissionStepKey("source_review_0")).toBe("review");
    expect(admissionStepKey("source_review_100")).toBe("review");
    expect(admissionStepKey("submitting")).toBe("submit");
    expect(admissionStepKey("anything-else")).toBeNull();
    expect(admissionStepKey(null)).toBeNull();
  });

  it("drops the review segment only for a build-only screening", () => {
    expect(admissionSteps(true).map((step) => step.key)).toEqual([
      "fetch",
      "build",
      "boot",
      "submit",
    ]);
    expect(admissionSteps(false).map((step) => step.key)).toEqual([
      "fetch",
      "build",
      "boot",
      "review",
      "submit",
    ]);
    // Unknown build-only state keeps the full track rather than guessing.
    expect(admissionSteps(null)).toHaveLength(5);
    expect(admissionSteps(undefined)).toHaveLength(5);
  });

  it("marks done, current, and queued segments for a live stage", () => {
    const { container } = render(() => (
      <AdmissionStepTrack steps={admissionSteps(null)} stage="source_review_20" waiting={false} />
    ));
    const track = container.querySelector(".admission-steps") as HTMLElement;
    expect(track).toHaveAttribute("aria-label", "Admission step 4 of 5: Source review");
    const segments = Array.from(track.querySelectorAll(".admission-step"), (el) => el.className);
    expect(segments).toEqual([
      "admission-step done",
      "admission-step done",
      "admission-step done",
      "admission-step current",
      "admission-step",
    ]);
  });

  it("renders an all-hollow track for a queued submission", () => {
    const { container } = render(() => (
      <AdmissionStepTrack steps={admissionSteps(null)} stage={null} waiting={true} />
    ));
    const track = container.querySelector(".admission-steps.waiting") as HTMLElement;
    expect(track).toHaveAttribute("aria-label", "Admission not started · 5 steps queued");
    expect(track.querySelectorAll(".admission-step")).toHaveLength(5);
    expect(track.querySelector(".done, .current, .unknown")).toBeNull();
  });

  it("shimmers the whole track when active work reports no stage", () => {
    const { container } = render(() => (
      <AdmissionStepTrack steps={admissionSteps(null)} stage={null} waiting={false} />
    ));
    const track = container.querySelector(".admission-steps") as HTMLElement;
    expect(track).toHaveAttribute("aria-label", "Admission in progress · stage not reported");
    expect(track.querySelectorAll(".admission-step.unknown")).toHaveLength(5);
  });
});

describe("ElapsedTime", () => {
  it("re-renders the elapsed duration every second", () => {
    const { container } = render(() => (
      <ElapsedTime class="benchmark-progress-time" startedAt="2026-07-31T13:00:00Z" />
    ));
    const node = container.querySelector(".benchmark-progress-time") as HTMLElement;
    expect(node).toHaveAttribute("data-started-at", "2026-07-31T13:00:00Z");
    expect(node.textContent).toBe("1h 0m 0s");
    vi.advanceTimersByTime(1000);
    expect(node.textContent).toBe("1h 0m 1s");
    vi.advanceTimersByTime(59_000);
    expect(node.textContent).toBe("1h 1m 0s");
  });
});

// ── Weekend drift: delayed telemetry, assignment handoff, relay wait ────────
// (monolith benchmarkProgressText 7028–7059; Python guard
// test_transient_validator_telemetry_uses_a_bounded_grace's rendering half)
describe("delayed telemetry and handoff grace (weekend drift)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("labels preserved telemetry as delayed, carrying the last report", () => {
    expect(
      benchmarkProgressText({
        stage: "running_benchmark",
        percent: 47,
        completed_checks: 132,
        total_checks: 281,
        _telemetry_delayed: true,
      }),
    ).toBe("Progress update delayed · last reported 47% · 132 of 281 checks");
    expect(benchmarkProgressText({ stage: "preparing", _telemetry_delayed: true })).toBe(
      "Progress update delayed · Preparing artifact",
    );
  });

  it("treats a young no-stage lease as the assignment handoff, not missing telemetry", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
    // Ticket issuance and the first signed heartbeat are separate writes: a
    // lease under a minute old is an expected transition.
    expect(benchmarkProgressText({ started_at: "2026-07-31T13:59:30Z" })).toBe(
      "Starting benchmark · awaiting first progress",
    );
    // Past the one-minute window the stronger warning returns.
    expect(benchmarkProgressText({ started_at: "2026-07-31T13:58:30Z" })).toBe(
      "Benchmark progress not reported",
    );
    expect(benchmarkProgressText(null)).toBe("Benchmark progress not reported");
  });

  it("gives waiting_for_relay its own sentence with carried progress", () => {
    expect(
      benchmarkProgressText({
        stage: "waiting_for_relay",
        percent: 100,
        completed_checks: 281,
        total_checks: 281,
      }),
    ).toBe("Waiting for relay · benchmark 100% · 281 of 281 checks");
    expect(benchmarkProgressText({ stage: "waiting_for_relay" })).toBe("Waiting for relay");
  });
});
