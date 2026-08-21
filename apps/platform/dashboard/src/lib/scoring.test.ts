// Parity tests for the scoring formulas (assert-inventory rows 36–38).
//
// From the original TestDashboardScoringTransparency docstring: consensus
// parameters (the incumbent margin, the champion share, the tail size, the
// authority-switch threshold, the benchmark version) are served by the API
// and can change without touching this code. A literal in the markup is a
// claim that silently stops being true, which is worse than no claim at all:
// a miner reads it as the rule they are being judged by. Every function here
// therefore derives from payload values, and these tests feed those values in.

import { describe, expect, it } from "vitest";

import {
  countdownClock,
  epochCountdown,
  COMPOSITE_CALC_HEADING,
  COMPOSITE_CALC_NOTE,
  boardEntryCompare,
  caseVerdict,
  chainChampionCompare,
  chainWeightLabel,
  cohortMedian,
  compositeCalculationRows,
  compositeCalculationHeading,
  compositeEquationText,
  continualSampleCount,
  continualWaves,
  crownChampionScoreLabel,
  crownChallengerScoreLabel,
  crownComparisonNote,
  crownContest,
  crownDifferenceText,
  crownHeldRowLabel,
  crownHeldRowTip,
  crownScaleNote,
  crownSeedDiffsText,
  crownThresholdLabel,
  crownWhyHigh,
  curveV3ScoreAdjustment,
  efficiencyTieBreakChipLabel,
  efficiencyBoardStatus,
  dethroneBandScale,
  dethroneFloor,
  displayComposite,
  embargoHours,
  foldArrival,
  foldArrivalMs,
  foldArrivalTip,
  tarballArrivalDiffers,
  emissionsSplit,
  errBandBounds,
  foldChainWeights,
  isEligible,
  isFinalized,
  isOlderRun,
  isRegistered,
  maxTokenPenaltyPct,
  qualityGateChipLabel,
  rankEntries,
  rolloutQuorum,
  rolloutSettledView,
  scoreClass,
  scoreQuorum,
  showsCompositeErrBand,
  tokenPenaltyChipLabel,
  trendDirection,
  unrankedKind,
  validatorWeightViews,
  vectorChampion,
} from "./scoring";
import type { CompositeBreakdown } from "../types";

describe("rolloutSettledView", () => {
  it("is true only for an authoritative view with an open active < desired rollout", () => {
    expect(
      rolloutSettledView({
        selection_mode: "current",
        active_bench_version: 6,
        desired_bench_version: 7,
      }),
    ).toBe(true);
  });

  it("is false for historical views, settled versions, and missing values", () => {
    expect(
      rolloutSettledView({
        selection_mode: "historical",
        active_bench_version: 6,
        desired_bench_version: 7,
      }),
    ).toBe(false);
    expect(
      rolloutSettledView({
        selection_mode: "current",
        active_bench_version: 6,
        desired_bench_version: 6,
      }),
    ).toBe(false);
    // Number(null) is 0, Number(undefined) is NaN — neither opens the view.
    expect(rolloutSettledView({ selection_mode: "current", desired_bench_version: 7 })).toBe(false);
    expect(rolloutSettledView({ selection_mode: "current", active_bench_version: 6 })).toBe(false);
  });
});

describe("foldArrival", () => {
  it("prefers the lineage crown clock over this tarball's upload", () => {
    const hogwarts = {
      first_seen: "2026-08-19T09:11:01.800258Z",
      crown_first_seen: "2026-08-19T05:31:40.678880Z",
    };
    expect(foldArrival(hogwarts)).toBe("2026-08-19T05:31:40.678880Z");
    expect(foldArrivalMs(hogwarts)).toBe(Date.parse("2026-08-19T05:31:40.678880Z"));
    expect(tarballArrivalDiffers(hogwarts)).toBe(true);
    expect(foldArrivalTip(hogwarts)).toContain("2026-08-19T05:31:40.678880Z");
    expect(foldArrivalTip(hogwarts)).toContain("2026-08-19T09:11:01.800258Z");
    expect(foldArrivalTip(hogwarts)).toContain("not this tarball");
    expect(foldArrivalTip(hogwarts)).toContain("not screen-complete");
  });

  it("falls back to this tarball when no lineage clock is published", () => {
    const omar = { first_seen: "2026-08-19T06:24:06.858354Z" };
    expect(foldArrival(omar)).toBe("2026-08-19T06:24:06.858354Z");
    expect(tarballArrivalDiffers(omar)).toBe(false);
    expect(foldArrival(omar)! < "2026-08-19T09:11:01.800258Z").toBe(true);
    expect(foldArrival({ crown_first_seen: "", first_seen: "2026-08-19T06:24:06.858354Z" })).toBe(
      "2026-08-19T06:24:06.858354Z",
    );
    expect(foldArrival({})).toBeNull();
    expect(foldArrivalMs({})).toBeNull();
  });
});

describe("displayComposite", () => {
  it("prefers official_composite over composite outside a rollout", () => {
    expect(displayComposite({ composite: 0.5, official_composite: 0.6 })).toBe(0.6);
    expect(displayComposite({ composite: 0.5 })).toBe(0.5);
    expect(displayComposite({ composite: 0.5, official_composite: null })).toBe(0.5);
  });

  it("ranks by the settled active-version median mid-rollout", () => {
    const entry = { composite: 0.7, official_composite: 0.7, settled_composite: 0.65 };
    expect(displayComposite(entry, true)).toBe(0.65);
    // An agent first scored during the rollout has no settled median and
    // falls back to its authoritative value.
    expect(displayComposite({ composite: 0.7, settled_composite: null }, true)).toBe(0.7);
    expect(displayComposite(entry, false)).toBe(0.7);
  });
});

describe("dethroneBandScale", () => {
  const emissions = {
    band_decay_min_bench_version: 6,
    band_decay_start_composite: 0.8,
    band_decay_rate: 2,
  };

  it("returns 1 when the decay parameters are absent or inert", () => {
    expect(dethroneBandScale(null, { bench_version: 7 }, 0.9)).toBe(1);
    expect(dethroneBandScale({}, { bench_version: 7 }, 0.9)).toBe(1);
    expect(dethroneBandScale({ ...emissions, band_decay_rate: 0 }, { bench_version: 7 }, 0.9)).toBe(
      1,
    );
    expect(
      dethroneBandScale({ ...emissions, band_decay_rate: -1 }, { bench_version: 7 }, 0.9),
    ).toBe(1);
    // Champion version unknown → not finite → no decay.
    expect(dethroneBandScale(emissions, {}, 0.9)).toBe(1);
  });

  it("returns 1 below the minimum bench version", () => {
    expect(dethroneBandScale(emissions, { bench_version: 5 }, 0.9)).toBe(1);
  });

  it("uses the LOWER of champion and challenger versions", () => {
    // Challenger still on v5 pulls the pair below the v6 floor.
    expect(dethroneBandScale(emissions, { bench_version: 7 }, 0.9, { bench_version: 5 })).toBe(1);
    expect(
      dethroneBandScale(emissions, { bench_version: 7 }, 0.9, { bench_version: 6 }),
    ).toBeCloseTo(Math.exp(-2 * (0.9 - 0.8)), 12);
  });

  it("decays exponentially from the start composite, bounded to [start, 1]", () => {
    // Below the decay start, scale stays exactly 1 (exp(0)).
    expect(dethroneBandScale(emissions, { bench_version: 7 }, 0.75)).toBe(1);
    expect(dethroneBandScale(emissions, { bench_version: 7 }, 0.9)).toBeCloseTo(
      Math.exp(-2 * 0.1),
      12,
    );
    // A composite above 1 is bounded at 1 before decaying.
    expect(dethroneBandScale(emissions, { bench_version: 7 }, 1.2)).toBeCloseTo(
      Math.exp(-2 * 0.2),
      12,
    );
  });
});

