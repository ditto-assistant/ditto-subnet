// The block escaped BOTH gates of the SPA port: #d-consensus is only filled
// while the modal is open, so the per-page DOM goldens never contained it, and
// the old Python suites did not assert it. What it is FOR is the plural — three
// independent validators each scored this agent and the platform finalizes on
// the median — so these tests pin the per-validator rows, the equation that
// produced each composite, the per-version grouping that keeps incomparable
// numbers apart, and every absence path.
//
import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import type { components as PlatformComponents } from "../../../../../backroom/src/generated/platform-api";
import { rankEntries } from "../../lib/scoring";
import { syncFromLocation } from "../../stores/routeStore";
import { FIXTURE_TOP_AGENT_ID, installFixtureFetch, loadFixture } from "../../test-fixtures";
import type { OperationsPayload } from "../../types/fleet";
import type { LeaderboardPayload, ScoresPayload, V9BaseEvidence } from "../../types/leaderboard";
import { EntityPanel } from "../EntityPanel";
import { Consensus } from "./Consensus";

const glossary = loadFixture("bench-glossary");
const recorded = loadFixture<ScoresPayload>("agent-top-scores");
const leaderboard = loadFixture<LeaderboardPayload>("leaderboard");
const operations = loadFixture<OperationsPayload>("operations");

type GeneratedPublicV9BaseEvidence = NonNullable<
  PlatformComponents["schemas"]["PublicValidatorScore"]["v9_base"]
>;

const AGENT = "44444444-dddd-4ddd-8ddd-dddddddddddd";
const V1 = "5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118";
const V2 = "5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C";
const V3 = "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz";

const NOTE =
  "Independent validators that scored this agent; the platform finalizes on the median, so no " +
  "single validator decides the score.";
const MULTI_NOTE =
  "This agent was scored under more than one benchmark version; scores compare only within a " +
  "version, never across.";

let restoreFetch: (() => void) | null = null;

/** Serve one synthetic /scores payload (or a status for the failure paths);
 * everything else comes from the recorded fixture set. */
function serve(payload: unknown, status = 200): void {
  const original = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (new URL(raw, "http://fixtures.test").pathname.endsWith("/scores")) {
      return Promise.resolve(
        new Response(JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        }),
      );
    }
    return Promise.resolve(
      new Response(JSON.stringify(glossary), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  }) as typeof fetch;
  restoreFetch = () => {
    globalThis.fetch = original;
  };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/leaderboard");
  syncFromLocation();
});

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
  vi.useRealTimers();
  document.body.classList.remove("entity-page");
});

function mount(agentId = AGENT): void {
  render(() => <Consensus agentId={agentId} />);
}

async function block(): Promise<HTMLElement> {
  return await waitFor(() => {
    const el = document.querySelector<HTMLElement>(".stat-group");
    if (!el) throw new Error("consensus block not rendered");
    return el;
  });
}

function cohorts(scope: ParentNode): HTMLElement[] {
  return Array.from(scope.querySelectorAll<HTMLElement>(".benchmark-cohort"));
}

function cohortNames(scope: ParentNode): string[] {
  return cohorts(scope).map((el) => el.querySelector("h5")?.textContent ?? "");
}

function summaries(scope: ParentNode): string[] {
  return cohorts(scope).map(
    (el) => el.querySelector(".benchmark-cohort-summary")?.textContent ?? "",
  );
}

/** One cohort's validator rows as [elided hotkey, composite] pairs. */
function scoreRows(scope: ParentNode): Array<[string, string]> {
  return Array.from(scope.querySelectorAll<HTMLElement>(".consensus-score")).map((el) => [
    el.querySelector(".k > span")?.textContent ?? "",
    el.querySelector(".v")?.textContent ?? "",
  ]);
}

function equations(scope: ParentNode): string[] {
  return Array.from(scope.querySelectorAll<HTMLElement>(".score-calc-inline")).map(
    (el) => el.textContent ?? "",
  );
}

