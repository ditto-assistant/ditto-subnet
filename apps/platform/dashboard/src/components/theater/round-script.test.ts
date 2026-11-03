import { describe, expect, it } from "vitest";

import { loadFixture } from "../../test-fixtures";
import type { GeneratorRoundDetail } from "../generators/generator-data";
import {
  BEAT_MS,
  agentMood,
  arenaLayout,
  catchPoint,
  flightFor,
  handPoint,
  headPoint,
  restingPoint,
  tableauThrow,
  throwPhases,
} from "./arena-motion";
import { agentPersona, humanPersona } from "./persona";
import type { ArenaThrow } from "./round-script";
import {
  OUTCOMES,
  apportion,
  arenaGenerators,
  buildRoundScript,
  roundSummary,
  smoothSequence,
} from "./round-script";

// The recorded round 25967: five counted generators (a sixth column is an
// uncounted challenger), a five-agent panel, no duplicates.
const round = (): GeneratorRoundDetail =>
  loadFixture<GeneratorRoundDetail>("generator-round-25967");

/** The same round with a public default generator that wrote duplicates. */
function withDefault(): GeneratorRoundDetail {
  const detail = round();
  detail.columns.push({
    column_id: "c6",
    submission_id: "72b1ed56-f86a-4076-b61e-a4c6179cfd73",
    miner_hotkey: null,
    counted: true,
    public_default: true,
    generated: true,
    score: 0,
    discriminating: 0,
    trivial: 29,
    unsolved: 0,
    duplicate: 3,
  });
  return detail;
}

describe("apportion and smoothSequence", () => {
  it("splits slots by largest remainder and sums exactly", () => {
    expect(apportion([26, 3, 3, 0], 10)).toEqual([8, 1, 1, 0]);
    expect(apportion([1, 1, 1], 2)).toEqual([1, 1, 0]);
    expect(apportion([0, 0], 5)).toEqual([0, 0]);
    expect(apportion([5, 5], 0)).toEqual([0, 0]);
    const split = apportion([32, 32, 32, 32, 32], 24);
    expect(split.reduce((a, b) => a + b, 0)).toBe(24);
  });

  it("spreads each bucket evenly through the sequence", () => {
    expect(smoothSequence([2, 1])).toEqual([0, 1, 0]);
    expect(smoothSequence([0, 3])).toEqual([1, 1, 1]);
    const sequence = smoothSequence([4, 4]);
    expect(sequence.filter((b) => b === 0)).toHaveLength(4);
    // Never three of one bucket in a row when the quotas are equal.
    expect(sequence.join("")).not.toMatch(/000|111/);
  });
});

