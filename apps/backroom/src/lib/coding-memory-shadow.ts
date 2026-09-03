/** Safe, aggregate-only presentation model for Coding Memory v2 shadow data. */

export type CodingMemoryShadowDiagnostic = Readonly<{
  p0: number;
  p1: number;
  p2: number;
  p3: number;
  p4: number;
  usefulLift: number;
  staleDelta: number;
  irrelevantDelta: number;
  monotoneShadowScore: number;
  sampleGroups: number;
  weightEligible: false;
}>;

export function codingMemoryShadowSummary(
  value: CodingMemoryShadowDiagnostic,
): Readonly<Record<string, number | boolean>> {
  const rates = [value.p0, value.p1, value.p2, value.p3, value.p4];
  if (
    value.sampleGroups < 0 ||
    rates.some((rate) => !Number.isFinite(rate) || rate < 0 || rate > 1) ||
    !Number.isFinite(value.monotoneShadowScore) ||
    value.monotoneShadowScore < 0 ||
    value.monotoneShadowScore > 1
  ) {
    throw new Error("invalid aggregate Coding Memory shadow diagnostic");
  }
  return Object.freeze({
    irrelevant_delta: value.irrelevantDelta,
    monotone_shadow_score: value.monotoneShadowScore,
    p0: value.p0,
    p1: value.p1,
    p2: value.p2,
    p3: value.p3,
    p4: value.p4,
    sample_groups: value.sampleGroups,
    stale_delta: value.staleDelta,
    useful_lift: value.usefulLift,
    weight_eligible: false,
  });
}
