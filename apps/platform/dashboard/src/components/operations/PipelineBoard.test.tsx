// Pipeline board behaviors from the monolith's operations section (markup
// 2742–2862, renderPipelineBoard 7976–8106). Two inline-comment contracts
// from the old suite are kept verbatim:
// - "Rank 1 alone must never earn the badge: a gated row can hold the head of
//   the list while no validator is able to lease it" (#458).
// - Continual-retest cards project into Evaluating by active slot, not
//   lifecycle, so the headline cannot claim zero while the board renders live
//   work.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { syncFromLocation } from "../../stores/routeStore";
import type { FleetReport } from "../../types/fleet";
import { PipelineBoard, RescreenNotice } from "./PipelineBoard";
import {
  QUEUE_GATES,
  pipelineBoardStage,
  pipelineRescoreState,
  queueGateLabel,
  queueRelevantBenchmark,
} from "./pipeline";
import { policyScreeningLabel } from "../pipeline/status";
import type { PipelineEntryExt } from "./pipeline";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/operations");
  syncFromLocation();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function board(
  entries: PipelineEntryExt[],
  options: {
    statusCounts?: Record<string, number>;
    unavailable?: boolean;
    loading?: boolean;
    screeners?: FleetReport | null;
    activeVersion?: number | null;
  } = {},
): HTMLElement {
  const { container } = render(() => (
    <PipelineBoard
      entries={entries}
      statusCounts={options.statusCounts ?? {}}
      unavailable={options.unavailable ?? false}
      loading={options.loading ?? false}
      screeners={options.screeners ?? null}
      activeVersion={options.activeVersion ?? 7}
    />
  ));
  return container;
}

const waiting = (over: Partial<PipelineEntryExt>): PipelineEntryExt => ({
  agent_id: "a-" + String(over.validator_queue_rank ?? "x"),
  name: "Agent",
  version: 1,
  status: "waiting_validator",
  submitted_at: "2026-07-31T10:00:00Z",
  ...over,
});

describe("the Up next badge (#458)", () => {
  it("badges rank 1 only when nothing gates the lease", () => {
    const container = board([waiting({ validator_queue_rank: 1, agent_id: "head" })], {
      statusCounts: { waiting_validator: 1 },
    });
    const card = container.querySelector("#pipeline-wait-validator .pipeline-item");
    expect(card?.querySelector(".pipeline-next-badge")?.textContent).toBe("Up next");
    expect(card?.getAttribute("aria-label")).toContain("up next for validator assignment");
  });

  it("never badges a gated row, whatever its rank", () => {
    // A previous-generation backlog holds the head of a line that is not
    // moving; "Up next" — which a miner reads as "you are about to be
    // scored" — must never appear on it.
    const container = board(
      [waiting({ validator_queue_rank: 1, validator_queue_gate: "previous_generation" })],
      { statusCounts: { waiting_validator: 1 } },
    );
    const card = container.querySelector("#pipeline-wait-validator .pipeline-item");
    expect(card?.querySelector(".pipeline-next-badge")).toBeNull();
    const gate = card?.querySelector(".pipeline-gate-badge");
    expect(gate?.textContent).toBe("Prev gen");
    expect(gate).toHaveAttribute("title", QUEUE_GATES.previous_generation?.title ?? "");
    expect(card?.getAttribute("aria-label")).toContain(
      "previous benchmark generation, waiting for the current era to finish",
    );
  });

  it("orders the waiting lane by validator queue rank", () => {
    const container = board(
      [
        waiting({ validator_queue_rank: 3, agent_id: "third" }),
        waiting({ validator_queue_rank: 1, agent_id: "first" }),
        waiting({ validator_queue_rank: 2, agent_id: "second" }),
      ],
      { statusCounts: { waiting_validator: 3 } },
    );
    // The compact card (#633) no longer prints the id slice; the anchor
    // target is the stable identity.
    const ids = Array.from(
      container.querySelectorAll("#pipeline-wait-validator .pipeline-item"),
      (el) => new URLSearchParams(el.getAttribute("href")?.split("?")[1]).get("agent"),
    );
    expect(ids).toEqual(["first", "second", "third"]);
  });

  it("knows the three queue gates and only those", () => {
    expect(Object.keys(QUEUE_GATES).sort()).toEqual([
      "not_leasable",
      "owner_serialized",
      "previous_generation",
    ]);
    expect(queueGateLabel(waiting({ validator_queue_gate: "owner_serialized" }))?.label).toBe(
      "Owner queued",
    );
    expect(queueGateLabel(waiting({}))).toBeNull();
  });
});

