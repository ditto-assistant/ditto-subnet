// The agent evidence deep view — the drawer body for one submission
// (monolith renderPipelineSummary 7940–7999, renderScoreHeadline 7882–7911,
// renderFamilyStanding 7913–7938, renderPipelineDetail 7745–7880,
// renderPipelineHistoryDisclosure 8001–8010, bindPipelineHistory 8012–8039,
// renderAcceptedScores, renderConfirmationScores, openActivityEntry):
// screening evidence, validator quorum progress with superseded failures kept
// as history (#459), per-check benchmark progress, terminal screening review
// cards, model-use verdicts (#527), the review packet, artifact download
// (#278), and the dispute form.
//
import { createQuery } from "@tanstack/solid-query";
import { For, Show, createMemo } from "solid-js";
import type { Accessor, JSX } from "solid-js";

import { publicQueryKeys, queryClient } from "../../data/queryClient";
import { getJSON } from "../../lib/api";
import {
  agentName,
  agentVersionLabel,
  duplicateLabel,
  fx,
  relTime,
  shortKey,
} from "../../lib/format";
import { cohortMedian, displayComposite } from "../../lib/scoring";
import { useEndpoint } from "../../data/useEndpoint";
import type { BenchConfigPayload, GlossaryPayload } from "../../types/bench";
import type { LeaderboardEntry } from "../../types/leaderboard";
import type {
  AcceptedScore,
  ActivityEntry,
  BenchmarkProgress,
  InferenceRun,
  PipelinePayload,
  ScreeningAttempt,
  ValidationAttempt,
} from "../../types/pipeline";
import { CopyButton } from "../shell/CopyButton";
import { EntityButton } from "../ui/EntityButton";
import { StatusChip } from "../ui/StatusChip";
import { BenchmarkProgressView } from "../operations/progress";
import type { ArtifactRelease } from "../pipeline/artifact-release";
import {
  benchmarkVersionKey,
  benchmarkVersionLabel,
  duplicateComparisonLabel,
  retestAttemptCounts,
  reviewEvidenceText,
  scoreFloorAttribution,
  validationDetail,
} from "../pipeline/status";
import type { ActivityStatusEntry } from "../pipeline/status";
import { ArtifactReleaseCard } from "./ArtifactRelease";
import { CasesSection } from "./Cases";
import { ScreeningDispute } from "./DisputeForm";
import { ScreeningReview } from "./ScreeningReview";
import { TelemetryLoader } from "./Telemetry";
import {
  benchmarkCohorts,
  cohortProgressSummary,
  pipelineCurrentCohort,
  pipelineDisplayState,
} from "./cohorts";
import type { BenchmarkCohort } from "./cohorts";
import { screeningAttemptLabel, validationAttemptView } from "./labels";
import { modelUseRows } from "./model-use";
import type { ModelUse } from "./model-use";
import { reviewPacket } from "./review-packet";

type RankedEntry = LeaderboardEntry & { rank?: number | null };

/** Board fields the confirmation-retest section reads (continual fold). */
interface BoardConfirmationFields {
  completed_wave_composites?: Array<number | string> | null;
  confirmation_seed_composites?: Array<number | string> | null;
  confirmation_seed_depth?: number | null;
  completed_wave_count?: number | null;
  aggregate_method?: string | null;
  aggregate_sample_count?: number | null;
  official_composite?: number | null;
}

interface SubmissionFamilyMember {
  agent_id?: string;
  agent_name?: string | null;
  agent_version?: number | null;
  canonical_composite?: number;
  representative?: boolean;
}

interface SubmissionFamily {
  member_count?: number;
  members?: SubmissionFamilyMember[];
}

/** Pipeline fields beyond the wire type that the drawer reads. */
export interface PipelineDetailPayload extends PipelinePayload {
  agent_id?: string;
  submission_family?: SubmissionFamily | null;
  score_floor_agent_id?: string | null;
  score_floor_agent_name?: string | null;
  score_floor_agent_version?: number | null;
  /** #648 added the release to the pipeline payload; the deferred history is
   * the authority once it lands, the entry is what paints until then. */
  artifact_release?: ArtifactRelease | null;
}

type ScoredScore = AcceptedScore & { model_use?: ModelUse | null };

/**
 * What the drawer needs of a submission before any history is loaded: an
 * activity row, an operations pipeline row, or a `/public/agent/{id}/summary`
 * payload. `score_composite` and `active_benchmarks` only ride the summary and
 * operations shapes, and only the summary path renders them (#648).
 */
export type AgentEvidenceEntry = ActivityEntry & {
  artifact_release?: ArtifactRelease | null;
  active_benchmarks?: BenchmarkProgress[] | null;
  score_composite?: number | null;
};

export interface AgentEvidenceProps {
  entry: AgentEvidenceEntry;
  /** Ranked board entries (continual retests + family standing cross-ref). */
  entries?: () => RankedEntry[];
  /** Mid-rollout settled view, for the family representative's composite. */
  settledView?: () => boolean;
  /** The full record is queried in parallel with the summary by EntityPanel. */
  pipeline?: Accessor<PipelineDetailPayload | undefined>;
  pipelineLoading?: Accessor<boolean>;
  pipelineFetching?: Accessor<boolean>;
  pipelineError?: Accessor<unknown>;
  retryPipeline?: () => void;
}

