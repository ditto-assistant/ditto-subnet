// Deterministic Paper personas for the pipeline theater.
//
// Every submitted agent becomes a Paper Ditto and every data generator a
// Paper person. The look is picked from a fixed catalogue by a seed derived
// from the submission's id, so the same agent wears the same body, tint and
// accessories on every visit, on every device, with nothing stored. The
// catalogue mirrors the Paper family from ditto-app (DITTO-1527; design
// draft ditto-assistant/ditto-app#3482,
// src/mockups/sets/agent-avatars/PaperCharacter.tsx): seven paper bodies,
// the brand-kit tints, and independent eyewear, headwear and extras.

/** Brand-kit tints the Paper family paints with (ditto-app `tones`). */
export const TONES = ["tide", "signal", "vermilion", "parchment", "carbon"] as const;
export type Tone = (typeof TONES)[number];

/** Paper bodies, index-aligned with ditto-app `bodyOptions`. */
export const BODIES = [
  "Paper",
  "Turned corner",
  "Bookmark",
  "Concertina",
  "Scroll",
  "Ticket",
  "Paper stack",
] as const;
export const EYEWEAR = ["None", "Studio frames", "Round frames", "Sunshades"] as const;
export const HEADWEAR = ["None", "Sprout", "Cap", "Beanie", "Bow", "Halo", "Crown"] as const;
export const EXTRAS = ["None", "Headphones", "Freckles", "Star pin"] as const;

export interface DittoPersona {
  seed: number;
  /** Index into BODIES; 0 is the classic chat-bubble Ditto. */
  body: number;
  tone: Tone;
  eyewear: number;
  headwear: number;
  extra: number;
}

/** Skin, hair and shirt for a Paper person: generators write humanlike
 * memory traces, so they are drawn as people in the same paper style. */
export const SKINS = [
  "oklch(91% 0.035 70)",
  "oklch(83% 0.06 60)",
  "oklch(72% 0.08 52)",
  "oklch(58% 0.085 45)",
  "oklch(45% 0.07 40)",
] as const;
export const HAIR_INKS = [
  "oklch(24% 0.02 50)",
  "oklch(40% 0.07 50)",
  "oklch(60% 0.13 42)",
  "oklch(84% 0.09 88)",
  "oklch(80% 0.008 270)",
] as const;
export const HAIRSTYLES = ["Crop", "Bob", "Bun", "Curls", "Spikes", "Beanie"] as const;
export const HUMAN_PROPS = ["Clipboard", "Headset", "Glasses", "Pencil"] as const;

export interface HumanPersona {
  seed: number;
  skin: number;
  hair: number;
  hairInk: number;
  shirt: Tone;
  prop: number;
}

/** FNV-1a over UTF-16 code units: small, stable and good enough to spread
 * UUIDs and names across a catalogue. */
export function hashSeed(text: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index++) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

/** mulberry32: a seeded generator so one seed yields several independent
 * picks without the correlation of slicing one hash into bit fields. */
export function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick(random: () => number, count: number): number {
  return Math.min(count - 1, Math.floor(random() * count));
}

/** Half the time nothing: accessories must stay quiet at small sizes. */
function sometimes(random: () => number, count: number, chance: number): number {
  return random() < chance ? 1 + pick(random, count - 1) : 0;
}

/** The persona key for a submission: its id when known (unique per
 * submission and stable across renames), else its name. */
export function personaKey(id: string | null | undefined, name?: string | null): string {
  return String(id || name || "anonymous");
}

export function agentPersona(key: string): DittoPersona {
  const seed = hashSeed("ditto:" + key);
  const random = seededRandom(seed);
  return {
    seed,
    body: pick(random, BODIES.length),
    tone: TONES[pick(random, TONES.length)]!,
    eyewear: sometimes(random, EYEWEAR.length, 0.5),
    headwear: sometimes(random, HEADWEAR.length, 0.55),
    extra: sometimes(random, EXTRAS.length, 0.35),
  };
}

export function humanPersona(key: string): HumanPersona {
  const seed = hashSeed("human:" + key);
  const random = seededRandom(seed);
  return {
    seed,
    skin: pick(random, SKINS.length),
    hair: pick(random, HAIRSTYLES.length),
    hairInk: pick(random, HAIR_INKS.length),
    shirt: TONES[pick(random, TONES.length)]!,
    prop: pick(random, HUMAN_PROPS.length),
  };
}

/** One sentence describing a persona, for titles and text alternatives. */
export function describeDitto(persona: DittoPersona): string {
  const parts = [persona.tone + " " + BODIES[persona.body]!.toLowerCase() + " Ditto"];
  if (persona.headwear) parts.push(HEADWEAR[persona.headwear]!.toLowerCase());
  if (persona.eyewear) parts.push(EYEWEAR[persona.eyewear]!.toLowerCase());
  if (persona.extra) parts.push(EXTRAS[persona.extra]!.toLowerCase());
  return parts.join(", ");
}