describe("dethroneFloor", () => {
  it("is ADDITIVE: champComposite + effectiveMargin, never champ * (1 + margin)", () => {
    // The old Python suite asserted the literal "champComposite * (1 + margin)"
    // was ABSENT from the source; this unit test on the correct math is its
    // translation. With champ 0.9 and margin 0.02 the two formulas disagree
    // (0.92 vs 0.918), so the assertion genuinely discriminates.
    const result = dethroneFloor({ margin: 0.02 }, { composite: 0.9 });
    expect(result).not.toBeNull();
    const floor = (result as NonNullable<typeof result>).floor;
    expect(floor).toBeCloseTo(0.9 + 0.02, 12);
    expect(floor).not.toBeCloseTo(0.9 * (1 + 0.02), 12);
  });

  it("scales the margin (not the floor) by the decay band", () => {
    const emissions = {
      margin: 0.02,
      band_decay_min_bench_version: 6,
      band_decay_start_composite: 0.8,
      band_decay_rate: 2,
    };
    const result = dethroneFloor(emissions, { composite: 0.9, bench_version: 7 });
    expect(result).not.toBeNull();
    const scale = Math.exp(-2 * (0.9 - 0.8));
    expect(result?.scale).toBeCloseTo(scale, 12);
    expect(result?.effectiveMargin).toBeCloseTo(0.02 * scale, 12);
    // Still additive after scaling.
    expect(result?.floor).toBeCloseTo(0.9 + 0.02 * scale, 12);
  });

  it("caps the margin at half the remaining headroom once the fleet activates it", () => {
    // The live saturated plateau: 0.007 * exp(-2 * (0.997012 - 0.6)) = 0.003164
    // published a floor of 1.000176, i.e. above a perfect score. The published
    // number has to match the fold, or the board tells miners to reach a score
    // the benchmark cannot express.
    const base = {
      margin: 0.007,
      band_decay_min_bench_version: 6,
      band_decay_start_composite: 0.6,
      band_decay_rate: 2,
      ceiling_headroom_share: 0.5,
    };
    const champion = { composite: 0.997012, bench_version: 9 };

    const uncapped = dethroneFloor(base, champion);
    expect(uncapped?.floor).toBeGreaterThan(1);

    const capped = dethroneFloor({ ...base, ceiling_band_clamp_active: true }, champion);
    expect(capped?.effectiveMargin).toBeCloseTo(0.5 * (1 - 0.997012), 12);
    expect(capped?.floor).toBeCloseTo(0.997012 + 0.5 * (1 - 0.997012), 12);
    expect(capped?.floor).toBeLessThan(1);
  });

  it("leaves the floor untouched away from the ceiling, and never raises it", () => {
    const base = {
      margin: 0.007,
      band_decay_min_bench_version: 6,
      band_decay_start_composite: 0.6,
      band_decay_rate: 2,
      ceiling_headroom_share: 0.5,
      ceiling_band_clamp_active: true,
    };
    for (const composite of [0.6, 0.75, 0.85, 0.95, 0.99]) {
      const champion = { composite, bench_version: 9 };
      const capped = dethroneFloor(base, champion);
      const uncapped = dethroneFloor({ ...base, ceiling_band_clamp_active: false }, champion);
      expect(capped!.floor).toBeLessThanOrEqual(uncapped!.floor);
    }
    // Two band widths below the ceiling the cap cannot bind at all.
    const champion = { composite: 0.85, bench_version: 9 };
    expect(dethroneFloor(base, champion)!.floor).toBeCloseTo(
      dethroneFloor({ ...base, ceiling_band_clamp_active: false }, champion)!.floor,
      12,
    );
  });

  it("returns null without emissions, a champion, or a finite margin", () => {
    expect(dethroneFloor(null, { composite: 0.9 })).toBeNull();
    expect(dethroneFloor({ margin: 0.02 }, null)).toBeNull();
    expect(dethroneFloor({}, { composite: 0.9 })).toBeNull();
    expect(dethroneFloor({ margin: NaN }, { composite: 0.9 })).toBeNull();
  });

  it("surfaces dethrone_z only when finite and positive", () => {
    expect(dethroneFloor({ margin: 0.02, dethrone_z: 1.96 }, { composite: 0.9 })?.z).toBe(1.96);
    expect(dethroneFloor({ margin: 0.02, dethrone_z: 0 }, { composite: 0.9 })?.z).toBeNull();
    expect(dethroneFloor({ margin: 0.02 }, { composite: 0.9 })?.z).toBeNull();
  });

  it("names the statistical band, not the flat margin, as the held-crown gate", () => {
    const contest = crownContest(
      {
        method: "paired",
        challenger_lead: 0.046535,
        required_lead: 0.070822,
        margin_lead: 0.007,
        statistical_lead: 0.13927208,
        paired_standard_error: 0.084922,
        shared_seed_count: 2,
        seed_differences: [0.167, -0.074],
      },
      { dethrone_z: 1.64 },
    );
    expect(contest?.bindingTerm).toBe("statistical");
    expect(contest?.shortfall).toBeCloseTo(0.024287, 6);
    expect(crownWhyHigh(contest!)).toContain("0.007 flat margin is not the gate");
    expect(crownWhyHigh(contest!)).toContain("2 shared seeds");
    expect(crownWhyHigh(contest!)).toContain("0.084922");
    expect(crownSeedDiffsText(contest!.seedDifferences!)).toBe(
      "Shared-seed diffs: +0.167000 · -0.074000",
    );
    expect(crownHeldRowLabel(contest!, true)).toBe("#1 · +0.046535 / need +0.070822");
    expect(crownDifferenceText(contest!)).toBe("Difference +0.046535");
  });

  it("prints the paired composites as challenger minus champion equals the lead", () => {
    const contest = crownContest({
      method: "paired",
      challenger_lead: 0.046535,
      required_lead: 0.070822,
      required_score: 0.914777,
      margin_lead: 0.007,
      statistical_lead: 0.13927208,
    });
    expect(contest?.championPairedScore).toBeCloseTo(0.843955, 6);
    expect(contest?.challengerPairedScore).toBeCloseTo(0.89049, 6);
    expect(contest?.requiredScore).toBeCloseTo(0.914777, 6);
    expect(crownDifferenceText(contest!)).toBe("0.890490 − 0.843955 = +0.046535");
  });

  it("refuses to treat the Score column as the paired dethrone bar", () => {
    // Hogwarts_v2 vs Alexandros-ditto-v11 on 2026-08-19: official 0.739125
    // vs required_score 0.724648, the exact miner misread.
    const contest = crownContest({
      method: "paired",
      challenger_lead: -0.040757,
      required_lead: 0.033269276681151067,
      required_score: 0.7246482766811511,
      margin_lead: 0.007,
      statistical_lead: 0.03994056,
      paired_standard_error: 0.024354,
      shared_seed_count: 2,
      seed_differences: [-0.065111, -0.016403],
    });
    expect(contest?.challengerPairedScore).toBeCloseTo(0.650622, 6);
    expect(crownThresholdLabel("paired")).toBe("Shared-seed bar");
    expect(crownChallengerScoreLabel("paired")).toBe("Shared-seed mean");
    expect(crownChampionScoreLabel("paired")).toBe("Champion shared-seed mean");
    expect(crownThresholdLabel("unpaired")).toBe("Score bar");
    const note = crownScaleNote(
      { composite: 0.772692, official_composite: 0.7391252142857143 },
      contest!,
    );
    expect(note).toContain("Do not compare the Score column (0.739125)");
    expect(note).toContain("shared-seed bar (>0.724648)");
    expect(note).toContain("shared-seed mean 0.650622");
    expect(note).toContain("0.772692 own-seed median");
    expect(note).not.toContain("own-seed median is the rank score");
    expect(crownComparisonNote("paired")).toContain("The Score column ranks the board");
    expect(crownComparisonNote("paired")).not.toContain("own-seed score");
    expect(crownHeldRowTip(contest!)).toContain("The shared-seed mean must exceed 0.724648");
    expect(crownHeldRowTip(contest!)).toContain("The Score column is a different number");
    expect(crownHeldRowTip(contest!)).not.toContain("The challenger score must exceed");
  });

  it("compares the unpaired bar to the Score column, not the own-seed median", () => {
    const contest = crownContest({
      method: "unpaired",
      challenger_lead: 0.0185715,
      required_lead: 0.046305194,
      required_score: 0.750175444,
    });
    const note = crownScaleNote({ composite: 0.772692, official_composite: 0.739125 }, contest!);
    expect(note).toContain("The Score column (0.739125) is the number the unpaired band compares");
    expect(note).toContain("It must exceed 0.750175");
    expect(note).toContain("0.772692 own-seed median is not the Score column");
    expect(crownComparisonNote("unpaired")).toContain("Score-column numbers");
    expect(crownHeldRowTip(contest!)).toContain("The Score column must exceed 0.750175");
  });

  it("derives paired SE from statistical_lead when the payload omits it", () => {
    const contest = crownContest(
      {
        method: "paired",
        challenger_lead: 0.005047,
        required_lead: 0.005458,
        margin_lead: 0.007,
        statistical_lead: 0.0164,
      },
      { dethrone_z: 1.64 },
    );
    expect(contest?.pairedStandardError).toBeCloseTo(0.01, 12);
    expect(contest?.bindingTerm).toBe("statistical");
  });

  it("measures the champion by its settled composite mid-rollout", () => {
    const champion = { composite: 0.95, settled_composite: 0.9 };
    expect(dethroneFloor({ margin: 0.02 }, champion, true)?.champComposite).toBe(0.9);
    expect(dethroneFloor({ margin: 0.02 }, champion, false)?.champComposite).toBe(0.95);
  });
});

