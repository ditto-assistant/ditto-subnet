import { describe, expect, it } from "vitest";

import {
  BODIES,
  EYEWEAR,
  EXTRAS,
  HAIRSTYLES,
  HEADWEAR,
  HUMAN_PROPS,
  SKINS,
  TONES,
  agentPersona,
  describeDitto,
  hashSeed,
  humanPersona,
  personaKey,
  seededRandom,
} from "./persona";

const IDS = Array.from(
  { length: 400 },
  (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
);

describe("persona picker", () => {
  it("hashes text stably (FNV-1a)", () => {
    expect(hashSeed("")).toBe(0x811c9dc5);
    expect(hashSeed("a")).toBe(0xe40c292c);
    expect(hashSeed("ditto")).toBe(hashSeed("ditto"));
    expect(hashSeed("ditto")).not.toBe(hashSeed("Ditto"));
  });

  it("draws a reproducible sequence in [0, 1)", () => {
    const a = seededRandom(42);
    const b = seededRandom(42);
    const first = Array.from({ length: 5 }, () => a());
    expect(Array.from({ length: 5 }, () => b())).toEqual(first);
    for (const value of first) {
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThan(1);
    }
  });

  it("gives an agent the same Paper Ditto every time", () => {
    const id = "c77140d8-3ac5-42c3-8f5c-a99cd283365d";
    expect(agentPersona(id)).toEqual(agentPersona(id));
    expect(humanPersona(id)).toEqual(humanPersona(id));
  });

  it("keys a persona on the submission id, falling back to its name", () => {
    expect(personaKey("abc", "vesper")).toBe("abc");
    expect(personaKey(null, "vesper")).toBe("vesper");
    expect(personaKey(undefined, null)).toBe("anonymous");
  });

  it("only picks from the Paper catalogue", () => {
    for (const id of IDS) {
      const ditto = agentPersona(id);
      expect(ditto.body).toBeGreaterThanOrEqual(0);
      expect(ditto.body).toBeLessThan(BODIES.length);
      expect(TONES).toContain(ditto.tone);
      expect(ditto.eyewear).toBeLessThan(EYEWEAR.length);
      expect(ditto.headwear).toBeLessThan(HEADWEAR.length);
      expect(ditto.extra).toBeLessThan(EXTRAS.length);
      const human = humanPersona(id);
      expect(human.skin).toBeLessThan(SKINS.length);
      expect(human.hair).toBeLessThan(HAIRSTYLES.length);
      expect(human.prop).toBeLessThan(HUMAN_PROPS.length);
      expect(TONES).toContain(human.shirt);
    }
  });

  it("spreads agents across every body, tint and skin", () => {
    const bodies = new Set(IDS.map((id) => agentPersona(id).body));
    const tones = new Set(IDS.map((id) => agentPersona(id).tone));
    const skins = new Set(IDS.map((id) => humanPersona(id).skin));
    expect(bodies.size).toBe(BODIES.length);
    expect(tones.size).toBe(TONES.length);
    expect(skins.size).toBe(SKINS.length);
    // Accessories stay quiet: a real share of agents wears none.
    const bare = IDS.filter((id) => agentPersona(id).headwear === 0).length;
    expect(bare).toBeGreaterThan(IDS.length * 0.3);
    expect(bare).toBeLessThan(IDS.length * 0.65);
  });

  it("picks a generator's person independently of an agent with the same id", () => {
    const id = IDS[7]!;
    expect(agentPersona(id).seed).not.toBe(humanPersona(id).seed);
  });

  it("describes a persona in words", () => {
    expect(
      describeDitto({ seed: 1, body: 1, tone: "tide", eyewear: 2, headwear: 6, extra: 0 }),
    ).toBe("tide turned corner Ditto, crown, round frames");
  });
});
