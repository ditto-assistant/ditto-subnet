// Pure data folds for the benchmark reference material (renderBenchDocs
// 9535–9606 + applyBenchVersion 9740–9761). The changelog authority tagging
// rides on lib/bench-state's benchmarkAuthorityState so the rollout target is
// never promoted here either: only "collecting" / "blocked_ineligible" mark a
// target as rolling, and a superseded or activated rollout tags nothing.
import { benchmarkAuthorityState } from "../../lib/bench-state";
import type { GlossaryCategory, GlossaryVersion } from "../../types/bench";

export interface ChangelogItem {
  version: number;
  title: string;
  summary: string;
  epoch: string;
  highlights: string[];
  /** This contract drives validator weights right now. */
  active: boolean;
  /** An OPEN rollout is collecting toward this contract. */
  rollout: boolean;
}

/**
 * The version changelog, newest-first as served — versions are whatever the
 * glossary returns (gaps included; a missing v6 is simply not a row), and no
 * version literal decides the tags (monolith 9550–9569):
 *   var activeVersion = Number(activeBench) || Number(currentBench) || null;
 *   var rolloutVersion = Number(desiredBench) || activeVersion;
 *   var rolloutOpen = activeVersion && rolloutVersion > activeVersion &&
 *     (status === "collecting" || status === "blocked_ineligible");
 */
export function changelogItems(
  versions: GlossaryVersion[],
  activeVersion: number | null,
  desiredVersion: number | null,
  rolloutStatus: string | null | undefined,
): ChangelogItem[] {
  const authority = benchmarkAuthorityState(activeVersion, desiredVersion, rolloutStatus);
  return versions.map((entry) => ({
    version: Number(entry.version),
    title: entry.title || "",
    summary: entry.summary || "",
    epoch: entry.epoch || "",
    highlights: entry.highlights || [],
    active: authority.active !== null && Number(entry.version) === authority.active,
    rollout: authority.rolling && Number(entry.version) === authority.desired,
  }));
}

/**
 * The version-specific paragraph describes what bench_version 4 corrected.
 * It is true of 4 and of nothing else, so it is swapped for this neutral
 * statement of the versioning rule the moment the live version moves on,
 * rather than being left to misdescribe a version it was not written for
 * (applyBenchVersion 9755–9761).
 */
export function neutralVersionCopy(version: number): string {
  return (
    "Version " +
    version +
    " is the current generation contract. Each version fixes the dataset bytes and the " +
    "grading rules together, so a scoring correction ships as a new version rather than changing " +
    "an already-published score. See the version history below for what this version changed."
  );
}

/** Glossary category groups, in the monolith's fixed kind order (9575–9579). */
export const GLOSSARY_KINDS: ReadonlyArray<readonly [string, string]> = [
  ["memory", "Memory"],
  ["conversational", "Conversational grounding"],
  ["multi_step", "Multi-step tool trajectories"],
  ["tool", "Tool use"],
  ["integrity", "Anti-gaming / integrity"],
];

export interface GlossaryGroup {
  label: string;
  rows: GlossaryCategory[];
}

/** Categories grouped by kind; a kind with no categories renders no head. */
export function glossaryGroups(categories: GlossaryCategory[]): GlossaryGroup[] {
  return GLOSSARY_KINDS.map(([kind, label]) => ({
    label,
    rows: categories.filter((category) => category.kind === kind),
  })).filter((group) => group.rows.length > 0);
}