describe("emissionsSplit", () => {
  it("gives the champion its share and the tail the remainder", () => {
    const split = emissionsSplit({
      champion_share: 0.9,
      tail_size: 4,
      rank_shares: [0.05, 0.03, NaN, 0.02],
      margin: 0.02,
      dethrone_z: 1.64,
    });
    expect(split).not.toBeNull();
    expect(split?.championShare).toBe(0.9);
    expect(split?.tailShare).toBeCloseTo(0.1, 12);
    expect(split?.tailSize).toBe(4);
    // Non-finite ranked shares are filtered, order preserved (descending).
    expect(split?.rankShares).toEqual([0.05, 0.03, 0.02]);
    expect(split?.margin).toBe(0.02);
    expect(split?.dethroneZ).toBe(1.64);
  });

  it("returns null when the fold has no finite champion share", () => {
    expect(emissionsSplit(null)).toBeNull();
    expect(emissionsSplit({})).toBeNull();
  });

  it("keeps a zero tail and gates margin / z exactly like the original copy", () => {
    const split = emissionsSplit({ champion_share: 1, tail_size: 0, dethrone_z: 0 });
    expect(split?.tailSize).toBe(0); // "there is no participation tail"
    expect(split?.margin).toBeNull();
    expect(split?.dethroneZ).toBeNull(); // z must be > 0 to be published
  });
});

describe("rolloutQuorum", () => {
  it("reads the fleet-wide gate raw and coerces the cohort sizes", () => {
    const q = rolloutQuorum({
      ranked_quorum_agents: 12,
      min_ranked_quorum_agents: 30,
      priority_cohort_size: 5,
      cohort_size: 25,
      cohort_ready_count: 7,
      members: [
        { position: 1, score_count: 3 },
        { position: 2, score_count: 2 }, // below the 3-score per-agent quorum
        { position: 5, score_count: 4 },
        { position: 6, score_count: 3 }, // outside the first-five barrier
      ],
    });
    expect(q.ready).toBe(12);
    expect(q.needed).toBe(30);
    expect(q.prioritySize).toBe(5);
    expect(q.cohortSize).toBe(25);
    expect(q.priorityReady).toBe(2);
    expect(q.cohortReadyCount).toBe(7);
  });

  it("preserves the Number() || defaults (5 priority, 0 cohort)", () => {
    const q = rolloutQuorum({});
    expect(q.prioritySize).toBe(5);
    expect(q.cohortSize).toBe(0);
    expect(q.priorityReady).toBe(0);
    expect(q.ready).toBeNull();
    expect(q.needed).toBeNull();
    // Number(0) || 5 → 5: a zero-size priority cohort still defaults.
    expect(rolloutQuorum({ priority_cohort_size: 0 }).prioritySize).toBe(5);
    expect(rolloutQuorum(null).cohortSize).toBe(0);
  });
});

describe("cohortMedian", () => {
  it("takes the middle score for odd counts, the mean of the two middles for even", () => {
    expect(cohortMedian([{ composite: 0.3 }, { composite: 0.1 }, { composite: 0.2 }])).toBe(0.2);
    expect(cohortMedian([{ composite: 0.4 }, { composite: 0.1 }])).toBeCloseTo(0.25, 12);
  });

  it("coerces composites with Number() like the original", () => {
    expect(cohortMedian([{ composite: "0.3" }, { composite: "0.1" }, { composite: "0.2" }])).toBe(
      0.2,
    );
  });
});