function StatRow(props: { k: string; v: string }): JSX.Element {
  return (
    <div class="stat-row">
      <span class="k">{props.k}</span>
      <span class="v">{props.v}</span>
    </div>
  );
}

// ── Score headline (renderScoreHeadline 7726–7755) ──────────────────────────

function ScoreHeadline(props: { pipeline: PipelineDetailPayload }): JSX.Element {
  const quorum = () => Math.max(1, Number(props.pipeline.quorum) || 3);
  const cohorts = createMemo(() =>
    benchmarkCohorts(props.pipeline)
      .filter((cohort) => cohort.scores.length)
      .slice()
      .sort((a, b) => {
        if (a.key === "unknown") return 1;
        if (b.key === "unknown") return -1;
        return Number(b.key) - Number(a.key);
      }),
  );
  return (
    <Show when={cohorts().length}>
      <section class="score-headline" aria-label="Benchmark medians">
        <For each={cohorts()}>
          {(cohort, index) => {
            const complete = cohort.scores.length >= quorum();
            const median = cohortMedian(cohort.scores);
            const stateLabel = complete
              ? "Official median"
              : "Preliminary · " + cohort.scores.length + " of " + quorum();
            const stateTip = complete
              ? "Quorum reached. The median of " +
                quorum() +
                " independent accepted scores is final for " +
                benchmarkVersionLabel(cohort.key) +
                "."
              : "Preliminary: only " +
                cohort.scores.length +
                " of " +
                quorum() +
                " scores are in. This median can change until quorum.";
            return (
              <div
                class={
                  "score-headline-item" +
                  (index() === 0 ? " primary" : "") +
                  (complete ? "" : " provisional")
                }
              >
                <span class="score-headline-bench">{benchmarkVersionLabel(cohort.key)}</span>
                <span class="score-headline-value">{fx(median)}</span>
                <span class="score-headline-state" title={stateTip}>
                  {stateLabel}
                </span>
              </div>
            );
          }}
        </For>
      </section>
    </Show>
  );
}

// ── Family standing (renderFamilyStanding 7757–7782) ────────────────────────

function FamilyStanding(props: {
  pipeline: PipelineDetailPayload;
  entries: () => RankedEntry[];
  settledView: boolean;
}): JSX.Element {
  const family = () => props.pipeline.submission_family || null;
  const view = createMemo(() => {
    const f = family();
    if (!f || !f.members || f.members.length < 2) return null;
    const current = f.members.find(
      (member) => String(member.agent_id) === String(props.pipeline.agent_id),
    );
    const representative = f.members.find((member) => member.representative);
    if (!current || !representative) return null;
    return { family: f, current, representative };
  });
  return (
    <Show when={view()}>
      {(v) => {
        const boardEntry = () =>
          props
            .entries()
            .find((entry) => String(entry.agent_id) === String(v().representative.agent_id));
        return (
          <Show
            when={!v().current.representative}
            fallback={
              <section class="family-standing" aria-label="Submission family standing">
                <strong>Ranked family representative</strong>
                <p>
                  This is the best canonical generation among {v().family.member_count} scored
                  submissions sharing one leaderboard and emissions slot. Expand its family on the
                  leaderboard to see the others.
                </p>
              </section>
            }
          >
            <section class="family-standing" aria-label="Submission family standing">
              <strong>Scored, but not independently ranked</strong>
              <p>
                This submission is finalized at {fx(Number(v().current.canonical_composite))}. It
                shares one leaderboard and emissions slot with {Number(v().family.member_count) - 1}
                {v().family.member_count === 2 ? " other submission. " : " other submissions. "}
                <EntityButton
                  kind="agent"
                  id={v().representative.agent_id}
                  label={agentName(v().representative.agent_name)}
                />{" "}
                currently represents the family
                {boardEntry()
                  ? " at raw rank #" +
                    (boardEntry() as RankedEntry).rank +
                    " with a current leaderboard score of " +
                    fx(displayComposite(boardEntry() as RankedEntry, props.settledView))
                  : " with a canonical score of " +
                    fx(Number(v().representative.canonical_composite))}
                .
              </p>
            </section>
          </Show>
        );
      }}
    </Show>
  );
}

// ── Screener result (renderScreeningAttempt 7594–7627) ──────────────────────

function screeningMetaRest(a: ScreeningAttempt, isOld: boolean): string {
  let meta = "";
  if (a.quarantine_resolution === "release") {
    meta += " · Operator released this submission from quarantine.";
  } else if (a.quarantine_resolution === "rescreen") {
    meta += " · Operator sent this submission through screening again.";
  } else if (a.quarantine_resolution === "reject") {
    meta += " · Operator rejected this submission after quarantine review.";
  } else if (isOld && a.status === "expired") {
    meta += " · This attempt did not finish before its deadline.";
  } else if (isOld && a.status === "failed") {
    meta += " · The screener could not complete this attempt.";
    if (a.reason) meta += " Recorded detail: " + a.reason;
  } else if (a.reason) {
    meta += " · " + a.reason;
  }
  if (a.status === "running" && a.deadline) {
    meta += " · deadline " + new Date(a.deadline).toLocaleString();
  }
  return meta;
}