describe("round → arena script", () => {
  it("casts counted generators as Paper people and the panel as Paper Dittos", () => {
    const script = buildRoundScript(withDefault());
    expect(script.round).toBe(25967);
    // c5 is an uncounted challenger column: it never throws.
    expect(script.generators.map((g) => g.columnId)).toEqual(["c0", "c1", "c2", "c3", "c4", "c6"]);
    expect(script.generators[5]).toMatchObject({ name: "Public default", isDefault: true });
    expect(script.generators[0]!.persona).toEqual(
      humanPersona("7241781f-99d0-4d96-a908-ac1281cca36f"),
    );
    expect(script.agents.map((a) => a.rank)).toEqual([1, 2, 3, 4, 5]);
    expect(script.agents[0]!.persona).toEqual(agentPersona("aa8019f0-7648-4479-8179-7bc48e44d616"));
    expect(script.agents[0]!.name).toBe("Agent aa8019f0");
  });

  it("names agents from the leaderboard when it knows them", () => {
    const script = buildRoundScript(round(), (id) =>
      id.startsWith("aa8019f0") ? { name: "vesper", avatarUrl: null } : null,
    );
    expect(script.agents[0]!.name).toBe("vesper");
  });

  it("keeps the real totals and samples throws in proportion", () => {
    const script = buildRoundScript(round(), () => null, { maxThrows: 24 });
    expect(script.totals).toEqual({
      discriminating: 49,
      trivial: 87,
      unsolved: 24,
      duplicate: 0,
      cases: 160,
    });
    expect(script.throws).toHaveLength(24);
    // Every generator throws in proportion to the cases it wrote (32 each
    // here), so the 24 throws split evenly between them.
    const per = script.generators.map(
      (_, i) => script.throws.filter((t) => t.generator === i).length,
    );
    expect(per.reduce((a, b) => a + b, 0)).toBe(24);
    expect(Math.max(...per) - Math.min(...per)).toBeLessThanOrEqual(1);
    // c0 wrote 24 of 32 split cases: most of its throws are labyrinths.
    const c0 = script.throws.filter((t) => t.generator === 0);
    expect(c0.filter((t) => t.outcome === "discriminating").length).toBeGreaterThan(c0.length / 2);
    // c3 wrote 30 easy cases and 2 split ones: it lobs easy ones.
    const c3 = script.throws.filter((t) => t.generator === 3);
    expect(c3.every((t) => t.outcome === "trivial")).toBe(true);
    // The public default wrote only easy cases and duplicates.
    const fallback = buildRoundScript(withDefault(), () => null, { maxThrows: 192 });
    const c6 = fallback.throws.filter((t) => t.generator === 5);
    expect(c6.every((t) => t.outcome === "trivial" || t.outcome === "duplicate")).toBe(true);
    expect(c6.filter((t) => t.outcome === "duplicate")).toHaveLength(3);
  });

  it("is deterministic for a round", () => {
    expect(buildRoundScript(round())).toEqual(buildRoundScript(round()));
  });

  it("maps every outcome to its catch and bonk rule", () => {
    const script = buildRoundScript(withDefault(), () => null, { maxThrows: 192 });
    expect(new Set(script.throws.map((t) => t.outcome))).toEqual(new Set(OUTCOMES));
    for (const shot of script.throws) {
      if (shot.outcome === "trivial") {
        expect(shot.catchers).toHaveLength(1);
        expect(shot.missers).toHaveLength(0);
        expect(shot.prop).toBe("sticky");
      } else if (shot.outcome === "unsolved") {
        expect(shot.catchers).toHaveLength(0);
        expect(shot.missers).toHaveLength(1);
        expect(shot.prop).toBe("knot");
      } else if (shot.outcome === "duplicate") {
        expect(shot.catchers).toHaveLength(0);
        expect(shot.missers).toHaveLength(0);
        expect(shot.prop).toBe("copy");
      } else {
        expect(shot.catchers).toHaveLength(1);
        expect(shot.missers).toHaveLength(1);
        expect(shot.catchers[0]).not.toBe(shot.missers[0]);
        expect(shot.prop).toBe("maze");
      }
    }
  });

  it("lets stronger panel agents catch more of the split cases", () => {
    const script = buildRoundScript(round(), () => null, { maxThrows: 160 });
    const split = script.throws.filter((t) => t.outcome === "discriminating");
    const catches = script.agents.map((_, i) => split.filter((t) => t.catchers[0] === i).length);
    const bonks = script.agents.map((_, i) => split.filter((t) => t.missers[0] === i).length);
    expect(catches[0]!).toBeGreaterThan(catches[4]!);
    expect(bonks[4]!).toBeGreaterThan(bonks[0]!);
  });

  it("captions each throw and summarizes the round, stating the dramatization", () => {
    const script = buildRoundScript(round());
    expect(script.throws[0]!.caption.length).toBeGreaterThan(10);
    const summary = roundSummary(script);
    expect(summary).toContain("Generator round 25967: 5 generators produced 160 cases");
    expect(summary).toContain("49 separated the agents");
    expect(summary).toContain("Per-agent results on individual cases are not public");
  });

  it("skips uncounted columns and survives an empty round", () => {
    const detail = round();
    expect(arenaGenerators(detail.columns).map((g) => g.columnId)).not.toContain("c5");
    const empty = buildRoundScript({ ...detail, standings: [] });
    expect(empty.throws).toEqual([]);
  });
});