describe("eligibility predicates", () => {
  it("treats a missing eligible/finalized flag as true (older APIs omit them)", () => {
    expect(isEligible({})).toBe(true);
    expect(isEligible({ eligible: true })).toBe(true);
    expect(isEligible({ eligible: false })).toBe(false);
    expect(isEligible(null)).toBe(true);
    expect(isFinalized({})).toBe(true);
    expect(isFinalized({ finalized: false })).toBe(false);
  });

  it("requires a strict registered === true (null/missing is unknown, not false)", () => {
    expect(isRegistered({ registered: true })).toBe(true);
    expect(isRegistered({ registered: null })).toBe(false);
    expect(isRegistered({})).toBe(false);
    expect(isRegistered(null)).toBe(false);
  });
});

describe("unrankedKind", () => {
  it("is null for eligible entries", () => {
    expect(unrankedKind({ eligible: true, n: 200 })).toBeNull();
    expect(unrankedKind({})).toBeNull();
  });

  it("labels a full run (n >= 100) 'zero' — it scored a non-positive composite", () => {
    // Mirrors the backend two-gate rule: never mislabel a zero-scoring full
    // run as a small practice run.
    expect(unrankedKind({ eligible: false, n: 100 })).toBe("zero");
    expect(unrankedKind({ eligible: false, n: 250 })).toBe("zero");
  });

  it("labels smaller or unreported profiles 'provisional'", () => {
    expect(unrankedKind({ eligible: false, n: 99 })).toBe("provisional");
    expect(unrankedKind({ eligible: false, n: null })).toBe("provisional");
    expect(unrankedKind({ eligible: false })).toBe("provisional");
  });
});

describe("boardEntryCompare / rankEntries", () => {
  const finalizedLow = { composite: 0.4 };
  const finalizedHigh = { composite: 0.6 };
  const provisional = { composite: 0.9, finalized: false };
  const ineligible = { composite: 0.95, eligible: false };

  it("ranks finalized ahead of provisional and eligible ahead of ineligible", () => {
    // Finalized submissions always rank ahead of pre-quorum feedback, even
    // when the provisional composite is higher.
    expect(boardEntryCompare(finalizedLow, provisional)).toBeLessThan(0);
    expect(boardEntryCompare(ineligible, finalizedLow)).toBeGreaterThan(0);
    expect(boardEntryCompare(finalizedHigh, finalizedLow)).toBeLessThan(0);
  });

  it("assigns dual rank counters and nulls for ineligible rows", () => {
    const ranked = rankEntries([provisional, finalizedLow, ineligible, finalizedHigh]);
    // The finalized tier is checked first, so a finalized-but-ineligible row
    // still sits above pre-quorum feedback; eligibility orders within a tier.
    expect(ranked.map((e) => e.composite)).toEqual([0.6, 0.4, 0.95, 0.9]);
    // Eligible finalized rows count 1..n; provisional rows restart at 1
    // (rendered "P1"); ineligible rows carry no rank at all.
    expect(ranked.map((e) => e.rank)).toEqual([1, 2, null, 1]);
  });

  it("does not mutate the input entries (the original wrote e.rank in place)", () => {
    const input = [{ composite: 0.4 }];
    rankEntries(input);
    expect("rank" in (input[0] as object)).toBe(false);
  });

  it("orders by the settled composite mid-rollout so scales never interleave", () => {
    const flipped = { composite: 0.9, official_composite: 0.9, settled_composite: 0.5 };
    const settled = { composite: 0.6 };
    expect(boardEntryCompare(flipped, settled, true)).toBeGreaterThan(0);
    expect(boardEntryCompare(flipped, settled, false)).toBeLessThan(0);
  });
});

describe("isOlderRun", () => {
  it("marks finalized runs below the settled version, or with no version at all", () => {
    expect(isOlderRun({ bench_version: 5 }, 6)).toBe(true);
    expect(isOlderRun({ bench_version: null }, 6)).toBe(true);
    expect(isOlderRun({ bench_version: 6 }, 6)).toBe(false);
    // A provisional run is not "older", it is pending.
    expect(isOlderRun({ bench_version: 5, finalized: false }, 6)).toBe(false);
    // No settled version known yet → only the missing-version case matches.
    expect(isOlderRun({ bench_version: 5 }, null)).toBe(false);
    expect(isOlderRun({ bench_version: null }, null)).toBe(true);
  });
});

describe("quorum coercions", () => {
  it("scoreQuorum defaults to 3 and floors at 1", () => {
    expect(scoreQuorum(null)).toBe(3);
    expect(scoreQuorum(undefined)).toBe(3);
    expect(scoreQuorum(0)).toBe(3); // Number(0) || 3
    expect(scoreQuorum(5)).toBe(5);
    expect(scoreQuorum(0.5)).toBe(1); // Math.max(1, …)
    expect(scoreQuorum("4")).toBe(4);
  });

  it("continualWaves prefers retained_sample_count over completed_wave_count", () => {
    expect(continualWaves({ retained_sample_count: 7, completed_wave_count: 4 })).toBe(7);
    expect(continualWaves({ retained_sample_count: null, completed_wave_count: 4 })).toBe(4);
    expect(continualWaves({})).toBe(0);
  });

  it("continualSampleCount falls back to the initial three scores plus waves", () => {
    expect(continualSampleCount({ aggregate_sample_count: 11 })).toBe(11);
    expect(continualSampleCount({ completed_wave_count: 4 })).toBe(7);
    expect(continualSampleCount({})).toBe(3);
  });
});