function ScreeningAttemptRow(props: { attempt: ScreeningAttempt; isOld: boolean }): JSX.Element {
  const a = () => props.attempt;
  const label = () => screeningAttemptLabel(a());
  const when = () => a().quarantine_resolved_at || a().finished_at || a().started_at;
  return (
    <div class="attempt">
      <div>
        <span class="attempt-version">Policy v{a().policy_version}</span>
      </div>
      <div class="attempt-main">
        <b>
          <StatusChip label={label()[0]} tone={label()[1]} />
        </b>
        <div class="attempt-meta">
          <EntityButton
            kind="screener"
            id={a().screener_hotkey}
            label={"Screener " + shortKey(a().screener_hotkey)}
          />
          {screeningMetaRest(a(), props.isOld)}
        </div>
        <Show when={a().quarantine_resolution_reason}>
          {(reason) => (
            <div class="attempt-resolution-reason">
              <b>Operator reason:</b> {reason()}
            </div>
          )}
        </Show>
      </div>
      <div class="attempt-time" title={String(when() ?? "")}>
        {relTime(when())}
      </div>
      <ScreeningReview attempt={a()} />
    </div>
  );
}

// ── Accepted validator scores (renderAcceptedScores 7313–7379) ──────────────

function AcceptedScoreView(props: {
  score: ScoredScore;
  index: number;
  complete: boolean;
  config: () => BenchConfigPayload | undefined;
  glossary: () => GlossaryPayload | undefined;
}): JSX.Element {
  const score = () => props.score;
  const label = () =>
    props.complete ? "Quorum input " + (props.index + 1) : "Provisional score " + (props.index + 1);
  const benchLabel = () =>
    score().bench_version == null ? "Bench version unknown" : "Bench v" + score().bench_version;
  const accepted = () => (score().accepted_at ? relTime(score().accepted_at) : "Accepted");
  const seedSource = () =>
    score().seed_source === "on_chain"
      ? "Derived from an on-chain block hash after submission commitment."
      : score().seed_source === "validator_local"
        ? "Chosen unpredictably by the scoring validator's benchmark harness after submission commitment; per-submission dataset pinning was not enabled for this run."
        : "Generated from an unpredictable random fallback after submission commitment; no block provenance is available.";
  const transcriptTemplate = () => props.config()?.public_transcript_url_template || null;
  const telemetryTemplate = () => props.config()?.public_transcript_telemetry_url_template || null;
  return (
    <div class="accepted-score">
      <div>
        <span class="accepted-score-value">{fx(score().composite)}</span>
        <span class="accepted-score-label">
          <span class="bench-version-badge">{benchLabel()}</span>
          {label()} · {accepted()}
        </span>
      </div>
      <div class="accepted-score-meta">
        <div class="accepted-score-meta-row">
          <b>Seed</b> <code>{String(score().seed)}</code>{" "}
          <CopyButton value={String(score().seed ?? "")} label="benchmark seed" />
          <br />
          {seedSource()}
        </div>
        <Show
          when={score().reproduction_command}
          fallback={
            <div class="accepted-score-meta-row">
              {score().seed_source === "validator_local"
                ? "No reproduction command: this run was scored before per-submission dataset pinning was enabled."
                : "Dataset command unavailable for this legacy score."}
            </div>
          }
        >
          {(command) => (
            <div class="accepted-score-meta-row">
              <b>Reproduce dataset</b>
              <div class="accepted-score-command">
                <code>{command()}</code>
                <CopyButton value={command()} label="dataset command" />
              </div>
            </div>
          )}
        </Show>
        <Show when={score().verification_command && score().dataset_sha256}>
          <div class="accepted-score-meta-row">
            <b>Verify hash</b> <code>{score().dataset_sha256}</code>
            <div class="accepted-score-command">
              <code>{score().verification_command}</code>
              <CopyButton
                value={score().verification_command}
                label="dataset verification command"
              />
            </div>
          </div>
        </Show>
        <Show when={score().transcript_sha256}>
          {(sha) => (
            <div class="accepted-score-meta-row">
              <b>Transcript</b> <code>{sha()}</code>{" "}
              <CopyButton value={sha()} label="transcript digest" />
              <Show when={transcriptTemplate()}>
                {(template) => (
                  <>
                    {" "}
                    <a href={template().replace("{sha256}", sha())} target="_blank" rel="noopener">
                      download ↗
                    </a>
                  </>
                )}
              </Show>
              <br />
              Signature-bound digest of this run’s published transcript (every graded response and
              observed tool call). Regenerate the dataset with the command above and re-run the
              public grader over the transcript: the result must equal this score exactly.
              <TelemetryLoader sha256={sha()} urlTemplate={telemetryTemplate()} />
            </div>
          )}
        </Show>
        <Show when={modelUseRows(score().model_use).length}>
          <div class="accepted-score-meta-row accepted-score-model-use">
            <For each={modelUseRows(score().model_use)}>
              {(row) => <StatRow k={row.k} v={row.v} />}
            </For>
          </div>
        </Show>
      </div>
      <CasesSection caseResults={score().case_results} glossary={props.glossary} />
    </div>
  );
}

