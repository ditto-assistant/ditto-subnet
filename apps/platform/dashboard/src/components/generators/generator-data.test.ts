import { describe, expect, it } from "vitest";

import { loadFixture } from "../../test-fixtures";
import type {
  GeneratorBoard,
  GeneratorBoardEntry,
  GeneratorRoundDetail,
  GeneratorRoundList,
} from "./generator-data";
import {
  columnCases,
  cooldownText,
  generatorEmissionShare,
  generatorLabel,
  isSubmissionId,
  latestScoredRound,
  newestScoredIndex,
  nextRoundEta,
  nextRoundIndex,
  payoutFor,
  projectGeneratorPayouts,
  taoAmount,
} from "./generator-data";

const board = loadFixture<GeneratorBoard>("generator-board");
const rounds = loadFixture<GeneratorRoundList>("generator-rounds");
const round = loadFixture<GeneratorRoundDetail>("generator-round-25967");

function entry(over: Partial<GeneratorBoardEntry>): GeneratorBoardEntry {
  return {
    miner_hotkey: "hk",
    submission_id: "00000000-0000-4000-8000-000000000000",
    round_index: 1,
    generator_contract_version: 1,
    weight_eligible: true,
    score: 0.1,
    discriminating_cases: 1,
    first_seen: "2026-10-01T00:00:00Z",
    scored_at: "2026-10-02T00:00:00Z",
    ...over,
  };
}

