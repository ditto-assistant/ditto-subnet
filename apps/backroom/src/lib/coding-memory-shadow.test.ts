import { describe, expect, it } from "vitest";

import { codingMemoryShadowSummary } from "./coding-memory-shadow";

describe("codingMemoryShadowSummary", () => {
  it("exposes only aggregate shadow measurements", () => {
    expect(
      codingMemoryShadowSummary({
        p0: 0.4,
        p1: 0.6,
        p2: 0.5,
        p3: 0.45,
        p4: 0.7,
        usefulLift: 0.2,
        staleDelta: 0.05,
        irrelevantDelta: 0.1,
        monotoneShadowScore: 0.55,
        expectedGroupCount: 12,
        quarantinedGroupCount: 1,
        missingResultCount: 2,
        untrustedResultCount: 1,
        weightEligible: false,
      }),
    ).toEqual({
      expected_group_count: 12,
      irrelevant_delta: 0.1,
      missing_result_count: 2,
      monotone_shadow_score: 0.55,
      p0: 0.4,
      p1: 0.6,
      p2: 0.5,
      p3: 0.45,
      p4: 0.7,
      quarantined_group_count: 1,
      stale_delta: 0.05,
      useful_lift: 0.2,
      untrusted_result_count: 1,
      weight_eligible: false,
    });
  });

  it("rejects an aggregate with every expected group quarantined", () => {
    expect(() =>
      codingMemoryShadowSummary({
        p0: 0,
        p1: 0,
        p2: 0,
        p3: 0,
        p4: 0,
        usefulLift: 0,
        staleDelta: 0,
        irrelevantDelta: 0,
        monotoneShadowScore: 0,
        expectedGroupCount: 1,
        quarantinedGroupCount: 1,
        missingResultCount: 0,
        untrustedResultCount: 0,
        weightEligible: false,
      }),
    ).toThrow("invalid aggregate");
  });

  it("rejects runtime attempts to mark shadow evidence weight eligible", () => {
    const value = {
      p0: 0,
      p1: 0,
      p2: 0,
      p3: 0,
      p4: 0,
      usefulLift: 0,
      staleDelta: 0,
      irrelevantDelta: 0,
      monotoneShadowScore: 0,
      expectedGroupCount: 1,
      quarantinedGroupCount: 0,
      missingResultCount: 5,
      untrustedResultCount: 0,
      weightEligible: true,
    };
    expect(() =>
      codingMemoryShadowSummary(
        value as unknown as Parameters<typeof codingMemoryShadowSummary>[0],
      ),
    ).toThrow("invalid aggregate");
  });
});