describe("chain-observation champion", () => {
  it("orders by weight value descending with uid ascending as tiebreak", () => {
    const a = { hotkey: "a", value: 0.5, uid: 9 };
    const b = { hotkey: "b", value: 0.5, uid: 3 };
    const c = { hotkey: "c", value: 0.7, uid: 20 };
    expect(
      [a, b, c]
        .slice()
        .sort(chainChampionCompare)
        .map((w) => w.hotkey),
    ).toEqual(["c", "b", "a"]);
    expect(vectorChampion([a, b])?.hotkey).toBe("b");
    expect(vectorChampion([])).toBeNull();
  });

  it("folds vectors excluding the owner and non-positive weights", () => {
    const fold = foldChainWeights({
      owner_hotkey: "owner",
      vectors: [
        {
          weights: [
            { hotkey: "m1", value: 0.6, uid: 1 },
            { hotkey: "m2", value: 0.4, uid: 2 },
            { hotkey: "owner", value: 0.9, uid: 0 },
            { hotkey: "m3", value: 0, uid: 3 },
          ],
        },
        {
          weights: [
            { hotkey: "m2", value: 0.8, uid: 2 },
            { hotkey: "m1", value: 0.2, uid: 1 },
          ],
        },
        // Owner-only vector carries no miner weight and is skipped entirely.
        { weights: [{ hotkey: "owner", value: 1, uid: 0 }] },
      ],
    });
    expect(fold).not.toBeNull();
    expect(fold?.minerVectors).toBe(2);
    expect(fold?.championCounts).toEqual({ m1: 1, m2: 1 });
    expect(fold?.byHotkey["m1"]).toMatchObject({ weighted: 2, champion: 1, vectors: 2 });
    expect(fold?.byHotkey["m2"]).toMatchObject({ weighted: 2, champion: 1, vectors: 2 });
    // Mean normalized share across ALL miner-bearing vectors: m1 holds 0.6
    // of vector one and 0.2 of vector two, m2 the complements.
    expect(fold?.byHotkey["m1"]?.share).toBeCloseTo(0.4, 10);
    expect(fold?.byHotkey["m2"]?.share).toBeCloseTo(0.6, 10);
    expect(fold?.byHotkey["m3"]).toBeUndefined();
    // Ties on crown count break lexicographically.
    expect(fold?.leaders).toEqual(["m1", "m2"]);
  });

  it("builds per-validator weight views in snapshot order, heaviest first", () => {
    const views = validatorWeightViews({
      owner_hotkey: "owner",
      vectors: [
        {
          validator_uid: 7,
          validator_hotkey: "vali-a",
          weights: [
            { hotkey: "m2", value: 25, uid: 2 },
            { hotkey: "m1", value: 75, uid: 1 },
            { hotkey: "owner", value: 100, uid: 0 },
          ],
        },
        // Owner-only vector is skipped, matching foldChainWeights.
        {
          validator_uid: 8,
          validator_hotkey: "vali-b",
          weights: [{ hotkey: "owner", value: 1, uid: 0 }],
        },
      ],
    });
    expect(views).toHaveLength(1);
    expect(views?.[0]?.validatorUid).toBe(7);
    expect(views?.[0]?.validatorHotkey).toBe("vali-a");
    expect(views?.[0]?.entries.map((e) => e.uid)).toEqual([1, 2]);
    expect(views?.[0]?.entries[0]).toMatchObject({ top: true, value: 75 });
    expect(views?.[0]?.entries[0]?.share).toBeCloseTo(0.75, 10);
    expect(views?.[0]?.entries[1]).toMatchObject({ top: false, value: 25 });
    expect(validatorWeightViews(null)).toBeNull();
    expect(validatorWeightViews({})).toBeNull();
  });

  it("returns null when the chain snapshot has no vectors array", () => {
    expect(foldChainWeights(null)).toBeNull();
    expect(foldChainWeights({})).toBeNull();
  });

  it("labels rows 'Validator top choice · c/v' or 'Validator support · w/v'", () => {
    expect(chainWeightLabel({ weighted: 3, champion: 2, vectors: 5, share: 0.4 })).toBe(
      "Validator top choice · 2/5",
    );
    expect(chainWeightLabel({ weighted: 3, champion: 0, vectors: 5, share: 0.2 })).toBe(
      "Validator support · 3/5",
    );
    expect(chainWeightLabel({ weighted: 3, champion: 1, vectors: 0, share: 0 })).toBe("");
    expect(chainWeightLabel(null)).toBe("");
  });
});

describe("chip thresholds", () => {
  it("shows the quality-gate chip only when the reduction meaningfully bites", () => {
    expect(qualityGateChipLabel({ benchmark_quality_multiplier: 0.95 } as CompositeBreakdown)).toBe(
      "gates −5%",
    );
    // A reduction at or below the 0.005 dead-band is hidden (0.996 keeps the
    // float below it; 1 − 0.995 lands a hair ABOVE 0.005 in floating point
    // and legitimately shows, matching the original).
    expect(
      qualityGateChipLabel({ benchmark_quality_multiplier: 0.996 } as CompositeBreakdown),
    ).toBeNull();
    expect(
      qualityGateChipLabel({ benchmark_quality_multiplier: 1 } as CompositeBreakdown),
    ).toBeNull();
    expect(qualityGateChipLabel(null)).toBeNull();
  });

  it("labels the token penalty above the 0.00005 dead-band, else 'token no penalty'", () => {
    expect(tokenPenaltyChipLabel({ token_penalty: 0.015 } as CompositeBreakdown)).toEqual({
      label: "token −1.5%",
      penalized: true,
    });
    expect(tokenPenaltyChipLabel({ token_penalty: 0.00004 } as CompositeBreakdown)).toEqual({
      label: "token no penalty",
      penalized: false,
    });
    expect(tokenPenaltyChipLabel({ token_penalty: null } as CompositeBreakdown)).toBeNull();
    expect(tokenPenaltyChipLabel(null)).toBeNull();
  });

  it("clamps the SE band to [0, 100] with a 0.6% minimum width", () => {
    expect(errBandBounds(0.5, 0.1)).toEqual({ lo: 40, hi: 60, width: 20 });
    const tiny = errBandBounds(0.5, 0.001);
    expect(tiny?.width).toBe(0.6);
    const clamped = errBandBounds(0.99, 0.05);
    expect(clamped?.hi).toBe(100);
    expect(errBandBounds(0.5, 0)).toBeNull();
    expect(errBandBounds(0.5, null)).toBeNull();
  });

  it("attaches the band only to the authoritative composite, never a continual mean", () => {
    expect(showsCompositeErrBand({ composite: 0.5 })).toBe(true);
    // Mid-rollout the cell shows the settled value: the stashed stderr would
    // describe the wrong number, so the band is dropped.
    expect(showsCompositeErrBand({ composite: 0.5, settled_composite: 0.45 }, true)).toBe(false);
    expect(showsCompositeErrBand({ composite: 0.5, aggregate_method: "continual_mean" })).toBe(
      false,
    );
  });

  it("uses the ±0.0005 trend dead-band and the 0.8/0.5 score bands", () => {
    expect(trendDirection(0.001)).toBe("up");
    expect(trendDirection(-0.001)).toBe("down");
    expect(trendDirection(0.0005)).toBe("flat");
    expect(trendDirection(-0.0005)).toBe("flat");
    expect(scoreClass(0.8)).toBe("good");
    expect(scoreClass(0.5)).toBe("mid");
    expect(scoreClass(0.49)).toBe("low");
    expect(scoreClass(null)).toBe("");
  });

  it("grades case verdicts: binary for memory, 0.999/0.001 thresholds for tool", () => {
    expect(caseVerdict({ kind: "memory", correct: true })).toBe("pass");
    expect(caseVerdict({ kind: "memory", correct: false })).toBe("fail");
    expect(caseVerdict({ kind: "memory", correct: null })).toBe("partial");
    expect(caseVerdict({ kind: "tool", tool_score: 1 })).toBe("pass");
    expect(caseVerdict({ kind: "tool", tool_score: 0.999 })).toBe("pass");
    expect(caseVerdict({ kind: "tool", tool_score: 0 })).toBe("fail");
    expect(caseVerdict({ kind: "tool", tool_score: 0.5 })).toBe("partial");
  });
});