function statRow(scope: ParentNode, key: string): HTMLElement | undefined {
  return Array.from(scope.querySelectorAll<HTMLElement>(":scope > .stat-row")).find(
    (el) => el.querySelector(".k")?.textContent === key,
  );
}

describe("Consensus · the recorded three-validator cohort", () => {
  it("heads the block with the quorum and the copy that explains it", async () => {
    serve(recorded);
    mount();
    const scope = await block();
    expect(scope.querySelector(".stat-head")?.textContent).toBe("Consensus (k=3)");
    // No single validator decides the score: that is the whole point of the
    // block, so it is stated, not implied by the row count.
    expect(scope.querySelector(".muted")?.textContent).toBe(NOTE);
  });

  it("groups under the bench version and names the canonical median", async () => {
    serve(recorded);
    mount();
    const scope = await block();
    expect(cohortNames(scope)).toEqual(["Bench v7"]);
    expect(summaries(scope)).toEqual(["3 of 3 scores · median 0.987"]);
    // One version means the endpoint's median IS that version's median.
    expect(statRow(scope, "Median (canonical)")?.querySelector(".v")?.textContent).toBe("0.987");
  });

  it("renders one row per independent validator, strongest composite first", async () => {
    serve(recorded);
    mount();
    const scope = await block();
    expect(scoreRows(scope)).toEqual([
      ["5Cg3DiRf…WSL118", "0.992"],
      ["5CqJAjSj…bSaD1C", "0.987"],
      ["5HmP9732…7FsmZz", "0.975"],
    ]);
    // The elided hotkey keeps the full value copyable and in its title.
    const first = scope.querySelector(".consensus-score .k");
    expect(first).toHaveAttribute("title", V1);
    expect(first?.querySelector("button.copy")).toHaveAttribute("data-key", V1);
    expect(first?.querySelector("button.copy")).toHaveAttribute(
      "data-copy-label",
      "validator hotkey",
    );
  });

  it("shows each composite's own equation with the factors named in its title", async () => {
    serve(recorded);
    mount();
    const scope = await block();
    // The recorded run carries no token multiplier, which reads "n/a" rather
    // than a fabricated 1.000.
    expect(equations(scope)).toEqual([
      "0.992 × 1.000 × n/a = 0.992",
      "0.987 × 1.000 × n/a = 0.987",
      "0.990 × 0.985 × n/a = 0.975",
    ]);
    expect(scope.querySelector(".score-calc-inline")).toHaveAttribute(
      "title",
      "base accuracy × benchmark quality gates × token efficiency = final composite",
    );
  });

  it("hangs that validator's own per-question results off its row, collapsed", async () => {
    serve(recorded);
    mount();
    const scope = await block();
    const cases = Array.from(
      scope.querySelectorAll<HTMLDetailsElement>(".consensus-score > details.cases"),
    );
    expect(cases).toHaveLength(3);
    expect(cases[0]?.open).toBe(false);
    expect(cases[0]?.querySelector("summary")?.textContent).toBe(
      "Per-question results · 281 cases",
    );
    // Closed details must not mount the grouped rows — a live v11 card has
    // ~350 cases × 3 scores and that tree froze the agent drawer.
    expect(cases[0]?.querySelectorAll("details.cgroup")).toHaveLength(0);
    expect(cases[0]?.querySelectorAll(".crow")).toHaveLength(0);
    const firstCases = cases[0] as HTMLDetailsElement;
    firstCases.open = true;
    firstCases.dispatchEvent(new Event("toggle"));
    expect(firstCases.querySelectorAll("details.cgroup").length).toBeGreaterThan(1);
    expect(firstCases.querySelector<HTMLDetailsElement>("details.cgroup")?.open).toBe(false);
    expect(firstCases.querySelectorAll(".crow")).toHaveLength(0);
    const firstGroup = firstCases.querySelector("details.cgroup") as HTMLDetailsElement;
    firstGroup.open = true;
    firstGroup.dispatchEvent(new Event("toggle"));
    expect(firstGroup.querySelectorAll(".crow").length).toBeGreaterThan(0);
  });
});

