import { describe, expect, it } from "vitest";

import {
  benchBadgeLabel,
  benchmarkAuthorityState,
  benchmarkDisplayVersion,
  leaderboardBenchState,
  rolloutCollectingVersion,
  rolloutStripState,
  settledBenchVersion,
} from "./bench-state";

describe("benchmarkAuthorityState (row 19)", () => {
  // An in-flight rollout target (desired v7) must never be reported as
  // active — only "activated" stops "rolling"; missing desired falls back to
  // the active version.
  it("keeps an in-flight rollout target out of the active seat", () => {
    expect(benchmarkAuthorityState(6, 7, "collecting")).toEqual({
      active: 6,
      desired: 7,
      rolling: true,
    });
    expect(benchmarkAuthorityState(6, 7, "blocked_ineligible")).toEqual({
      active: 6,
      desired: 7,
      rolling: true,
    });
  });

  it("stops rolling only on activation", () => {
    expect(benchmarkAuthorityState(6, 7, "activated")).toEqual({
      active: 6,
      desired: 7,
      rolling: false,
    });
    // A superseded rollout still carries desired > active but was never
    // rolling toward activation.
    expect(benchmarkAuthorityState(6, 7, "superseded")).toEqual({
      active: 6,
      desired: 7,
      rolling: false,
    });
  });

  it("falls back to the active version when no desired version exists", () => {
    expect(benchmarkAuthorityState(6, null, "inactive")).toEqual({
      active: 6,
      desired: 6,
      rolling: false,
    });
  });

  it("reports nothing before live data arrives", () => {
    expect(benchmarkAuthorityState(null, null, "inactive")).toEqual({
      active: null,
      desired: null,
      rolling: false,
    });
  });
});

describe("leaderboardBenchState (row 20)", () => {
  // The leaderboard's selected version stays independent of active/desired:
  // a historical view selects v5 while v6 is active and v7 rolling out, and
  // the rollout target must never overwrite the current bench (the old suite
  // additionally banned `currentBench = data.desired_bench_version` from the
  // source; the build-invariants grep carries that half).
  it("separates active, desired, and the selected history view", () => {
    expect(leaderboardBenchState("historical", 5, 6, 7, null)).toEqual({
      active: 6,
      desired: 7,
      selected: 5,
    });
  });

  it("selects the active version on the authoritative view", () => {
    expect(leaderboardBenchState("authoritative", 7, 6, 7, null)).toEqual({
      active: 6,
      desired: 7,
      selected: 6,
    });
    expect(leaderboardBenchState("authoritative", 7, 6, 7, 7)).toEqual({
      active: 6,
      desired: 7,
      selected: 6,
    });
  });

  it("falls back through current, then the fallback, against an older API", () => {
    expect(leaderboardBenchState("authoritative", 6, null, null, 6)).toEqual({
      active: null,
      desired: null,
      selected: 6,
    });
    expect(leaderboardBenchState("authoritative", null, null, null, 4)).toEqual({
      active: null,
      desired: null,
      selected: 4,
    });
  });
});

describe("settledBenchVersion / benchmarkDisplayVersion", () => {
  it("resolves active first, then current, then null (never a literal)", () => {
    expect(settledBenchVersion(6, 7)).toBe(6);
    expect(settledBenchVersion(null, 7)).toBe(7);
    expect(settledBenchVersion(null, null)).toBeNull();
    expect(benchmarkDisplayVersion(6, 7)).toBe(6);
    expect(benchmarkDisplayVersion(null, null)).toBeNull();
  });
});

describe("benchBadgeLabel (row 33 positive strings)", () => {
  it("names the rollout transition instead of a bare 'latest' claim", () => {
    expect(benchBadgeLabel(benchmarkAuthorityState(6, 7, "collecting"), false)).toBe(
      "DittoBench v6 → v7 rollout",
    );
  });

  it("names the settled version, marking older runs when they share the board", () => {
    expect(benchBadgeLabel(benchmarkAuthorityState(6, null, "inactive"), false)).toBe(
      "DittoBench v6",
    );
    expect(benchBadgeLabel(benchmarkAuthorityState(6, null, "inactive"), true)).toBe(
      "DittoBench v6 · older runs marked",
    );
  });

  it("stays empty until live data provides a version", () => {
    expect(benchBadgeLabel(benchmarkAuthorityState(null, null, "inactive"), true)).toBe("");
  });
});

describe("rolloutStripState", () => {
  it("marks an open rollout as rolling and collecting per its status", () => {
    expect(
      rolloutStripState({ active_version: 6, desired_version: 7, status: "collecting" }),
    ).toEqual({ active: 6, desired: 7, status: "collecting", rolling: true, collecting: true });
    expect(
      rolloutStripState({ active_version: 6, desired_version: 7, status: "blocked_ineligible" })
        ?.collecting,
    ).toBe(true);
  });

  it("never describes a superseded rollout as rolling out", () => {
    // It still carries its old desired_version, but nothing is collecting
    // for it and weights will never move to it.
    const superseded = rolloutStripState({
      active_version: 6,
      desired_version: 7,
      status: "superseded",
    });
    expect(superseded?.rolling).toBe(false);
    expect(superseded?.collecting).toBe(false);
  });

  it("defaults a missing status to 'inactive' and hides without both versions", () => {
    expect(rolloutStripState({ active_version: 6, desired_version: 6 })?.status).toBe("inactive");
    expect(rolloutStripState({ active_version: 6 })).toBeNull();
    expect(rolloutStripState({ desired_version: 7 })).toBeNull();
    expect(rolloutStripState(null)).toBeNull();
  });
});

describe("rolloutCollectingVersion (timeline open-era marking)", () => {
  it("marks the desired version open while the rollout is neither activated nor superseded", () => {
    expect(rolloutCollectingVersion({ desired_version: 7, status: "collecting" })).toBe(7);
    expect(rolloutCollectingVersion({ desired_version: 7, status: "blocked_ineligible" })).toBe(7);
  });

  it("closes the era once activated or superseded (only 'activated' stops 'rolling', but both settle the band)", () => {
    expect(rolloutCollectingVersion({ desired_version: 7, status: "activated" })).toBeNull();
    expect(rolloutCollectingVersion({ desired_version: 7, status: "superseded" })).toBeNull();
    expect(rolloutCollectingVersion(null)).toBeNull();
    expect(rolloutCollectingVersion({ status: "collecting" })).toBeNull();
  });
});