describe("composite equations (row 38: quality and token adjustments stay separate)", () => {
  const breakdown: CompositeBreakdown = {
    base_accuracy: 0.62,
    benchmark_quality_multiplier: 0.9,
    pre_token_composite: 0.558,
    final_composite: 0.5468,
    token_penalty: 0.02,
    token_efficiency_multiplier: 0.98,
    maximum_token_penalty: null,
  };

  it("renders the inline equation with 'n/a' for a missing token multiplier", () => {
    expect(compositeEquationText(breakdown)).toBe("0.620 × 0.900 × 0.980 = 0.547");
    expect(compositeEquationText({ ...breakdown, token_efficiency_multiplier: null })).toBe(
      "0.620 × 0.900 × n/a = 0.547",
    );
    expect(compositeEquationText(null)).toBe("");
  });

  it("separates the quality gates from the bounded token adjustment", () => {
    const rows = compositeCalculationRows({
      tool_mean: 0.7,
      memory_mean: 0.54,
      composite_breakdown: breakdown,
      token_efficiency: {
        observed_total_tokens: 120000,
        baseline_total_tokens: 100000,
        budget_percentile: 0.95,
      },
    });
    expect(rows).not.toBeNull();
    const byKey = Object.fromEntries((rows ?? []).map((row) => [row.k, row.v]));
    // Row 37 mirror: the base IS the ½·tool + ½·memory average.
    expect(byKey["Tool/memory base"]).toBe("0.5 × 0.700 + 0.5 × 0.540 = 0.620");
    expect(byKey["Benchmark quality gates"]).toBe("× 0.900 (−10.0%)");
    expect(byKey["Pre-token composite"]).toBe("0.558");
    // A missing maximum_token_penalty reads as the 10% contract bound.
    expect(byKey["Token efficiency"]).toBe("× 0.980 (−2.0%; max 10%)");
    expect(byKey["Final composite"]).toBe("0.547");
    expect(byKey["Observed token use"]).toBe(
      (120000).toLocaleString() + " / " + (100000).toLocaleString() + " p95 baseline",
    );
  });

  it("shows the post-continual efficiency fold as separate ranking provenance", () => {
    const rows = compositeCalculationRows({
      tool_mean: 0.8,
      memory_mean: 0.6,
      bench_version: 7,
      aggregate_method: "continual_mean",
      composite: 0.7,
      official_composite: 0.756,
      effective_composite: 0.756,
      pre_efficiency_composite: 0.72,
      efficiency_bonus: 0.05,
      efficiency_fold_applied: true,
      composite_breakdown: { ...breakdown, final_composite: 0.7 },
    });
    const byKey = Object.fromEntries((rows ?? []).map((row) => [row.k, row.v]));
    expect(byKey["Initial quorum signed composite"]).toBe("0.700");
    expect(byKey["Continual aggregate"]).toBe("0.720");
    expect(byKey["Relative token-efficiency bonus"]).toBe("+5.0% · frozen cohort award");
    expect(byKey["Folded ranking score"]).toBe("0.756 · used for rank, KOTH, and emissions");
    expect(byKey["Token efficiency"]).toBeUndefined();
    expect(compositeCalculationHeading({ pre_efficiency_composite: 0.72 })).toBe(
      "Score provenance and ranking fold",
    );
  });

  it.each([
    [0.85, 0.68, "× 0.850 (−15.0% · frozen cohort P25 reference)"],
    [1.1, 0.82, "× 1.100 (+10.0% · frozen cohort P25 reference)"],
  ])("shows a bounded v9 factor of %s after authoritative quality", (factor, final, expected) => {
    const rows = compositeCalculationRows({
      tool_mean: 0.8,
      memory_mean: 0.8,
      bench_version: 9,
      composite: 0.8,
      official_composite: 0.8,
      effective_composite: final,
      pre_efficiency_composite: 0.8,
      efficiency_bonus: 0.05,
      efficiency_factor: factor,
      efficiency_fold_applied: true,
      composite_breakdown: { ...breakdown, final_composite: 0.8 },
    });
    const byKey = Object.fromEntries((rows ?? []).map((row) => [row.k, row.v]));

    expect(byKey["Bounded token-efficiency factor"]).toBe(expected);
    expect(byKey["Current quality score"]).toBe("0.800 · primary ranking key");
    expect(byKey["Efficiency tie-break"]).toBe(
      final.toFixed(3) + " · active only after exact quality equality",
    );
    expect(byKey["Folded ranking score"]).toBeUndefined();
    expect(byKey["Relative token-efficiency bonus"]).toBeUndefined();
  });

  it("shows the Bench v9 remaining-headroom transform", () => {
    const input = {
      tool_mean: 0.95,
      memory_mean: 0.95,
      bench_version: 9,
      composite: 0.95,
      official_composite: 0.95,
      effective_composite: 0.955,
      pre_efficiency_composite: 0.95,
      efficiency_factor: 1.1,
      efficiency_fold_applied: true,
      composite_breakdown: { ...breakdown, final_composite: 0.95 },
    };

    expect(curveV3ScoreAdjustment(input)).toEqual({
      quality: 0.95,
      factor: 1.1,
      adjusted: 0.955,
      mode: "headroom",
    });
    const byKey = Object.fromEntries(
      (compositeCalculationRows(input) ?? []).map((row) => [row.k, row.v]),
    );
    expect(byKey["Bench v9+ efficiency transform"]).toBe(
      "0.950 + (1.100 − 1) × (1 − 0.950) = 0.955",
    );
    expect(byKey["Current quality score"]).toBe("0.950 · primary ranking key");
    expect(byKey["Efficiency tie-break"]).toBe("0.955 · active only after exact quality equality");
  });

  it("shows the curve-v4 asymptotic remaining-headroom transform", () => {
    const input = {
      tool_mean: 0.997012,
      memory_mean: 0.997012,
      bench_version: 10,
      composite: 0.997012,
      official_composite: 0.997012,
      effective_composite: 0.998008,
      pre_efficiency_composite: 0.997012,
      efficiency_factor: 1.5,
      efficiency_curve_version: 4,
      efficiency_fold_applied: true,
      composite_breakdown: { ...breakdown, final_composite: 0.997012 },
    };

    expect(curveV3ScoreAdjustment(input)).toEqual({
      quality: 0.997012,
      factor: 1.5,
      adjusted: 0.997012 + (1 - 0.997012) * (1 - 1 / 1.5),
      mode: "headroom",
    });
    const byKey = Object.fromEntries(
      (compositeCalculationRows(input) ?? []).map((row) => [row.k, row.v]),
    );
    expect(byKey["Bench v9+ efficiency transform"]).toContain("1 − 1 / 1.500");
    expect(byKey["Current quality score"]).toBe("0.997 · primary ranking key");
    expect(byKey["Efficiency tie-break"]).toContain("active only after exact quality equality");
  });

  it("labels the tie-break by direction and magnitude, never by its raw value", () => {
    // A floored agent's tie-break value (0.847 beside quality 0.997) is the
    // exact pairing that reads as a contradiction on the board.
    expect(efficiencyTieBreakChipLabel({ efficiency_factor: 0.85 }, { applied: true })).toEqual({
      label: "efficiency tie-break ▼ 15.0% (floor)",
      direction: "down",
      atBound: true,
    });
    expect(efficiencyTieBreakChipLabel({ efficiency_factor: 1.1 }, { applied: true })).toEqual({
      label: "efficiency tie-break ▲ 10.0% (cap)",
      direction: "up",
      atBound: true,
    });
    // Off the bounds the chip drops the qualifier but keeps the direction.
    expect(
      efficiencyTieBreakChipLabel({ efficiency_factor: 1.0182248358524693 }, { applied: true })
        ?.label,
    ).toBe("efficiency tie-break ▲ 1.8%");
    // An unapplied factor stays marked as a preview.
    expect(
      efficiencyTieBreakChipLabel({ efficiency_factor: 0.85 }, { applied: false })?.label,
    ).toBe("efficiency tie-break preview ▼ 15.0% (floor)");
  });

  it("reads a sub-precision factor as neutral rather than asserting a direction", () => {
    expect(efficiencyTieBreakChipLabel({ efficiency_factor: 1 }, { applied: true })).toEqual({
      label: "efficiency tie-break · neutral",
      direction: "neutral",
      atBound: false,
    });
    expect(
      efficiencyTieBreakChipLabel({ efficiency_factor: 1.0004 }, { applied: true })?.direction,
    ).toBe("neutral");
  });

  it("does not label an unclamped v4 factor as a v3 floor or cap", () => {
    expect(
      efficiencyTieBreakChipLabel(
        { efficiency_factor: 0.84, efficiency_curve_version: 4 },
        { applied: true },
      ),
    ).toEqual({
      label: "efficiency tie-break ▼ 16.0%",
      direction: "down",
      atBound: false,
    });
    expect(
      efficiencyTieBreakChipLabel(
        { efficiency_factor: 1.5, efficiency_curve_version: 4 },
        { applied: true },
      )?.label,
    ).toBe("efficiency tie-break ▲ 50.0%");
    expect(
      efficiencyTieBreakChipLabel({ efficiency_factor: 0.85 }, { applied: true })?.atBound,
    ).toBe(true);
  });

  it("has no tie-break chip without a curve-v3 factor", () => {
    expect(efficiencyTieBreakChipLabel({}, { applied: true })).toBeNull();
    expect(efficiencyTieBreakChipLabel({ efficiency_factor: null }, { applied: true })).toBeNull();
    expect(efficiencyTieBreakChipLabel({ efficiency_factor: NaN }, { applied: true })).toBeNull();
  });

  it("does not apply the Bench v9 transform to a historical v1/v2 bonus", () => {
    const input = {
      tool_mean: 0.95,
      memory_mean: 0.95,
      bench_version: 8,
      composite: 0.95,
      official_composite: 1.045,
      effective_composite: 1.045,
      pre_efficiency_composite: 0.95,
      efficiency_bonus: 0.1,
      efficiency_fold_applied: true,
      composite_breakdown: { ...breakdown, final_composite: 0.95 },
    };

    const byKey = Object.fromEntries(
      (compositeCalculationRows(input) ?? []).map((row) => [row.k, row.v]),
    );
    expect(curveV3ScoreAdjustment(input)).toBeNull();
    expect(byKey["Bench v9+ efficiency transform"]).toBeUndefined();
    expect(byKey["Folded ranking score"]).toBe("1.045 · used for rank, KOTH, and emissions");
  });

  it("keeps a neutral factor audit-only until the explicit fold flag is true", () => {
    const input = {
      tool_mean: 0.8,
      memory_mean: 0.8,
      bench_version: 9,
      composite: 0.8,
      official_composite: 0.8,
      effective_composite: 0.8,
      pre_efficiency_composite: 0.8,
      efficiency_factor: 1,
      efficiency_fold_applied: false,
      composite_breakdown: { ...breakdown, final_composite: 0.8 },
    };

    const rows = compositeCalculationRows(input);
    const byKey = Object.fromEntries((rows ?? []).map((row) => [row.k, row.v]));
    expect(byKey["Efficiency projection"]).toContain("audit only");
    expect(byKey["Folded ranking score"]).toBeUndefined();
    expect(compositeCalculationHeading(input)).toBe("Score provenance and efficiency projection");
  });

  it.each([
    {
      name: "bounded factor",
      adjustment: { efficiency_factor: 0.85, efficiency_bonus: 0.05 },
      row: "Bounded token-efficiency factor",
      expected: "× 0.850 (−15.0% · frozen cohort P25 reference)",
      effective: 0.68,
    },
    {
      name: "legacy bonus",
      adjustment: { efficiency_bonus: 0.05 },
      row: "Relative token-efficiency bonus",
      expected: "+5.0% · frozen cohort award",
      effective: 0.84,
    },
  ])("labels an inactive $name as an audit-only projection", (example) => {
    const input = {
      tool_mean: 0.8,
      memory_mean: 0.8,
      bench_version: 9,
      composite: 0.8,
      official_composite: 0.8,
      effective_composite: example.effective,
      pre_efficiency_composite: 0.8,
      composite_breakdown: { ...breakdown, final_composite: 0.8 },
      ...example.adjustment,
    };
    const rows = compositeCalculationRows(input);
    const byKey = Object.fromEntries((rows ?? []).map((row) => [row.k, row.v]));

    expect(byKey[example.row]).toBe(example.expected);
    expect(byKey["Efficiency projection"]).toBe(
      example.effective.toFixed(3) + " · audit only; not used for rank, KOTH, or emissions",
    );
    expect(byKey["Current ranking score"]).toBe("0.800 · used for rank, KOTH, and emissions");
    expect(byKey["Folded ranking score"]).toBeUndefined();
    expect(compositeCalculationHeading(input)).toBe("Score provenance and efficiency projection");
  });

  it("says 'not applied or unavailable' when the token multiplier is absent", () => {
    const rows = compositeCalculationRows({
      tool_mean: 0.7,
      memory_mean: 0.54,
      composite_breakdown: { ...breakdown, token_efficiency_multiplier: null },
    });
    const tokenRow = rows?.find((row) => row.k === "Token efficiency");
    expect(tokenRow?.v).toBe("not applied or unavailable");
    // No token-efficiency payload → no observed-use row.
    expect(rows?.some((row) => row.k === "Observed token use")).toBe(false);
  });

  it("returns null without a breakdown (the block does not render)", () => {
    expect(compositeCalculationRows({ tool_mean: 0.7, memory_mean: 0.5 })).toBeNull();
  });

  it("carries the block heading and bounded-adjustment note verbatim", () => {
    expect(COMPOSITE_CALC_HEADING).toBe("Composite calculation");
    expect(COMPOSITE_CALC_NOTE).toContain("Bench v7/v8 relative-efficiency awards are upside");
    expect(COMPOSITE_CALC_NOTE).toContain("Bench v9+ uses a bounded factor");
    expect(COMPOSITE_CALC_NOTE).toContain("Downside multiplies quality");
    expect(COMPOSITE_CALC_NOTE).toContain("imperfect quality cannot become 1.000");
    expect(COMPOSITE_CALC_NOTE).toContain("audit-only projection is not used");
  });

  it("maxTokenPenaltyPct defaults to 10% only when the API omits the bound", () => {
    expect(maxTokenPenaltyPct(null)).toBe(10);
    expect(maxTokenPenaltyPct(undefined)).toBe(10);
    expect(maxTokenPenaltyPct(0.15)).toBeCloseTo(15, 12);
  });
});

