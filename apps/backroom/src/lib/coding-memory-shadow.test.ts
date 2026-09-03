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
        sampleGroups: 12,
        weightEligible: false,
      }),
    ).toEqual({
      irrelevant_delta: 0.1,
      monotone_shadow_score: 0.55,
      p0: 0.4,
      p1: 0.6,
      p2: 0.5,
      p3: 0.45,
      p4: 0.7,
      sample_groups: 12,
      stale_delta: 0.05,
      useful_lift: 0.2,
      weight_eligible: false,
    });
  });
});
