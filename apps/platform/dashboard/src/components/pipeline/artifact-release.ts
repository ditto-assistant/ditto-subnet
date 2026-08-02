// Source-release copy for a submission's artifact (monolith
// artifactReleaseCopy 3544–3585, artifactReleaseNote 3587–3592). Pure: the
// note/card components render these. The only defaulted consensus-adjacent
// literal in the dashboard lives here via lib/scoring.embargoHours (48h).
import { embargoHours } from "../../lib/scoring";
import { relTimeUntil } from "../../lib/format";

export interface ArtifactRelease {
  /** "available" | "embargoed" | "under_review" | "awaiting_quorum" | other. */
  status?: string | null;
  bench_version?: number | null;
  embargo_hours?: number | null;
  available_at?: string | null;
  download_available?: boolean | null;
}

export interface ArtifactReleaseCopy {
  state: "available" | "embargoed" | "under_review" | "awaiting_quorum" | "unavailable";
  label: string;
  detail: string;
}

export function artifactReleaseCopy(
  release: ArtifactRelease | null | undefined,
): ArtifactReleaseCopy | null {
  if (!release) return null;
  const hours = embargoHours(release);
  const bench = release.bench_version == null ? "" : " on Bench v" + release.bench_version;
  if (release.status === "available") {
    return {
      state: "available",
      label: "Source public",
      detail:
        "This king's source" +
        bench +
        " cleared its " +
        hours +
        "-hour public window after on-chain weights were set on it. The download link lasts five minutes.",
    };
  }
  if (release.status === "embargoed") {
    return {
      state: "embargoed",
      label: "Privacy window",
      detail: release.available_at
        ? "Source unlocks " +
          relTimeUntil(release.available_at) +
          ", " +
          hours +
          " hours after validators' on-chain weights were set on this king" +
          bench +
          "."
        : "King-only source: unlocks " +
          hours +
          " hours after validators' on-chain weights are confirmed on this miner (commit-reveal). Awaiting that on-chain confirmation.",
    };
  }
  if (release.status === "under_review") {
    const elapsed = Boolean(release.available_at && Date.parse(release.available_at) <= Date.now());
    return {
      state: "under_review",
      label: "Held for review",
      detail:
        "Source stays private while review is active. If cleared, its king reveal timing still applies" +
        (elapsed ? ", so this source will be public immediately." : "."),
    };
  }
  if (release.status === "awaiting_quorum") {
    return {
      state: "awaiting_quorum",
      label: "Awaiting 3/3",
      detail:
        "Source remains private until three validators score the same benchmark version. Source is only ever released for the leaderboard king.",
    };
  }
  return {
    state: "unavailable",
    label: "Source private",
    detail:
      "Source is private: only the leaderboard king's source is ever released, and only after on-chain weights confirm its reign. This submission has not qualified.",
  };
}

/** The stage-cell note renders only the three externally meaningful states
 * (artifactReleaseNote 3587–3592); null hides it. */
export function artifactReleaseNote(
  release: ArtifactRelease | null | undefined,
): { state: string; text: string } | null {
  const copy = artifactReleaseCopy(release);
  if (!copy || ["available", "embargoed", "under_review"].indexOf(copy.state) < 0) return null;
  return {
    state: copy.state,
    text:
      copy.label +
      (copy.state === "embargoed" && release?.available_at
        ? " · " + relTimeUntil(release.available_at)
        : ""),
  };
}
