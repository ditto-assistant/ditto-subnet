// The submission-pipeline page. The top is the pipeline theater: the same
// flow told as three acts (a factory that builds each submission into its
// Paper Ditto, airport-style screening, and the scoring arena), each with its
// authoritative counts and cast. Beneath it the atlas map (board, rescreen
// notice, deferred source review branch) stays as the stage-by-stage
// manifest with every row, filter and link. Every panel consumes exactly ONE
// /public/operations snapshot per tick, and the snapshot note states the
// reconciliation plus its age (skew is visible, not papered over).
import { createMemo, onMount } from "solid-js";
import type { JSX } from "solid-js";

import {
  IntegrityReviewBranch,
  PipelineBoard,
  RescreenNotice,
} from "../components/operations/PipelineBoard";
import type { FleetReportExt } from "../components/operations/fleet";
import { PipelineTheater } from "../components/theater/PipelineTheater";
import type { PipelineEntryExt } from "../components/operations/pipeline";
import { operationsResource } from "../data/operations";
import { useEndpoint } from "../data/useEndpoint";
import type { ResourceState } from "../data/useEndpoint";
import { REFRESH_MS } from "../lib/config";
import type { FleetReport, OperationsPayload } from "../types/fleet";
import { latest, useOperationsSnapshot } from "./operations-shared";

export function PipelinePage(
  props: {
    operations?: ResourceState<OperationsPayload>;
  } = {},
): JSX.Element {
  const operations = props.operations ?? operationsResource();
  if (!props.operations) onMount(() => operations.refresh());
  const screeners = useEndpoint<FleetReport>("/public/screeners", { pollMs: REFRESH_MS });

  const snap = useOperationsSnapshot(operations);
  const pipelineEntries = createMemo<PipelineEntryExt[]>(() => snap.activity()?.entries ?? []);
  const statusCounts = () => snap.activity()?.status_counts ?? {};

  // The benchmark the fleet is actually scoring, as reported alongside the
  // verdicts computed against it; the snapshot-level version is the fallback
  // (activeBenchVersion 8295–8298). Never a literal.
  const benchVersion = createMemo(() => {
    const report = snap.ops()?.validators as FleetReportExt | undefined;
    return Number(report?.active_bench_version) || Number(snap.ops()?.active_bench_version) || null;
  });

  return (
    <section class="page active" data-page="pipeline">
      <p class="atlas-note theater-snapshot" id="operations-snapshot" aria-live="polite">
        {snap.snapshotNote()}
      </p>
      <PipelineTheater
        entries={pipelineEntries()}
        statusCounts={statusCounts()}
        screeners={latest(screeners) ?? null}
        activeVersion={benchVersion()}
        unavailable={snap.opsUnavailable()}
      />
      <section class="operations pipeline-manifest" aria-labelledby="pipeline-manifest-title">
        <h2 id="pipeline-manifest-title" class="pipeline-manifest-title">
          Every submission, stage by stage
        </h2>
        <div class="fleet-atlas">
          <div class="pipeline-map">
            <div class="atlas-label">
              <div>
                <span class="atlas-note">
                  Mechanical admission builds a verified image before validators. Deferred source
                  review happens later only for qualifying or anomalous results; entering it is not
                  a finding. Each held row names its trigger and what the automated review
                  concluded.
                </span>
              </div>
            </div>
            <RescreenNotice entries={pipelineEntries()} unavailable={snap.opsUnavailable()} />
            <PipelineBoard
              entries={pipelineEntries()}
              statusCounts={statusCounts()}
              unavailable={snap.opsUnavailable()}
              loading={snap.opsLoading()}
              screeners={latest(screeners) ?? null}
              activeVersion={benchVersion()}
            />
            <IntegrityReviewBranch
              entries={pipelineEntries()}
              statusCounts={statusCounts()}
              unavailable={snap.opsUnavailable()}
              loading={snap.opsLoading()}
            />
          </div>
        </div>
      </section>
    </section>
  );
}