function AcceptedScores(props: {
  pipeline: PipelineDetailPayload;
  config: () => BenchConfigPayload | undefined;
  glossary: () => GlossaryPayload | undefined;
}): JSX.Element {
  const quorum = () => Math.max(1, Number(props.pipeline.quorum) || 3);
  const deprioritized = () => props.pipeline.status === "below_score_floor";
  const cohorts = createMemo(() =>
    benchmarkCohorts(props.pipeline).filter((cohort) => cohort.scores.length),
  );
  const note = (cohort: BenchmarkCohort, complete: boolean): string =>
    (complete
      ? "This benchmark version has reached quorum. Its aggregate is the median of these independent accepted scores."
      : deprioritized() && cohort.key === benchmarkVersionKey(props.pipeline.active_bench_version)
        ? "The third score remains queued at low priority because the best reachable median is below the continuation floor. " +
          scoreFloorAttribution(props.pipeline as ActivityStatusEntry) +
          " This result remains provisional until quorum."
        : "Scores in this benchmark version remain provisional until " +
          quorum() +
          " independent scores arrive. Provisional scores may change; the final median is authoritative within this benchmark version.") +
    " Every seed is fixed only after submission commitment, so it makes the completed evaluation reproducible without allowing the already-submitted artifact to be retuned.";
  return (
    <section class="pipeline-section" aria-labelledby="pipeline-accepted-scores">
      <div class="pipeline-section-heading">
        <h4 id="pipeline-accepted-scores">Accepted validator scores</h4>
      </div>
      <Show
        when={cohorts().length}
        fallback={<p class="pipeline-detail-state">No validator score has been accepted yet.</p>}
      >
        <For each={cohorts()}>
          {(cohort) => {
            const complete = cohort.scores.length >= quorum();
            const median = complete ? cohortMedian(cohort.scores) : null;
            return (
              <div class="benchmark-cohort">
                <div class="benchmark-cohort-heading">
                  <h5>{benchmarkVersionLabel(cohort.key)}</h5>
                  <span class="benchmark-cohort-summary">
                    Canonical quorum · {Math.min(cohort.scores.length, quorum())} of {quorum()}
                  </span>
                </div>
                <p class="accepted-score-note">{note(cohort, complete)}</p>
                <div class="accepted-score-list">
                  <For each={cohort.scores}>
                    {(score, index) => (
                      <AcceptedScoreView
                        score={score as ScoredScore}
                        index={index()}
                        complete={complete}
                        config={props.config}
                        glossary={props.glossary}
                      />
                    )}
                  </For>
                </div>
                <Show when={median != null}>
                  <p class="accepted-score-final">
                    {benchmarkVersionLabel(cohort.key)} initial quorum aggregate:{" "}
                    {fx(median as number)} · canonical median of {quorum()}. Completed continual
                    waves then move the current leaderboard score.
                  </p>
                </Show>
              </div>
            );
          }}
        </For>
      </Show>
    </section>
  );
}

// ── Continual top-five retests (renderConfirmationScores 7381–7459) ─────────