describe("embargoHours", () => {
  it("defaults the source-release embargo to 48 hours", () => {
    expect(embargoHours({})).toBe(48);
    expect(embargoHours(null)).toBe(48);
    expect(embargoHours({ embargo_hours: 0 })).toBe(48); // Number(0) || 48
    expect(embargoHours({ embargo_hours: 72 })).toBe(72);
  });
});

describe("efficiencyBoardStatus", () => {
  const cohort = {
    n_min: 8,
    reference_p25_tokens: 1_800_000,
    minimum_factor: 0.85,
    maximum_factor: 1.1,
  };

  it("renders nothing for a board the adjustment cannot apply to", () => {
    expect(efficiencyBoardStatus(null)).toBeNull();
  });

  it("separates an empty cohort from a switched-off adjustment", () => {
    // The production shape on 2026-08-13: preview computed, nothing qualified.
    // Both render blank per-row, so the distinction has to be stated here.
    const status = efficiencyBoardStatus({
      ...cohort,
      active: false,
      preview: true,
      cohort_size: 0,
      reference_p25_tokens: null,
    });
    expect(status?.tone).toBe("dormant");
    expect(status?.headline).toContain("not adjusting any score");
    expect(status?.detail).toContain("No agent has qualified");
    expect(status?.cohortSize).toBe(0);
    expect(status?.referenceTokens).toBeNull();
  });

  it("reports a partial cohort as dormant and names the shortfall", () => {
    const status = efficiencyBoardStatus({ ...cohort, active: true, cohort_size: 5 });
    expect(status?.tone).toBe("dormant");
    expect(status?.detail).toContain("5 of the 8 agents required");
  });

  it("marks a qualified cohort as projection while the fold is off", () => {
    const status = efficiencyBoardStatus({
      ...cohort,
      active: false,
      preview: true,
      cohort_size: 12,
    });
    expect(status?.tone).toBe("projected");
    expect(status?.detail).toContain("Nothing here moves a rank");
    expect(status?.referenceTokens).toBe(1_800_000);
  });

  it("marks an active cohort as ranking the board", () => {
    const status = efficiencyBoardStatus({ ...cohort, active: true, cohort_size: 12 });
    expect(status?.tone).toBe("applied");
    expect(status?.headline).toBe("Token efficiency is active as the quality tie-break");
    expect(status?.detail).toContain("lower-quality agent never passes a higher-quality agent");
    expect(status?.minimumFactor).toBe(0.85);
    expect(status?.maximumFactor).toBe(1.1);
  });
});