describe("Consensus · cohort ordering and the multi-version rule", () => {
  const multi: ScoresPayload = {
    quorum: 3,
    median_composite: 0.612,
    scores: [
      { validator_hotkey: V1, composite: 0.547, bench_version: 6 },
      { validator_hotkey: V2, composite: 0.612, bench_version: 7 },
      { validator_hotkey: V3, composite: 0.701, bench_version: 7 },
      { validator_hotkey: "5FU3YKmvLegacyRowWithNoBenchVersionRecorded00000", composite: 0.4 },
    ],
  };

  it("orders cohorts newest first with an unknown version last", async () => {
    serve(multi);
    mount();
    const scope = await block();
    expect(cohortNames(scope)).toEqual(["Bench v7", "Bench v6", "Bench version unknown"]);
    expect(summaries(scope)).toEqual([
      "2 of 3 scores · preliminary median 0.656",
      "1 of 3 scores · preliminary median 0.547",
      "1 of 3 scores · preliminary median 0.400",
    ]);
  });

  it("sorts each cohort's rows by composite, independently of the others", async () => {
    serve(multi);
    mount();
    const scope = await block();
    expect(scoreRows(cohorts(scope)[0] as HTMLElement)).toEqual([
      ["5HmP9732…7FsmZz", "0.701"],
      ["5CqJAjSj…bSaD1C", "0.612"],
    ]);
  });

  it("withholds the canonical median and says why once versions are mixed", async () => {
    serve(multi);
    mount();
    const scope = await block();
    // The endpoint's median_composite would mix incomparable composites here,
    // so the per-version medians above are the only meaningful aggregates.
    expect(statRow(scope, "Median (canonical)")).toBeUndefined();
    expect(scope.querySelector(".muted")?.textContent).toBe(NOTE + " " + MULTI_NOTE);
  });

  it("labels a below-quorum median preliminary and still shows the canonical row", async () => {
    serve({
      quorum: 3,
      median_composite: 0.6155,
      scores: [
        { validator_hotkey: V1, composite: 0.62, bench_version: 6 },
        { validator_hotkey: V2, composite: 0.611, bench_version: 6 },
      ],
    });
    mount();
    const scope = await block();
    expect(summaries(scope)).toEqual(["2 of 3 scores · preliminary median 0.615"]);
    // A single cohort, so the endpoint's own median is still that cohort's.
    expect(statRow(scope, "Median (canonical)")?.querySelector(".v")?.textContent).toBe("0.616");
  });

  it("defaults a missing or nonsense quorum to three", async () => {
    serve({ quorum: null, scores: [{ validator_hotkey: V1, composite: 0.5, bench_version: 7 }] });
    mount();
    const scope = await block();
    expect(scope.querySelector(".stat-head")?.textContent).toBe("Consensus (k=3)");
    expect(summaries(scope)).toEqual(["1 of 3 scores · preliminary median 0.500"]);
  });
});