function ConfirmationScores(props: {
  pipeline: PipelineDetailPayload;
  entries: () => RankedEntry[];
}): JSX.Element {
  const boardEntry = createMemo(
    () =>
      props.entries().find((entry) => entry.agent_id === props.pipeline.agent_id) as
        | (RankedEntry & BoardConfirmationFields)
        | undefined,
  );
  const activeVersion = () => Number(props.pipeline.active_bench_version);
  const benchBadge = () => benchmarkVersionLabel(benchmarkVersionKey(activeVersion()));
  const completedWaves = createMemo(() => {
    const entry = boardEntry();
    return entry && Array.isArray(entry.completed_wave_composites)
      ? entry.completed_wave_composites.map(Number).filter(Number.isFinite)
      : [];
  });
  const pendingSeeds = createMemo(() => {
    const entry = boardEntry();
    return entry && Array.isArray(entry.confirmation_seed_composites)
      ? entry.confirmation_seed_composites.map(Number).filter(Number.isFinite)
      : [];
  });
  const counts = createMemo(() =>
    retestAttemptCounts(
      (props.pipeline.validation_attempts || []).filter(
        (attempt) => Number(attempt.bench_version) === activeVersion(),
      ),
    ),
  );
  // Accepted-but-not-yet-foldable retests still belong on the page: the rows
  // are append-only and were never deleted, they are merely waiting on a
  // cohort-wide shared seed. Keep the section when any exist.
  const pendingDepth = () =>
    Math.max(Number(boardEntry()?.confirmation_seed_depth) || 0, pendingSeeds().length);
  const aggregateDepth = () => Number(boardEntry()?.completed_wave_count) || 0;
  const excludedDepth = () => Math.max(0, pendingDepth() - aggregateDepth());
  const visible = () =>
    Boolean(
      completedWaves().length ||
      pendingDepth() ||
      counts().running ||
      counts().assigned ||
      counts().expired,
    );
  const stateBits = () => {
    const bits: string[] = [];
    if (counts().running) bits.push(counts().running + " running");
    if (counts().assigned) bits.push(counts().assigned + " assigned");
    if (counts().expired) bits.push(counts().expired + " expired");
    return bits;
  };
  return (
    <Show when={visible()}>
      <section class="pipeline-section" aria-labelledby="pipeline-confirmation-scores">
        <div class="pipeline-section-heading">
          <h4 id="pipeline-confirmation-scores">Continual top-five retests</h4>
          <Show when={stateBits().length}>
            <span class="benchmark-cohort-summary">{stateBits().join(" · ")}</span>
          </Show>
        </div>
        <Show
          when={boardEntry()?.aggregate_method === "continual_mean"}
          fallback={
            <Show
              when={completedWaves().length}
              fallback={
                <Show
                  when={pendingDepth()}
                  fallback={
                    <p class="accepted-score-note">
                      No full cohort wave has completed for this agent yet, so the initial
                      three-score median still drives its leaderboard position.
                    </p>
                  }
                >
                  <p class="accepted-score-note">
                    {pendingDepth()} retest {pendingDepth() === 1 ? "seed is" : "seeds are"}{" "}
                    recorded for this agent, but no cohort wave is complete: a wave counts only once
                    every current emission-set member has a result for the same seed, so it resets
                    whenever that set changes. The rows below are the append-only audit trail and
                    were not discarded; the initial three-score median drives its leaderboard
                    position meanwhile.
                  </p>
                </Show>
              }
            >
              <p class="accepted-score-note">
                {completedWaves().length} full cohort{" "}
                {completedWaves().length === 1 ? "wave is" : "waves are"} recorded. The initial
                three-score median remains authoritative until the compatible continual-mean rollout
                activates.
              </p>
            </Show>
          }
        >
          <p class="accepted-score-final">
            Current leaderboard score: {fx(Number(boardEntry()?.official_composite))} · arithmetic
            mean of the initial three scores plus {boardEntry()?.completed_wave_count}{" "}
            aggregate-eligible shared {boardEntry()?.completed_wave_count === 1 ? "wave" : "waves"}{" "}
            ({boardEntry()?.aggregate_sample_count} samples total). {pendingDepth()} retained
            confirmation {pendingDepth() === 1 ? "seed remains" : "seeds remain"} in the append-only
            audit ledger; {excludedDepth()} are not currently fold-eligible.
          </p>
        </Show>
        <p class="accepted-score-note">
          The initial quorum remains immutable provenance. The current leaderboard score continually
          adjusts up or down as full cohort waves complete.
        </p>
        <p class="accepted-score-note">
          Only aggregate-eligible shared-wave averages are shown here. Partial, legacy, and
          superseded confirmation seeds remain in the append-only audit ledger but are omitted from
          the score list.
        </p>
        <Show
          when={completedWaves().length || pendingSeeds().length}
          fallback={<p class="pipeline-detail-state">No completed continual retest wave yet.</p>}
        >
          <div class="accepted-score-list">
            <Show
              when={completedWaves().length}
              fallback={
                <For each={pendingSeeds()}>
                  {(score, index) => (
                    <div class="accepted-score">
                      <div>
                        <span class="accepted-score-value">{fx(score)}</span>
                        <span class="accepted-score-label">
                          <span class="bench-version-badge">{benchBadge()}</span>
                          Retest seed {index() + 1} · recorded, wave not complete
                        </span>
                      </div>
                    </div>
                  )}
                </For>
              }
            >
              <For each={completedWaves()}>
                {(score, index) => (
                  <div class="accepted-score">
                    <div>
                      <span class="accepted-score-value">{fx(score)}</span>
                      <span class="accepted-score-label">
                        <span class="bench-version-badge">{benchBadge()}</span>
                        Aggregate-eligible shared wave {index() + 1} · consensus aggregate
                      </span>
                    </div>
                  </div>
                )}
              </For>
            </Show>
          </div>
        </Show>
      </section>
    </Show>
  );
}

// ── Validator progress (superseded failures stay history, #459) ─────────────

function ValidationAttemptRow(props: { attempt: ValidationAttempt }): JSX.Element {
  const view = () => validationAttemptView(props.attempt);
  return (
    <div class="attempt">
      <div>
        <span class="bench-version-badge">{view().benchLabel}</span>
      </div>
      <div class="attempt-main">
        <b>
          <span class={"stage" + (view().tone ? " " + view().tone : "")}>
            {view().headline}
            <Show when={view().retryTip}>
              {(tip) => (
                <span
                  class="retry-info"
                  role="img"
                  tabindex="0"
                  aria-label={tip()}
                  data-tooltip={tip()}
                >
                  i
                </span>
              )}
            </Show>
          </span>
        </b>
        <div class="attempt-meta">
          {view().purposeLabel}
          <EntityButton
            kind="validator"
            id={props.attempt.validator_hotkey}
            label={view().validatorLabel}
          />
          {view().metaRest}
        </div>
        <Show when={props.attempt.benchmark_progress}>
          {(progress) => <BenchmarkProgressView progress={progress()} />}
        </Show>
      </div>
      <div class="attempt-time" title={String(view().when ?? "")}>
        {relTime(view().when)}
      </div>
    </div>
  );
}

function formatMicrousd(value: number | null | undefined): string {
  const dollars = Math.max(0, Number(value) || 0) / 1_000_000;
  return "$" + dollars.toFixed(dollars >= 1 ? 2 : 4);
}

function formatCount(value: number | null | undefined): string {
  return Math.max(0, Number(value) || 0).toLocaleString();
}

