// The sidebar DittoBench badge (monolith renderBenchBadge 3831–3849): names
// the rollout *transition* ("DittoBench v6 → v7 rollout") instead of a bare
// "latest" claim, and never promotes the in-flight rollout target to the
// active seat (benchmarkAuthorityState only reports rolling for
// "collecting" / "blocked_ineligible"). All versions arrive from API
// payloads; nothing here is a literal.
import type { JSX } from "solid-js";

import { benchBadgeLabel, benchmarkAuthorityState } from "../../lib/bench-state";

export interface BenchBadgeProps {
  /** The authoritative active bench version (activeBench || currentBench). */
  active: unknown;
  desired: unknown;
  status: string | null | undefined;
  /** Finalized runs older than the settled version are on the board. */
  hasOlderRuns: boolean;
}

export function BenchBadge(props: BenchBadgeProps): JSX.Element {
  const label = () =>
    benchBadgeLabel(
      benchmarkAuthorityState(props.active, props.desired, props.status),
      props.hasOlderRuns,
    );
  return (
    <span id="bench-badge" class="bench-badge" classList={{ show: label() !== "" }}>
      {label()}
    </span>
  );
}