describe("Consensus · per-row absences", () => {
  it("accepts the generated Platform public score contract without a hand-shaped adapter", () => {
    expectTypeOf<V9BaseEvidence>().toEqualTypeOf<GeneratedPublicV9BaseEvidence>();
  });

  it("renders the Platform serializer's v9 evidence without private content", async () => {
    const v9Base = loadFixture<V9BaseEvidence>("v9-base-public");
    serve({
      quorum: 3,
      scores: [
        {
          validator_hotkey: V1,
          composite: 0.5,
          bench_version: 9,
          v9_base: v9Base,
        },
      ],
    });
    const privateMarker = "private prompt must never render";
    mount();
    const scope = await block();
    const evidence = scope.querySelector<HTMLElement>(".v9-gate-evidence");
    expect(evidence).toBeTruthy();
    expect(evidence?.textContent).toContain("Trusted Bench 9 score gates");
    expect(evidence?.textContent).toContain("Enforced");
    expect(evidence?.textContent).toContain("Coverage 66.7% · threshold 0.01%");
    expect(evidence?.textContent).toContain("2 of 3 eligible cases");
    expect(evidence?.textContent).toContain("4 of 5 relay requests succeeded");
    expect(evidence?.textContent).toContain("2 of 3 expected executions matched");
    expect(evidence?.textContent).toContain("3 observed · 1 unexpected");
    expect(evidence?.textContent).toContain(
      "Prompts, responses, and answer content are not included",
    );
    expect(evidence?.textContent).not.toContain(privateMarker);
  });

  it("omits the equation for a score with no breakdown", async () => {
    serve({ quorum: 3, scores: [{ validator_hotkey: V1, composite: 0.5, bench_version: 7 }] });
    mount();
    const scope = await block();
    expect(equations(scope)).toEqual([]);
  });

  it("omits the per-question section for a score with no case results", async () => {
    serve({
      quorum: 3,
      scores: [{ validator_hotkey: V1, composite: 0.5, bench_version: 7, case_results: [] }],
    });
    mount();
    const scope = await block();
    expect(scope.querySelector("details.cases")).toBeNull();
  });

  it("names a present token multiplier in the equation", async () => {
    serve({
      quorum: 3,
      scores: [
        {
          validator_hotkey: V1,
          composite: 0.547,
          bench_version: 7,
          composite_breakdown: {
            base_accuracy: 0.62,
            benchmark_quality_multiplier: 0.9,
            pre_token_composite: 0.558,
            token_efficiency_multiplier: 0.98,
            final_composite: 0.547,
          },
        },
      ],
    });
    mount();
    const scope = await block();
    expect(equations(scope)).toEqual(["0.620 × 0.900 × 0.980 = 0.547"]);
  });
});

describe("Consensus · whole-block absences", () => {
  it("announces the fetch while it is in flight", () => {
    serve(recorded);
    mount();
    const state = document.querySelector(".pipeline-detail-state");
    expect(state?.className).toBe("pipeline-detail-state loading");
    expect(state).toHaveAttribute("role", "status");
  });

  it("states the absence for an agent with no published scores", async () => {
    serve({ quorum: 3, scores: [] });
    mount();
    await waitFor(() => {
      expect(document.querySelector(".pipeline-detail-state")?.textContent).toBe(
        "No validator score has been published for this agent yet.",
      );
    });
    expect(document.querySelector(".stat-group")).toBeNull();
  });

  it("states the same absence rather than an error when the endpoint fails", async () => {
    serve({ detail: "not found" }, 404);
    mount();
    await waitFor(() => {
      expect(document.querySelector(".pipeline-detail-state")?.textContent).toBe(
        "No validator score has been published for this agent yet.",
      );
    });
  });
});

describe("Consensus · mounted in the miner modal", () => {
  it("fills #d-consensus for the open miner's best-scoring agent", async () => {
    restoreFetch = installFixtureFetch();
    const entries = rankEntries(leaderboard.entries ?? []);
    const top = entries[0] as (typeof entries)[number];
    expect(top.agent_id).toBe(FIXTURE_TOP_AGENT_ID);
    render(() => (
      <EntityPanel
        entries={() => entries}
        operations={() => operations}
        validatorNames={() => ({})}
        currentBench={() => 7}
        settledView={() => false}
      />
    ));
    history.replaceState(null, "", "/#/leaderboard?miner=" + top.miner_hotkey);
    syncFromLocation();
    const box = await waitFor(() => {
      const el = document.querySelector<HTMLElement>("#d-consensus > .stat-group");
      if (!el) throw new Error("consensus block not mounted");
      return el;
    });
    expect(box.querySelector(".stat-head")?.textContent).toBe("Consensus (k=3)");
    expect(cohortNames(box)).toEqual(["Bench v7"]);
    expect(scoreRows(box)).toHaveLength(3);
  });
});
