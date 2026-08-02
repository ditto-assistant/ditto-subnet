// Parity tests for the benchmark page (assert-inventory rows 37, 39, 41 from
// TestDashboardScoringTransparency). Class docstring rationale, kept:
// "The SPA must not restate consensus parameters as literals. Every number
// here (the incumbent margin, the champion share, the tail size, the
// authority-switch threshold, the benchmark version) is served by the API and
// can change without touching this file. A literal in the markup is a claim
// that silently stops being true, which is worse than no claim at all: a
// miner reads it as the rule they are being judged by."
import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { compositeCalculationRows } from "../lib/scoring";
import { syncFromLocation } from "../stores/routeStore";
import { installFixtureFetch } from "../test-fixtures";
import { BenchmarkPage } from "./BenchmarkPage";

let restoreFetch: (() => void) | null = null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-31T14:00:00Z"));
  history.replaceState(null, "", "/#/benchmark");
  syncFromLocation();
  restoreFetch = installFixtureFetch();
});

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
  vi.useRealTimers();
});

async function renderPage(): Promise<void> {
  render(() => <BenchmarkPage />);
  await waitFor(() => expect(document.getElementById("bs-version")?.textContent).toBe("v7"));
}

function text(id: string): string {
  return document.getElementById(id)?.textContent ?? "";
}

// ── Row 37: test_explainer_covers_scoring_emissions_and_koth ────────────────
describe("explainer covers scoring, emissions and KOTH (row 37)", () => {
  it("renders the four disclosures", async () => {
    await renderPage();
    for (const id of ["scoring-explainer", "bench-setup", "bench-versions", "bench-glossary"]) {
      const details = document.querySelector(`details.bench-disclosure#${id}`);
      expect(details, id).toBeTruthy();
    }
  });

  it("covers every scoring/emissions/KOTH heading with the formula copy", async () => {
    await renderPage();
    const explainer = document.getElementById("scoring-explainer")?.textContent ?? "";
    for (const heading of [
      "What a score is.",
      "Which runs rank.",
      "Scores compare only within one benchmark version.",
      "How emissions work.",
      "How the crown changes hands.",
      "When weights move to a new version.",
    ]) {
      expect(explainer, heading).toContain(heading);
    }
    expect(explainer).toContain("0.5 × tool mean + 0.5 × memory mean");
    expect(explainer).toContain("Bench v6 and older retain their signed legacy token penalty");
    expect(explainer).toContain("How the current token-efficiency bonus works.");
    // The 50% pillar tags explain both halves of the composite.
    const tips = Array.from(
      document.querySelectorAll(".about .pillar .tag.tip"),
      (el) => el.getAttribute("data-tooltip") ?? "",
    );
    expect(tips.join(" ")).toContain("Memory cases contribute half of the unadjusted composite.");
    expect(tips.join(" ")).toContain("Tool-use cases contribute the other half");
  });

  it("fills the fold parameters from the API payloads, never literals", async () => {
    await renderPage();
    expect(text("ex-champion-share")).toBe("65.0%");
    expect(text("ex-tail-size")).toBe("4");
    expect(text("ex-tail-share")).toBe("35.0%");
    expect(text("ex-margin")).toBe("0.007 composite points hysteresis");
    expect(text("ex-dethrone-z")).toBe("1.64");
    // The rollout threshold is read from /public/bench/rollout.
    expect(document.querySelector(".quorum-needed")?.textContent).toBe("5");
    // Banned fold literals (row 35's contract, kept off this page).
    const body = document.body.textContent ?? "";
    expect(body).not.toContain("2% protection margin");
    expect(body).not.toContain("receives 90% of the miner pool");
    expect(body).not.toContain("up to four participation-tail recipients");
  });

  it("renders the changelog newest-first with the active version highlighted", async () => {
    await renderPage();
    expect(document.getElementById("bench-versions")?.textContent).toContain(
      "the active version is highlighted",
    );
    const items = Array.from(document.querySelectorAll("#bench-changelog .ver-item"));
    expect(items.length).toBe(6);
    const first = items[0] as HTMLElement;
    expect(first.classList.contains("ver-current")).toBe(true);
    expect(first.querySelector(".ver-tag")?.textContent).toBe("v7");
    expect(first.querySelector(".ver-now")?.textContent).toBe("active");
    // No rollout is open in the fixtures (status "activated"), so nothing may
    // carry the rollout tag — an in-flight target is never promoted either
    // (see components/benchmark/Reference.test.tsx for the open-rollout case).
    expect(document.querySelector("#bench-changelog .ver-next")).toBeNull();
    const tags = items.map((item) => item.querySelector(".ver-tag")?.textContent);
    expect(tags).toEqual(["v7", "v6", "v5", "v4", "v3", "v2"]);
  });

  it("renders the glossary grouped by kind with counts from the payload", async () => {
    await renderPage();
    const display = document.getElementById("bench-glossary-display") as HTMLElement;
    const summaries = Array.from(
      display.querySelectorAll("details.gloss > summary"),
      (el) => el.textContent,
    );
    expect(summaries).toEqual([
      "Score metrics & gate factors (10)",
      "Every scored test category (73)",
    ]);
    const groups = Array.from(display.querySelectorAll(".gcat-head"), (el) => el.textContent);
    expect(groups).toEqual([
      "Memory",
      "Conversational grounding",
      "Multi-step tool trajectories",
      "Tool use",
      "Anti-gaming / integrity",
    ]);
    expect(display.textContent).toContain("What each case probes, never the answer key.");
  });
});