describe("epochCountdown", () => {
  // SN118 as observed on 2026-08-21: a 360-block tempo (~72 min) whose next
  // tick was 15 blocks (180s) out at 20:05:12Z.
  const epoch = {
    tempo_blocks: 360,
    block_seconds: 12,
    epoch_seconds: 4320,
    last_epoch_block: 8_895_229,
    next_epoch_block: 8_895_589,
    blocks_since_last_epoch: 345,
    blocks_until_next_epoch: 15,
    next_epoch_at: "2026-08-21T20:08:12Z",
    commit_reveal_enabled: true,
    reveal_period_epochs: 1,
    weights_rate_limit_blocks: 100,
  };
  const at = (iso: string): number => Date.parse(iso);

  it("counts down to the served tick", () => {
    const countdown = epochCountdown(epoch, at("2026-08-21T20:05:12Z"));
    expect(countdown?.secondsRemaining).toBe(180);
    expect(countdown?.nextEpochBlock).toBe(8_895_589);
    expect(countdown?.projected).toBe(false);
    expect(countdown?.commitRevealEnabled).toBe(true);
    expect(countdown?.revealPeriodEpochs).toBe(1);
  });

  it("rolls a spent target forward a whole epoch instead of pinning zero", () => {
    // A cached snapshot can outlive the tick it named. Clamping to zero would
    // claim a payout is perpetually imminent; the cycle is fixed, so the next
    // tick is exactly one epoch (and one tempo of blocks) later.
    const countdown = epochCountdown(epoch, at("2026-08-21T20:10:12Z"));
    expect(countdown?.secondsRemaining).toBe(4200);
    expect(countdown?.nextEpochBlock).toBe(8_895_949);
    expect(countdown?.projected).toBe(true);
  });

  it("rolls forward by as many whole epochs as have elapsed", () => {
    const countdown = epochCountdown(epoch, at("2026-08-21T22:00:12Z"));
    expect(countdown?.targetMs).toBe(at("2026-08-21T22:32:12Z"));
    expect(countdown?.nextEpochBlock).toBe(8_896_309);
    expect(countdown?.projected).toBe(true);
  });

  it("does not roll forward at the exact tick", () => {
    const countdown = epochCountdown(epoch, at("2026-08-21T20:08:12Z"));
    expect(countdown?.secondsRemaining).toBe(0);
    expect(countdown?.projected).toBe(false);
  });

  it("states absence rather than inventing a countdown", () => {
    expect(epochCountdown(null, at("2026-08-21T20:05:12Z"))).toBeNull();
    expect(epochCountdown({ tempo_blocks: 360 }, at("2026-08-21T20:05:12Z"))).toBeNull();
    expect(
      epochCountdown({ ...epoch, tempo_blocks: 0, epoch_seconds: 0 }, at("2026-08-21T20:05:12Z")),
    ).toBeNull();
    expect(
      epochCountdown({ ...epoch, next_epoch_at: "not a date" }, at("2026-08-21T20:05:12Z")),
    ).toBeNull();
  });
});

describe("countdownClock", () => {
  it("renders a clock, padded, and never negative", () => {
    expect(countdownClock(0)).toBe("0:00");
    expect(countdownClock(9)).toBe("0:09");
    expect(countdownClock(180)).toBe("3:00");
    expect(countdownClock(4200)).toBe("1:10:00");
    expect(countdownClock(-5)).toBe("0:00");
  });
});