describe("arena choreography", () => {
  const shot = (over: Partial<ArenaThrow>): ArenaThrow => ({
    index: 0,
    generator: 0,
    outcome: "discriminating",
    prop: "maze",
    catchers: [0],
    missers: [3],
    caption: "",
    ...over,
  });

  it("times a split case in two landings and others in one", () => {
    expect(throwPhases("discriminating").map((p) => p.phase)).toEqual([
      "windup",
      "flight",
      "first",
      "second",
      "rest",
    ]);
    expect(throwPhases("trivial").map((p) => p.phase)).toEqual([
      "windup",
      "flight",
      "first",
      "rest",
    ]);
    expect(BEAT_MS.next).toBeGreaterThan(BEAT_MS.rest);
  });

  it("reacts: the misser is bonked first, the catcher cheers after the bounce", () => {
    const split = shot({});
    expect(agentMood(split, 0, "flight")).toBe("focus");
    expect(agentMood(split, 3, "first")).toBe("bonk");
    expect(agentMood(split, 0, "first")).toBe("focus");
    expect(agentMood(split, 0, "second")).toBe("cheer");
    expect(agentMood(split, 0, "rest")).toBe("cheer");
    expect(agentMood(split, 1, "second")).toBe("idle");
    const easy = shot({ outcome: "trivial", prop: "sticky", catchers: [2], missers: [] });
    expect(agentMood(easy, 2, "first")).toBe("cheer");
    const stumper = shot({ outcome: "unsolved", prop: "knot", catchers: [], missers: [1] });
    expect(agentMood(stumper, 1, "first")).toBe("bonk");
    expect(agentMood(null, 0, "rest")).toBe("idle");
  });

  it("flies each case to the right place", () => {
    const layout = arenaLayout("wide", 5, 5);
    const split = flightFor(layout, shot({ generator: 1 }));
    expect(split.kind).toBe("double");
    expect(split.from).toEqual(handPoint(layout, 1));
    expect(split.to).toEqual(headPoint(layout, 3));
    expect(split.bounce).toEqual(catchPoint(layout, 0));
    expect(restingPoint(split)).toEqual(catchPoint(layout, 0));
    expect(split.peaks[0]!).toBeLessThan(Math.min(split.from.y, split.to.y));

    const stumper = flightFor(layout, shot({ outcome: "unsolved", catchers: [], missers: [2] }));
    expect(stumper).toMatchObject({ kind: "single", bounce: null });
    expect(stumper.to).toEqual(headPoint(layout, 2));

    const dupe = flightFor(layout, shot({ outcome: "duplicate", catchers: [], missers: [] }));
    expect(dupe.kind).toBe("dupe");
    expect(dupe.to.x).toBeGreaterThan(dupe.from.x);
    expect(dupe.to.x).toBeLessThan(headPoint(layout, 0).x);
  });

  it("lays out side by side when wide and top to bottom when tall", () => {
    const wide = arenaLayout("wide", 6, 5);
    expect(Math.max(...wide.generators.map((p) => p.x))).toBeLessThan(
      Math.min(...wide.agents.map((p) => p.x)),
    );
    const tall = arenaLayout("tall", 6, 5);
    expect(Math.max(...tall.generators.map((p) => p.y))).toBeLessThan(
      Math.min(...tall.agents.map((p) => p.y)),
    );
    for (const p of [...tall.generators, ...tall.agents]) {
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.x + Math.max(tall.agentSize, tall.generatorSize)).toBeLessThanOrEqual(tall.width);
    }
  });

  it("freezes the still tableau on the first split case", () => {
    const easy = shot({ index: 0, outcome: "trivial" });
    const split = shot({ index: 1 });
    expect(tableauThrow([easy, split])).toBe(split);
    expect(tableauThrow([easy])).toBe(easy);
    expect(tableauThrow([])).toBeNull();
  });
});