// ── Row 39: test_benchmark_version_is_never_a_literal ───────────────────────
// The static markup carries only a placeholder — the frozen-setup tag and the
// version copy are filled from the API.
describe("benchmark version is never a literal (row 39)", () => {
  it("shows placeholders until the API supplies a version", async () => {
    restoreFetch?.();
    restoreFetch = null;
    globalThis.fetch = (() => Promise.reject(new Error("offline"))) as typeof fetch;
    render(() => <BenchmarkPage />);
    await waitFor(() => expect(document.getElementById("bs-version")).toBeTruthy());
    expect(document.getElementById("bs-version")?.textContent).toBe("v–");
    expect(document.getElementById("bs-version")?.classList.contains("tag")).toBe(true);
    expect(document.querySelector(".bv-desired")?.textContent).toBe("–");
    // The static fold copy stays generic — no number is invented offline.
    expect(text("ex-champion-share")).toBe("a fixed share");
    expect(document.querySelector(".quorum-needed")?.textContent).toBe("the required number of");
  });

  it("adopts the served version everywhere once it lands", async () => {
    await renderPage();
    expect(text("bs-version")).toBe("v7");
    expect(document.querySelector(".bv-desired")?.textContent).toBe("7");
    // The frozen setup mirrors the config payload, not the static fallback.
    expect(text("bs-model")).toBe("openai/gpt-oss-20b");
    expect(text("bs-serving")).toBe("OpenRouter dynamic provider route");
    expect(text("bs-thinking")).toBe("medium");
    // No mirror template in the fixture → the verify-yourself item stays hidden.
    expect((document.getElementById("bs-mirror-li") as HTMLElement).style.display).toBe("none");
    // The version copy comes from the glossary changelog for v7.
    expect(text("bs-v4-copy")).toContain("GPT-OSS inference contract");
  });
});

// ── Row 41: test_neighbouring_comparison_features_survive ───────────────────
// Docstring, kept: "Removing the baseline must not touch the features that
// sat near it. The off-network harness comparison and the token-efficiency
// budget are separate measurements that merely live beside the removed card;
// both stay." In the SPA split the off-network harness comparison (Hermes
// Agent / OpenClaw, THIRD_PARTY_HARNESSES) leads the OVERVIEW page and is
// guarded by rows 2/5/6 in Overview.test.tsx; the token-efficiency budget is
// the neighbour this page and the scoring lib keep.
describe("neighbouring comparison features survive (row 41)", () => {
  it("keeps the token-efficiency budget copy on the page", async () => {
    await renderPage();
    const explainer = document.getElementById("scoring-explainer")?.textContent ?? "";
    expect(explainer).toContain("Bench v6 and older retain their signed legacy token penalty");
  });

  it("keeps the baseline_total_tokens budget in the composite breakdown", () => {
    const rows = compositeCalculationRows({
      tool_mean: 0.8,
      memory_mean: 0.6,
      composite_breakdown: {
        base_accuracy: 0.7,
        benchmark_quality_multiplier: 0.9,
        pre_token_composite: 0.63,
        final_composite: 0.62,
        token_penalty: 0.016,
        token_efficiency_multiplier: 0.984,
      },
      token_efficiency: {
        observed_total_tokens: 12345,
        baseline_total_tokens: 20000,
        budget_percentile: 0.9,
      },
    });
    const observed = rows?.find((row) => row.k === "Observed token use");
    expect(observed?.v).toBe("12,345 / 20,000 p90 baseline");
  });
});
