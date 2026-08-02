// ATH review-queue data logic (monolith athReviewSnapshot 9153–9174,
// updateAthSnapshotLabel 9183–9196, loadAthReviews 9239–9274): the public
// activity endpoint filtered to held high-scores, fanned out over its pages
// with a bounded pool, cached so a failed refresh degrades to a labeled
// stale snapshot — honest copy, never example or private data.
import { getJSON, poolMap } from "../../lib/api";
import { REFRESH_MS } from "../../lib/config";
import { relTime } from "../../lib/format";
import type { AthSnapshot } from "../../types/pipeline";

/**
 * Stitch the full under-review snapshot: page 1, then the remaining pages
 * through a 4-wide pool so a deep queue can't open dozens of sockets at
 * once (9153–9174).
 */
export function athReviewSnapshot(): Promise<AthSnapshot> {
  const path = "/public/activity?review=ath&status=under_review&limit=200&page=1";
  return getJSON<AthSnapshot>(path).then((first) => {
    const pages = Number(first.total_pages) || 1;
    if (pages <= 1) return first;
    const pageNumbers: number[] = [];
    for (let page = 2; page <= pages; page++) pageNumbers.push(page);
    return poolMap(pageNumbers, 4, (pageNumber) =>
      getJSON<AthSnapshot>(path.replace("page=1", "page=" + pageNumber)),
    ).then((rest) => {
      let entries = (first.entries || []).slice();
      rest.forEach((snapshot) => {
        entries = entries.concat(snapshot.entries || []);
      });
      first.entries = entries;
      first.count = entries.length;
      first.total = entries.length;
      return first;
    });
  });
}

export interface AthSnapshotLabel {
  text: string;
  stale: boolean;
}

/** Fresh / cached (older than two refresh ticks) / refresh-failed states
 * (updateAthSnapshotLabel 9183–9196). */
export function athSnapshotLabel(
  snapshot: AthSnapshot,
  refreshFailed: boolean,
  now: number = Date.now(),
): AthSnapshotLabel {
  const generated = new Date(snapshot.generated_at ?? "");
  const stale =
    refreshFailed ||
    Number.isNaN(generated.getTime()) ||
    now - generated.getTime() > REFRESH_MS * 2;
  if (refreshFailed) {
    return { text: "Refresh failed · showing last public snapshot", stale };
  }
  if (stale) {
    return { text: "Cached snapshot · " + relTime(snapshot.generated_at), stale };
  }
  return { text: "Public snapshot · " + relTime(snapshot.generated_at), stale };
}
