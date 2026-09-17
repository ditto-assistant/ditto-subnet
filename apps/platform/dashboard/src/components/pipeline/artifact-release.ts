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
  emission_confirmed_at?: string | null;
  /** Historical telemetry only; never starts the disclosure clock. */
  weight_confirmed_at?: string | null;
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
  const earned = Boolean(
    release.emission_confirmed_at && Number.isFinite(Date.parse(release.emission_confirmed_at)),
  );
  if (["available", "embargoed"].includes(release.status ?? "") && !earned) {
    return {
      state: "unavailable",
      label: "Awaiting winner earnings",
      detail:
        "Source stays private until this exact submission earns winner emissions in a completed tempo. A crown or positive revealed weights alone does not start the " +
        hours +
        "-hour privacy window.",
    };
  }
  if (release.status === "available") {
    return {
      state: "available",
      label: "Source public",
      detail:
        "This king's source" +
        bench +
        " cleared its " +
        hours +
        "-hour privacy window after this submission earned winner emissions in a completed tempo. The download link lasts five minutes.",
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
          " hours after this submission earned winner emissions in a completed tempo" +
          bench +
          "."
        : "King-only source: unlocks " +
          hours +
          " hours after confirmed winner emissions in a completed tempo. Awaiting a release deadline.",
    };
  }
  if (release.status === "under_review") {
    const elapsed = Boolean(
      earned && release.available_at && Date.parse(release.available_at) <= Date.now(),
    );
    return {
      state: "under_review",
      label: "Held for review",
      detail:
        "Source stays private while review is active. If cleared, its privacy window after confirmed winner earnings still applies" +
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
      "Source is private: only the leaderboard king's source is ever released, and only after it earns winner emissions in a completed tempo and its privacy window ends. This submission has not qualified.",
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
