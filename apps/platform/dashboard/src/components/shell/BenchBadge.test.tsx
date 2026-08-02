// Parity tests for the sidebar bench badge (assert-inventory row 33,
// test_benchmark_badge_communicates_rollout_transition): the badge names the
// rollout *transition* ("DittoBench v6 → v7 rollout") instead of a bare
// "latest" claim, and the in-flight rollout target is never promoted to the
// active seat. (The "· latest" negative grep also lives in
// src/build-invariants.test.ts.)
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import { BenchBadge } from "./BenchBadge";

afterEach(cleanup);

function badge(): HTMLElement {
  const el = document.getElementById("bench-badge");
  if (!el) throw new Error("bench badge missing");
  return el;
}

describe("BenchBadge (row 33)", () => {
  it("names the rollout transition while collecting", () => {
    render(() => <BenchBadge active={6} desired={7} status="collecting" hasOlderRuns={false} />);
    expect(badge().textContent).toBe("DittoBench v6 → v7 rollout");
    expect(badge().className).toBe("bench-badge show");
  });

  it("keeps naming the transition while blocked_ineligible", () => {
    render(() => (
      <BenchBadge active={6} desired={7} status="blocked_ineligible" hasOlderRuns={false} />
    ));
    expect(badge().textContent).toBe("DittoBench v6 → v7 rollout");
  });

  it("never promotes the rollout target: a superseded rollout names only the active version", () => {
    render(() => <BenchBadge active={6} desired={7} status="superseded" hasOlderRuns={false} />);
    expect(badge().textContent).toBe("DittoBench v6");
  });

  it("marks older runs beside the settled version", () => {
    render(() => <BenchBadge active={7} desired={7} status="activated" hasOlderRuns={true} />);
    expect(badge().textContent).toBe("DittoBench v7 · older runs marked");
  });

  it("names the version plainly when nothing is rolling and no older runs exist", () => {
    render(() => <BenchBadge active={7} desired={7} status="activated" hasOlderRuns={false} />);
    expect(badge().textContent).toBe("DittoBench v7");
  });

  it("renders an empty, hidden badge before live data arrives", () => {
    render(() => <BenchBadge active={null} desired={null} status={null} hasOlderRuns={false} />);
    expect(badge().textContent).toBe("");
    expect(badge().className).toBe("bench-badge");
  });

  it('never makes a bare "latest" claim', () => {
    render(() => <BenchBadge active={7} desired={7} status="activated" hasOlderRuns={false} />);
    expect(badge().textContent).not.toContain("latest");
  });
});