function InferenceRuns(props: { runs: InferenceRun[] }): JSX.Element {
  const runs = createMemo(() =>
    props.runs
      .slice()
      .sort(
        (a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime(),
      ),
  );
  const totalCost = createMemo(() =>
    runs().reduce((sum, run) => sum + Math.max(0, Number(run.cost_microusd) || 0), 0),
  );
  const status = (run: InferenceRun): [string, string] => {
    if (run.status === "exhausted") return ["Allowance exhausted", "bad"];
    if (run.status === "active") return ["Running", "progress"];
    if (run.status === "pending") return ["Preparing", "progress"];
    return ["Closed", ""];
  };
  return (
    <Show when={runs().length}>
      <section class="pipeline-section" aria-labelledby="pipeline-inference-spend">
        <div class="pipeline-section-heading inference-spend-heading">
          <div>
            <h4 id="pipeline-inference-spend">Benchmark inference cost</h4>
            <p>
              Platform-metered chat and embedding spend. Each row is one validator benchmark run.
            </p>
          </div>
          <strong class="inference-spend-total">{formatMicrousd(totalCost())} total</strong>
        </div>
        <div class="inference-run-list">
          <For each={runs()}>
            {(run) => {
              const state = () => status(run);
              const chatTokens = () =>
                (Number(run.prompt_tokens) || 0) + (Number(run.completion_tokens) || 0);
              return (
                <div class="inference-run">
                  <div class="inference-run-cost">{formatMicrousd(run.cost_microusd)}</div>
                  <div class="inference-run-main">
                    <div class="inference-run-title">
                      <span class="bench-version-badge">
                        {run.bench_version == null
                          ? "Bench unknown"
                          : "Bench v" + run.bench_version}
                      </span>
                      <EntityButton
                        kind="validator"
                        id={run.validator_hotkey}
                        label={"Validator " + shortKey(run.validator_hotkey)}
                      />
                      <span class={"stage" + (state()[1] ? " " + state()[1] : "")}>
                        {state()[0]}
                      </span>
                    </div>
                    <div class="inference-run-meta">
                      {formatCount(run.requests)} of {formatCount(run.request_budget)} chat requests
                      {" · "}
                      {formatCount(chatTokens())} of {formatCount(run.token_budget)} chat tokens
                      {" · "}
                      {formatCount(run.embedding_requests)} embedding requests /{" "}
                      {formatCount(run.embedding_tokens)} tokens
                    </div>
                  </div>
                  <div class="attempt-time" title={String(run.updated_at || run.created_at || "")}>
                    {relTime(run.updated_at || run.created_at)}
                  </div>
                </div>
              );
            }}
          </For>
        </div>
      </section>
    </Show>
  );
}

// ── The drawer body ──────────────────────────────────────────────────────────
//
export function AgentEvidence(props: AgentEvidenceProps): JSX.Element {
  const entries = () => (props.entries ? props.entries() : []);
  const settled = () => (props.settledView ? props.settledView() : false);
  const agentId = () => String(props.entry.agent_id || "");
  // EntityPanel supplies the query it started alongside the summary. Keep a
  // self-owned fallback for isolated embeds and component tests; its identical
  // key still shares the same Solid Query cache and never duplicates a read.
  const fallbackPipeline = createQuery<PipelineDetailPayload>(
    () => {
      const id = agentId();
      return {
        queryKey: publicQueryKeys.agentPipeline(id),
        queryFn: ({ signal }) =>
          getJSON<PipelineDetailPayload>(
            "/public/agent/" + encodeURIComponent(id) + "/pipeline",
            signal,
          ),
        enabled: Boolean(id) && !props.pipeline,
      };
    },
    () => queryClient,
  );
  const pipelineData = (): PipelineDetailPayload | undefined =>
    props.pipeline
      ? props.pipeline()
      : fallbackPipeline.isPending
        ? undefined
        : fallbackPipeline.data;
  const pipelineLoading = (): boolean =>
    props.pipelineLoading ? props.pipelineLoading() : fallbackPipeline.isPending;
  const pipelineFetching = (): boolean =>
    props.pipelineFetching ? props.pipelineFetching() : fallbackPipeline.isFetching;
  const pipelineError = (): unknown =>
    props.pipelineError ? props.pipelineError() : fallbackPipeline.error;
  const retryPipeline = (): void => {
    if (props.retryPipeline) props.retryPipeline();
    else void fallbackPipeline.refetch();
  };

  // Transcript templates + category names; both effectively static payloads.
  const benchConfig = useEndpoint<BenchConfigPayload>("/public/bench/config");
  const glossary = useEndpoint<GlossaryPayload>("/public/bench/glossary");
  const config = (): BenchConfigPayload | undefined => {
    try {
      return benchConfig.error() ? undefined : benchConfig.data();
    } catch {
      return undefined;
    }
  };
  const glossaryData = (): GlossaryPayload | undefined => {
    try {
      return glossary.error() ? undefined : glossary.data();
    } catch {
      return undefined;
    }
  };

  const loadedPipeline = (): PipelineDetailPayload | null => {
    if (pipelineError()) return null;
    return pipelineData() || null;
  };

  // #622/#636 (openActivityEntry 7986–7995): the current review reason leads
  // under its event label; a duplicate claim past the opening event reads as
  // the initial comparison, not live evidence.
  const evidence = (): string => {
    const e = props.entry as ActivityStatusEntry;
    let out = e.review_reason
      ? reviewEvidenceText(e)
      : e.screening_reason
        ? "Screening: " + e.screening_reason
        : "No additional evidence reported.";
    if (e.duplicate_of) {
      out +=
        duplicateComparisonLabel(e) === "Initial comparison"
          ? " Initial comparison: " + duplicateLabel(e) + "."
          : " Compared with " + duplicateLabel(e) + ".";
    }
    return out;
  };

  const current = createMemo(() =>
    pipelineDisplayState(props.entry as ActivityStatusEntry, loadedPipeline()),
  );
  const displayScoreCount = (): number =>
    current().score_count == null ? 0 : Number(current().score_count);
  const displayQuorum = (): number => (current().quorum == null ? 3 : Number(current().quorum));
  // Until the deep history loads, the summary is the whole answer — so it
  // carries the two facts the pipeline payload would otherwise supply: the
  // benchmark runs in flight and the median under the label its score count
  // earns (#648). Both step aside once the history's own headline is there.
  const summaryBenchmarks = (): BenchmarkProgress[] =>
    loadedPipeline() || !Array.isArray(props.entry.active_benchmarks)
      ? []
      : props.entry.active_benchmarks;
  const summaryComposite = (): number | null =>
    !loadedPipeline() && props.entry.score_composite != null ? props.entry.score_composite : null;
  const currentCohort = () => pipelineCurrentCohort(current() as PipelinePayload);
  const validationLabel = () => {
    const cohort = currentCohort();
    return cohort ? benchmarkVersionLabel(cohort.key) + " canonical quorum" : "Canonical quorum";
  };
  const retestFacts = createMemo(() => {
    const detail = loadedPipeline();
    const cohort = currentCohort();
    const retests = retestAttemptCounts(
      ((detail && detail.validation_attempts) || []).filter(
        (attempt) => !cohort || benchmarkVersionKey(attempt.bench_version) === cohort.key,
      ),
    );
    const boardEntry = entries().find((entry) => detail && entry.agent_id === detail.agent_id) as
      | (RankedEntry & BoardConfirmationFields)
      | undefined;
    const acceptedRetests = Math.max(0, Number(boardEntry?.completed_wave_count) || 0);
    const facts: string[] = [];
    if (acceptedRetests)
      facts.push(acceptedRetests + " completed " + (acceptedRetests === 1 ? "wave" : "waves"));
    if (retests.running) facts.push(retests.running + " running");
    if (retests.assigned) facts.push(retests.assigned + " assigned");
    if (retests.expired) facts.push(retests.expired + " expired");
    return facts;
  });

  const preliminary = createMemo(() => {
    const cohort = currentCohort();
    const quorum = current().quorum == null ? 3 : Number(current().quorum);
    if (!cohort || !cohort.scores.length || cohort.scores.length >= quorum) return null;
    const latest = cohort.scores
      .slice()
      .sort(
        (a, b) => new Date(a.accepted_at || 0).getTime() - new Date(b.accepted_at || 0).getTime(),
      )
      .pop() as AcceptedScore;
    return { cohort, quorum, latest };
  });

  const screeningAttempts = () => loadedPipeline()?.screening_attempts || [];
  const currentPolicy = () =>
    screeningAttempts().reduce(
      (latest, attempt) => Math.max(latest, Number(attempt.policy_version) || 0),
      0,
    );
  const currentScreening = () =>
    screeningAttempts().filter((a) => (Number(a.policy_version) || 0) === currentPolicy());
  const oldScreening = () =>
    screeningAttempts().filter((a) => (Number(a.policy_version) || 0) !== currentPolicy());

  const validationCohorts = createMemo(() => {
    const detail = loadedPipeline();
    return detail ? benchmarkCohorts(detail) : [];
  });

  return (
    <>
      <div class="pipeline-detail" data-agent-evidence={agentId()}>
        <Show when={loadedPipeline()}>
          {(detail) => (
            <>
              <ScoreHeadline pipeline={detail()} />
              <FamilyStanding pipeline={detail()} entries={entries} settledView={settled()} />
            </>
          )}
        </Show>
        <div class="pipeline-summary">
          <section class="pipeline-current" aria-labelledby="pipeline-current-title">
            <h4 id="pipeline-current-title">Current progress</h4>
            <p class="pipeline-current-message">
              {validationDetail(current() as ActivityStatusEntry)}
            </p>
            <For each={summaryBenchmarks()}>
              {(progress) => <BenchmarkProgressView progress={progress} />}
            </For>
            <dl class="pipeline-key-facts">
              <div>
                <dt>{validationLabel()}</dt>
                <dd>
                  {displayScoreCount()} of {displayQuorum()}
                </dd>
              </div>
              <Show when={summaryComposite() != null}>
                <div>
                  <dt>
                    {displayScoreCount() >= displayQuorum()
                      ? "Canonical median"
                      : "Preliminary median"}
                  </dt>
                  <dd>{fx(summaryComposite() as number)}</dd>
                </div>
              </Show>
              <Show when={retestFacts().length}>
                <div>
                  <dt>Continual retests</dt>
                  <dd>{retestFacts().join(" · ")}</dd>
                </div>
              </Show>
              <Show when={preliminary()}>
                {(fact) => (
                  <div>
                    <dt>{benchmarkVersionLabel(fact().cohort.key)} preliminary</dt>
                    <dd>
                      <span class="pipeline-preliminary-value">{fx(fact().latest.composite)}</span>
                      <span class="pipeline-preliminary-progress">
                        {fact().cohort.scores.length} of {fact().quorum}
                      </span>
                    </dd>
                  </div>
                )}
              </Show>
              <div>
                <dt>Submitted</dt>
                <dd>{new Date(props.entry.submitted_at || "").toLocaleString()}</dd>
              </div>
            </dl>
          </section>
          <section class="pipeline-meta" aria-labelledby="pipeline-meta-title">
            <h4 id="pipeline-meta-title">Submission details</h4>
            <dl class="pipeline-meta-list">
              <div>
                <dt>Agent</dt>
                <dd>{agentName(props.entry.name)}</dd>
              </div>
              <div>
                <dt>Submission</dt>
                <dd>{agentVersionLabel(props.entry.version)}</dd>
              </div>
              <div>
                <dt>Agent ID</dt>
                <dd>
                  <span class="copyable">
                    <code>
                      <EntityButton kind="agent" id={agentId()} label={agentId()} />
                    </code>
                    <CopyButton value={agentId()} label="agent ID" />
                  </span>
                </dd>
              </div>
              <div>
                <dt>Evidence</dt>
                <dd>{evidence()}</dd>
              </div>
            </dl>
            <div class="review-copy-row">
              <CopyButton
                class="review-copy"
                value={reviewPacket(props.entry)}
                label="review details"
              >
                <span>Copy review details</span>
              </CopyButton>
            </div>
          </section>
        </div>
        <ArtifactReleaseCard
          release={loadedPipeline()?.artifact_release || props.entry.artifact_release}
          agentId={agentId()}
        />
        <Show when={!loadedPipeline() && pipelineLoading()}>
          <section class="pipeline-section-loading" aria-busy="true" aria-live="polite">
            <span class="pipeline-loading-kicker">Source availability</span>
            <span class="pipeline-loading-line" />
          </section>
        </Show>
      </div>
      <section
        class="pipeline-history-region"
        data-agent-history=""
        aria-labelledby="pipeline-history-title"
      >
        <div class="pipeline-history-heading">
          <h4 id="pipeline-history-title">Screening, scores, and validator history</h4>
          <Show when={pipelineFetching() && loadedPipeline()}>
            <span class="pipeline-refreshing">Refreshing</span>
          </Show>
        </div>
        <div
          class="pipeline-history-body"
          data-agent-history-body=""
          aria-busy={pipelineFetching() && !loadedPipeline() ? "true" : undefined}
        >
          <Show
            when={loadedPipeline()}
            fallback={
              <Show
                when={pipelineError()}
                fallback={
                  <div class="pipeline-history-skeleton" role="status">
                    <span>Loading evidence record…</span>
                    <div class="pipeline-skeleton-section" />
                    <div class="pipeline-skeleton-section short" />
                    <div class="pipeline-skeleton-section" />
                  </div>
                }
              >
                <div class="pipeline-history-error" role="alert">
                  <p>Detailed history is temporarily unavailable.</p>
                  <button
                    type="button"
                    class="btn"
                    disabled={pipelineFetching()}
                    onClick={retryPipeline}
                  >
                    {pipelineFetching() ? "Retrying…" : "Retry details"}
                  </button>
                </div>
              </Show>
            }
          >
            {(detail) => (
              <div class="pipeline-history">
                <ScreeningDispute
                  agentId={agentId()}
                  status={detail().status}
                  dispute={detail().dispute}
                  onSubmitted={retryPipeline}
                />
                <section class="pipeline-section" aria-labelledby="pipeline-screening-history">
                  <div class="pipeline-section-heading">
                    <h4 id="pipeline-screening-history">Screener result</h4>
                  </div>
                  <div class="attempt-list">
                    <Show
                      when={currentScreening().length}
                      fallback={
                        <p class="pipeline-detail-state">
                          No versioned screening attempt recorded yet.
                        </p>
                      }
                    >
                      <For each={currentScreening()}>
                        {(attempt) => <ScreeningAttemptRow attempt={attempt} isOld={false} />}
                      </For>
                    </Show>
                    <Show when={oldScreening().length}>
                      <details class="old-screeners">
                        <summary>Old screener results</summary>
                        <For each={oldScreening()}>
                          {(attempt) => <ScreeningAttemptRow attempt={attempt} isOld={true} />}
                        </For>
                      </details>
                    </Show>
                  </div>
                </section>
                <InferenceRuns runs={detail().inference_runs || []} />
                <AcceptedScores pipeline={detail()} config={config} glossary={glossaryData} />
                <ConfirmationScores pipeline={detail()} entries={entries} />
                <section class="pipeline-section" aria-labelledby="pipeline-validator-history">
                  <div class="pipeline-section-heading">
                    <h4 id="pipeline-validator-history">Validator progress</h4>
                  </div>
                  <div class="benchmark-cohort-list">
                    <Show
                      when={validationCohorts().length}
                      fallback={<p class="pipeline-detail-state">No validator is assigned yet.</p>}
                    >
                      <For each={validationCohorts()}>
                        {(cohort) => (
                          <div class="benchmark-cohort">
                            <div class="benchmark-cohort-heading">
                              <h5>{benchmarkVersionLabel(cohort.key)}</h5>
                              <span class="benchmark-cohort-summary">
                                {cohortProgressSummary(cohort, detail().quorum)}
                              </span>
                            </div>
                            <div class="attempt-list">
                              <Show
                                when={cohort.attempts.length}
                                fallback={
                                  <p class="pipeline-detail-state">
                                    No validator assignment history is available for this benchmark
                                    version.
                                  </p>
                                }
                              >
                                <For each={cohort.attempts}>
                                  {(attempt) => <ValidationAttemptRow attempt={attempt} />}
                                </For>
                              </Show>
                            </div>
                          </div>
                        )}
                      </For>
                    </Show>
                  </div>
                </section>
              </div>
            )}
          </Show>
        </div>
      </section>
    </>
  );
}
