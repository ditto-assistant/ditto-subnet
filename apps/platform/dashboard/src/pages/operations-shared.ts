// The one shared /public/operations snapshot, as both the pipeline page and
// the fleet page consume it (loadOperations 9434–9462). A refresh failure
// does not invalidate the last reconciled snapshot: every panel keeps
// rendering it while the next poll retries, and only the note changes. Only
// a cold start with no trustworthy data renders the unavailable placeholders
// (loadOperations catch, weekend drift 9816–9834).
import { createMemo } from "solid-js";
import type { Accessor } from "solid-js";

import type { PipelineEntryExt } from "../components/operations/pipeline";
import type { ResourceState } from "../data/useEndpoint";
import { relTime } from "../lib/format";
import type { OperationsPayload } from "../types/fleet";

/** Errored resources read as absent — stated absence, never stale-as-fresh. */
export function latest<T>(resource: ResourceState<T>): T | undefined {
  if (resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

/** The operations activity slice as actually served — the shared PipelineFeed
 * type declares only `entries`; the feed also carries the authoritative
 * status counts and the visible/total window the snapshot note reads. */
export interface OpsActivityFeed {
  entries?: PipelineEntryExt[];
  status_counts?: Record<string, number>;
  count?: number;
  total?: number;
}

export interface OperationsSnapshot {
  ops: Accessor<OperationsPayload | undefined>;
  refreshDelayed: Accessor<boolean>;
  opsUnavailable: Accessor<boolean>;
  opsLoading: Accessor<boolean>;
  activity: Accessor<OpsActivityFeed | undefined>;
  /** The one shared snapshot's provenance note (loadOperations 9448–9460). */
  snapshotNote: Accessor<string>;
}

export function useOperationsSnapshot(
  operations: ResourceState<OperationsPayload>,
): OperationsSnapshot {
  const ops = createMemo<OperationsPayload | undefined>((prev) => latest(operations) ?? prev);
  const refreshDelayed = () => Boolean(operations.error()) && Boolean(ops());
  const opsUnavailable = () => Boolean(operations.error()) && !ops();
  const opsLoading = () => !ops() && !opsUnavailable();
  const activity = (): OpsActivityFeed | undefined =>
    ops()?.activity as OpsActivityFeed | undefined;
  const snapshotNote = createMemo(() => {
    if (opsUnavailable()) return "Shared operations snapshot unavailable";
    if (refreshDelayed()) {
      const at = ops()?.generated_at;
      return "Refresh delayed · showing last reconciled snapshot" + (at ? " · " + relTime(at) : "");
    }
    const data = ops();
    if (!data) return "Loading one shared operations snapshot…";
    const visibleHistory =
      Number(activity()?.count ?? NaN) < Number(activity()?.total ?? NaN)
        ? " · recent history shown; full history in Activity"
        : "";
    return "Pipeline and fleet reconciled" + visibleHistory + " · " + relTime(data.generated_at);
  });
  return { ops, refreshDelayed, opsUnavailable, opsLoading, activity, snapshotNote };
}