describe("retry-state chips", () => {
  it("chips only the states that need explaining", () => {
    const container = board(
      [
        waiting({
          validator_queue_rank: 1,
          retry_state: "cooling_down",
          retry_after: "2026-07-31T13:30:00Z",
        }),
      ],
      { statusCounts: { waiting_validator: 1 } },
    );
    const chip = container.querySelector(".retry-chip.cooling");
    expect(chip?.textContent).toBe("Cooling down · 30m ago");
    expect(container.querySelector(".retry-chip.exhausted")).toBeNull();
  });
});

describe("continual retests and rescores in Evaluating", () => {
  const retest = waiting({
    agent_id: "retest",
    status: "scored",
    validator_queue_rank: null,
    active_benchmarks: [
      { slot_id: "slot-0", stage: "running_benchmark", percent: 10, bench_version: 7 },
    ],
  });

  it("projects a scored agent with live work into Evaluating by active slot", () => {
    const container = board([retest], { statusCounts: { scored: 1 } });
    expect(container.querySelectorAll("#pipeline-evaluating .pipeline-item").length).toBe(1);
    // The headline counts the same slot rows the board renders.
    expect(container.querySelector("#pipeline-evaluating-count")?.textContent).toBe("1");
    expect(
      container.querySelector("#pipeline-evaluating .pipeline-item .pipeline-item-meta")
        ?.textContent,
    ).toContain("Bench v7 rescore");
  });

  it("projects secondary-slot jobs into Scoring without duplicating submission cards", () => {
    const secondarySlots = waiting({
      agent_id: "secondary-slots",
      status: "waiting_validator",
      active_benchmarks: [
        {
          slot_id: "slot-1",
          stage: "running_benchmark",
          percent: 35,
          bench_version: 8,
        },
        {
          slot_id: "slot-4",
          stage: "running_benchmark",
          percent: 62,
          bench_version: 8,
        },
      ],
    });
    const oneSlot = waiting({
      agent_id: "one-slot",
      status: "waiting_validator",
      active_benchmarks: [
        {
          slot_id: "slot-2",
          stage: "running_benchmark",
          percent: 18,
          bench_version: 8,
        },
      ],
    });

    const container = board([secondarySlots, oneSlot], {
      activeVersion: 8,
      statusCounts: { waiting_validator: 2 },
    });

    expect(container.querySelectorAll("#pipeline-wait-validator .pipeline-item").length).toBe(0);
    expect(container.querySelectorAll("#pipeline-evaluating .pipeline-item").length).toBe(2);
    expect(container.querySelectorAll("#pipeline-evaluating .benchmark-progress").length).toBe(3);
    expect(container.querySelector("#pipeline-evaluating-count")?.textContent).toBe("3");
  });

  it("labels an inherited-cohort qualification and keeps the old score live", () => {
    const qualifying: PipelineEntryExt = {
      ...retest,
      active_benchmarks: [
        { slot_id: "slot-0", stage: "running_benchmark", percent: 10, bench_version: 8 },
      ],
    };
    const container = board([qualifying], { statusCounts: { scored: 1 } });
    const card = container.querySelector("#pipeline-evaluating .pipeline-item");
    expect(card?.querySelector(".pipeline-qualification-badge")?.textContent).toBe("Cohort → v8");
    expect(card?.querySelector(".pipeline-item-qualification-detail")?.textContent).toBe(
      "v7 score stays live until v8 quorum",
    );
    expect(card?.getAttribute("aria-label")).toContain(
      "inherited benchmark cohort qualification in progress",
    );
  });

  it("ignores stale previous-generation slots when projecting", () => {
    expect(queueRelevantBenchmark({ bench_version: 6 }, 7)).toBe(false);
    expect(queueRelevantBenchmark({ bench_version: 7 }, 7)).toBe(true);
    expect(queueRelevantBenchmark({ bench_version: 8 }, 7)).toBe(true);
    // No known active version: any positive version counts.
    expect(queueRelevantBenchmark({ bench_version: 3 }, null)).toBe(true);
    const stale = { ...retest, active_benchmarks: [{ bench_version: 6, slot_id: "slot-0" }] };
    expect(pipelineBoardStage(stale, 7)).toBe("scored");
    expect(pipelineRescoreState(stale, 7)).toBeNull();
  });
});

