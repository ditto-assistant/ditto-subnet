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
  expectedGroupCount: number;
  quarantinedGroupCount: number;
  missingResultCount: number;
  untrustedResultCount: number;
  weightEligible: false;
}>;

export function codingMemoryShadowSummary(
  value: CodingMemoryShadowDiagnostic,
): Readonly<Record<string, number | boolean>> {
  const rates = [value.p0, value.p1, value.p2, value.p3, value.p4];
  const counts = [
    value.expectedGroupCount,
    value.quarantinedGroupCount,
    value.missingResultCount,
    value.untrustedResultCount,
  ];
  if (
    counts.some((count) => !Number.isSafeInteger(count) || count < 0) ||
    value.expectedGroupCount === 0 ||
    value.quarantinedGroupCount >= value.expectedGroupCount ||
    rates.some((rate) => !Number.isFinite(rate) || rate < 0 || rate > 1) ||
    [value.usefulLift, value.staleDelta, value.irrelevantDelta].some(
      (delta) => !Number.isFinite(delta) || delta < -1 || delta > 1,
    ) ||
    !Number.isFinite(value.monotoneShadowScore) ||
    value.monotoneShadowScore < 0 ||
    value.monotoneShadowScore > 1
  ) {
    throw new Error("invalid aggregate Coding Memory shadow diagnostic");
  }
  return Object.freeze({
    expected_group_count: value.expectedGroupCount,
    irrelevant_delta: value.irrelevantDelta,
    missing_result_count: value.missingResultCount,
    monotone_shadow_score: value.monotoneShadowScore,
    p0: value.p0,
    p1: value.p1,
    p2: value.p2,
    p3: value.p3,
    p4: value.p4,
    quarantined_group_count: value.quarantinedGroupCount,
    stale_delta: value.staleDelta,
    useful_lift: value.usefulLift,
    untrusted_result_count: value.untrustedResultCount,
    weight_eligible: false,
  });
}