describe("projectGeneratorPayouts", () => {
  it("pays the five rank shares on the live board and leaves the sixth unpaid", () => {
    const rows = projectGeneratorPayouts(board.entries);
    expect(rows.map((row) => row.position)).toEqual([1, 2, 3, 4, 5, null]);
    expect(rows[0]?.entry.miner_hotkey).toBe("5CXEMm6u6onoMpFPbpSqFLdeDzkG68nyTirSg3SVQfiMVhJa");
    expect(rows[5]?.unpaidReason).toBe("beyond_paid_ranks");
    const shares = rows.map((row) => row.trackShare);
    expect(shares.slice(0, 5)).toEqual(
      [0.4, 0.25, 0.15, 0.12, 0.08].map((v) => expect.closeTo(v, 12)),
    );
    expect(shares.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
  });

  it("rescales the paid shares when fewer than five generators are payable", () => {
    const rows = projectGeneratorPayouts(board.entries.slice(0, 4));
    expect(rows.map((row) => row.position)).toEqual([1, 2, 3, 4]);
    expect(rows[0]?.trackShare).toBeCloseTo(0.4 / 0.92, 12);
    expect(rows[3]?.trackShare).toBeCloseTo(0.12 / 0.92, 12);
  });

  it("breaks score ties on first seen, then submission id, like the validator", () => {
    const rows = projectGeneratorPayouts([
      entry({ miner_hotkey: "late", first_seen: "2026-10-03T00:00:00Z" }),
      entry({ miner_hotkey: "b", submission_id: "b" }),
      entry({ miner_hotkey: "a", submission_id: "a" }),
    ]);
    expect(rows.map((row) => row.entry.miner_hotkey)).toEqual(["a", "b", "late"]);
  });

  it("pays nothing for ineligible, zero, repeated or sixth-place entries", () => {
    const rows = projectGeneratorPayouts([
      entry({ miner_hotkey: "off", weight_eligible: false, score: 0.9 }),
      entry({ miner_hotkey: "zero", score: 0 }),
      entry({ miner_hotkey: "h1", score: 0.6, submission_id: "x" }),
      entry({ miner_hotkey: "h1", score: 0.5, submission_id: "y" }),
      entry({ miner_hotkey: "h2", score: 0.4 }),
      entry({ miner_hotkey: "h3", score: 0.3 }),
      entry({ miner_hotkey: "h4", score: 0.2 }),
      entry({ miner_hotkey: "h5", score: 0.15 }),
      entry({ miner_hotkey: "h6", score: 0.12 }),
    ]);
    const byKey = (key: string, sub?: string) =>
      rows.find(
        (row) => row.entry.miner_hotkey === key && (!sub || row.entry.submission_id === sub),
      );
    expect(byKey("off")?.unpaidReason).toBe("ineligible");
    expect(byKey("zero")?.unpaidReason).toBe("zero_score");
    expect(byKey("h1", "y")?.unpaidReason).toBe("duplicate_hotkey");
    expect(byKey("h6")?.unpaidReason).toBe("beyond_paid_ranks");
    expect(byKey("h1", "x")?.trackShare).toBeCloseTo(0.4, 12);
    expect(byKey("h5")?.trackShare).toBeCloseTo(0.08, 12);
  });

  it("reports no payable generator as an all-unpaid board", () => {
    const rows = projectGeneratorPayouts([entry({ score: 0 })]);
    expect(rows.every((row) => row.position === null)).toBe(true);
  });
});

describe("nextRoundEta", () => {
  const epoch = { next_epoch_at: "2026-10-26T21:00:00Z", epoch_seconds: 4320 };

  it("is the next epoch boundary when the next round opens there", () => {
    expect(nextRoundEta(rounds.schedule, epoch)?.toISOString()).toBe("2026-10-26T21:00:00.000Z");
  });

  it("adds one epoch per whole epoch still between them", () => {
    const schedule = { ...rounds.schedule, next_round_epoch: 25970 };
    expect(nextRoundEta(schedule, epoch)?.toISOString()).toBe("2026-10-26T23:24:00.000Z");
  });

  it("is unknown when the schedule's opening epoch has already passed", () => {
    const stale = { ...rounds.schedule, next_round_epoch: 25963, subnet_epoch_index: 25963 };
    expect(nextRoundEta(stale, epoch)).toBeNull();
  });

  it("is unknown without a chain clock", () => {
    expect(nextRoundEta(rounds.schedule, null)).toBeNull();
    expect(nextRoundEta({ mode: "enforce", epochs_per_round: 1 }, epoch)).toBeNull();
  });
});

describe("round helpers", () => {
  it("counts every case bucket and finds the newest scored round", () => {
    expect(round.columns.map(columnCases)).toEqual([32, 32, 32, 32, 32, 32]);
    expect(latestScoredRound(rounds)?.round_index).toBe(25967);
    const ascending = { ...rounds, rounds: [...rounds.rounds].reverse() };
    expect(latestScoredRound(ascending)?.round_index).toBe(25967);
  });

  it("maps the next round's first epoch back to its round index", () => {
    expect(nextRoundIndex(rounds.schedule)).toBe(25968);
    expect(
      nextRoundIndex({ mode: "enforce", epochs_per_round: 4, next_round_epoch: 103_860 }),
    ).toBe(25965);
    expect(
      nextRoundIndex({
        mode: "enforce",
        epochs_per_round: 4,
        subnet_epoch_index: 103_860,
        next_round_epoch: 103_860,
      }),
    ).toBeNull();
    expect(
      nextRoundIndex({
        mode: "enforce",
        epochs_per_round: 4,
        current_round: 25964,
        next_round_epoch: 103_860,
      }),
    ).toBe(25965);
  });

  it("reads the generator share only once the split is published", () => {
    expect(
      generatorEmissionShare({ forecast_burn_share: 0.5, forecast_miner_share: 0.5 }),
    ).toBeNull();
    expect(
      generatorEmissionShare({
        forecast_burn_share: 0.5,
        forecast_miner_share: 0.5,
        emission_split: [
          { kind: "burn", id: null, share: 0.5, bps: 5000 },
          { kind: "track", id: "memory", share: 0.35, bps: 3500 },
          { kind: "track", id: "generator", share: 0.15, bps: 1500 },
        ],
      }),
    ).toBe(0.15);
  });

  it("formats fees and cooldowns exactly", () => {
    expect(taoAmount(50_000_000)).toBe("0.05 TAO");
    expect(taoAmount(2_000_000_000)).toBe("2 TAO");
    expect(taoAmount(null)).toBe("–");
    expect(cooldownText(3600)).toBe("1h");
    expect(cooldownText(90)).toBe("90s");
  });
});

describe("generator identity helpers", () => {
  it("labels a named generator by name and an unnamed one by short id", () => {
    const id = "7241781f-99d0-4d96-a908-ac1281cca36f";
    expect(generatorLabel({ submission_id: id, name: "  Needle Forge " })).toBe("Needle Forge");
    expect(generatorLabel({ submission_id: id, name: "   " })).toBe("Generator 7241781f");
    expect(generatorLabel({ submission_id: id })).toBe("Generator 7241781f");
  });

  it("accepts only canonical lowercase submission ids from the URL", () => {
    expect(isSubmissionId("7241781f-99d0-4d96-a908-ac1281cca36f")).toBe(true);
    expect(isSubmissionId("7241781F-99D0-4D96-A908-AC1281CCA36F")).toBe(false);
    expect(isSubmissionId("../etc/passwd")).toBe(false);
    expect(isSubmissionId(null)).toBe(false);
  });

  it("finds the newest scored round from either the list or the board", () => {
    const rounds = loadFixture<GeneratorRoundList>("generator-rounds");
    const board = loadFixture<GeneratorBoard>("generator-board");
    expect(newestScoredIndex(rounds, null)).toBe(latestScoredRound(rounds)?.round_index);
    expect(newestScoredIndex(null, board)).toBe(25967);
    expect(newestScoredIndex(null, null)).toBeNull();
    const ahead = structuredClone(board);
    (ahead.entries[0] as GeneratorBoardEntry).round_index = 99_999;
    expect(newestScoredIndex(rounds, ahead)).toBe(99_999);
  });

  it("looks up a submission's payout row", () => {
    const board = loadFixture<GeneratorBoard>("generator-board");
    const rows = projectGeneratorPayouts(board.entries);
    const top = rows[0];
    expect(payoutFor(rows, top?.entry.submission_id ?? "")).toBe(top);
    expect(payoutFor(rows, "00000000-0000-4000-8000-000000000000")).toBeNull();
  });
});
