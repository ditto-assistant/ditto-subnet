import { describe, expect, it } from "vitest";

import { minerBenchChip } from "../EntityPanel";

type Entry = Parameters<typeof minerBenchChip>[0];

function entry(benchVersion: number | null): Entry {
  return { bench_version: benchVersion, score_count: 3, rank: 1 } as unknown as Entry;
}

describe("minerBenchChip", () => {
  it("says a run scored ahead of the paying version is not paying yet", () => {
    const chip = minerBenchChip(entry(13), 12, 12);
    expect(chip.text).toBe("DittoBench v13 · not paying yet");
    expect(chip.title).toContain("still being collected");
    expect(chip.title).toContain("Emissions stay settled on v12");
    expect(chip.title).toContain("until that rollout activates");
  });

  it("leaves a run on the paying version unqualified", () => {
    const chip = minerBenchChip(entry(12), 12, 12);
    expect(chip.text).toBe("DittoBench v12");
    expect(chip.title).toBe("Scored on DittoBench v12.");
  });

  it("keeps marking an older run as old rather than as unpaid", () => {
    const chip = minerBenchChip(entry(11), 12, 12);
    expect(chip.text).toBe("DittoBench v11 · old");
    expect(chip.class).toBe("prev");
    expect(chip.title).toContain("a previous benchmark");
    expect(chip.title).not.toContain("not paying");
  });

  it("never names a historical board pin as the emission authority", () => {
    // The board is pinned to v11 while the ledger pays v12. The old label may
    // compare against the pinned view, but nothing may claim v11 pays.
    const chip = minerBenchChip(entry(13), 11, 12);
    expect(chip.text).toBe("DittoBench v13 · not paying yet");
    expect(chip.title).toContain("Emissions stay settled on v12");
    expect(chip.title).not.toContain("v11");
  });

  it("stays quiet about emissions when the paying version is unknown", () => {
    expect(minerBenchChip(entry(13), 12, null).text).toBe("DittoBench v13");
    expect(minerBenchChip(entry(13), null, null).text).toBe("DittoBench v13");
  });
});