describe("screening cross-feed and policy labels", () => {
  it("merges waiting and active admission work while naming each row's state", () => {
    const container = board(
      [
        waiting({ agent_id: "queued", status: "waiting_screening" }),
        waiting({ agent_id: "building", status: "screening" }),
      ],
      { statusCounts: { waiting_screening: 2, screening: 3 } },
    );
    expect(container.querySelector("#pipeline-admission-count")?.textContent).toBe("5");
    const states = Array.from(
      container.querySelectorAll("#pipeline-admission .pipeline-admission-state"),
      (node) => node.textContent,
    );
    expect(states).toEqual(["Waiting for admission", "Building image & admission"]);
    expect(container.querySelectorAll("#pipeline-admission .pipeline-item")).toHaveLength(2);
  });

  it("overlays the live screener stage onto the screening card", () => {
    const screeners: FleetReport = {
      screeners: [
        {
          screener_hotkey: "5S",
          instance_id: "screener-1",
          active_agent_id: "in-screening",
          screening_progress: { stage: "source_review_20", started_at: "2026-07-31T13:58:00Z" },
        },
      ],
    };
    const container = board(
      [waiting({ agent_id: "in-screening", status: "screening", validator_queue_rank: null })],
      { statusCounts: { screening: 1 }, screeners },
    );
    const card = container.querySelector("#pipeline-admission .pipeline-item");
    expect(card?.querySelector(".screener-progress-stage")?.textContent).toContain(
      "L1 source review · 40%",
    );
    expect(card?.querySelector(".pipeline-admission-state")?.textContent).toBe(
      "L1 source review · 40%",
    );
    expect(card?.getAttribute("aria-label")).toContain("L1 source review · 40%");
  });

  it("derives the policy rescreen labels from public activity state", () => {
    expect(
      policyScreeningLabel({ screening_policy_version: 3, required_screening_policy_version: 5 }),
    ).toBe("Rescreen · policy v3 → v5");
    expect(
      policyScreeningLabel({ screening_policy_version: 0, required_screening_policy_version: 5 }),
    ).toBe("Policy v5 screening");
    expect(
      policyScreeningLabel({ screening_policy_version: 5, required_screening_policy_version: 5 }),
    ).toBe("");
  });
});

describe("rescreen notice", () => {
  const rescreen = (over: Partial<PipelineEntryExt>): PipelineEntryExt =>
    waiting({
      status: "waiting_screening",
      screening_policy_version: 3,
      required_screening_policy_version: 5,
      ...over,
    });

  it("explains an in-flight policy rescreen from queue state alone", () => {
    const { container } = render(() => (
      <RescreenNotice
        entries={[
          rescreen({ agent_id: "r1", score_count: 2 }),
          rescreen({ agent_id: "r2", score_count: 0 }),
          // First-time screening under the new policy is not a rescreen.
          rescreen({ agent_id: "fresh", screening_policy_version: 0 }),
        ]}
        unavailable={false}
      />
    ));
    const notice = container.querySelector("#rescreen-notice") as HTMLElement;
    expect(notice.hidden).toBe(false);
    expect(notice.querySelector("#rescreen-title")?.textContent).toBe(
      "Policy v5 rescreen in progress",
    );
    expect(notice.querySelector("#rescreen-policy")?.textContent).toBe("LIVE API STATE");
    const copy = notice.querySelector("#rescreen-copy")?.textContent ?? "";
    expect(copy).toContain("Prior scores remain preserved");
    expect(copy).toContain("validators may intentionally idle");
    expect(copy).toContain("This is not data loss");
    expect(notice.querySelector("#rescreen-count")?.textContent).toBe("2");
    expect(notice.querySelector("#rescreen-scored")?.textContent).toBe("1");
  });

  it("stays hidden with no queued rescreens or while unavailable", () => {
    const { container } = render(() => (
      <RescreenNotice entries={[rescreen({ agent_id: "r1" })]} unavailable={true} />
    ));
    expect((container.querySelector("#rescreen-notice") as HTMLElement).hidden).toBe(true);
  });
});

describe("stated absence", () => {
  it("marks every queue unavailable on a failed snapshot", () => {
    const container = board([], { unavailable: true });
    const empties = Array.from(
      container.querySelectorAll(".pipeline-empty"),
      (el) => el.textContent,
    );
    expect(empties).toEqual(Array.from({ length: 4 }, () => "Queue unavailable."));
    expect(container.querySelector("#pipeline-scored-count")?.textContent).toBe("–");
  });

  it("shows the loading placeholders before the first snapshot", () => {
    const container = board([], { loading: true });
    expect(container.querySelector("#pipeline-admission .pipeline-empty")?.textContent).toBe(
      "Loading…",
    );
    expect(container.querySelector("#pipeline-admission-count")?.textContent).toBe("–");
  });
});
