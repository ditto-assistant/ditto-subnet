// The live submission-queue board and the policy-rescreen notice (monolith
// markup 2761–2800, renderPipelineBoard 7976–8106, renderPolicyRescreenNotice
// 6808–6832). Data comes from ONE shared /public/operations snapshot; the
// screener feed only decorates Screening cards with live stages.
import { For, Index, Show, createMemo, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { reconciledList } from "../../data/reconciled";
import { agentName, agentVersionLabel, fx, relTime } from "../../lib/format";
import { entityHref } from "../../lib/router";
import { pushEntityRoute } from "../../stores/routeStore";
import { HandleBadge } from "../ui/HandleBadge";
import { MinerAvatar } from "../ui/MinerAvatar";
import { policyScreeningLabel } from "../pipeline/status";
import type { FleetReport } from "../../types/fleet";
import type { CodingShadowScore } from "../../types/leaderboard";
import type { BenchmarkProgress } from "../../types/pipeline";
import {
  AdmissionStepTrack,
  BenchmarkProgressView,
  ElapsedTime,
  benchmarkProgressText,
  benchmarkStageLabel,
  PipelineScreenerProgressView,
  ReviewStageLadder,
  admissionSteps,
  reviewStageIndex,
  screenerStageLabel,
} from "./progress";
import {
  activeScreenerFor,
  integrityReviewReason,
  integrityReviewView,
  pipelineAgentVersionLabel,
  pipelineColumnViews,
  pipelineRescoreState,
  policyRescreenView,
  queueGateLabel,
} from "./pipeline";
import type { IndexedEntry, PipelineColumnView, PipelineEntryExt } from "./pipeline";

/** Screening backlog after a policy bump is intentional, not data loss —
 * the notice explains it from public queue state alone. */
export function RescreenNotice(props: {
  entries: PipelineEntryExt[];
  unavailable: boolean;
}): JSX.Element {
  const view = createMemo(() => policyRescreenView(props.entries, props.unavailable));
  return (
    <div
      id="rescreen-notice"
      class="rescreen-notice"
      role="status"
      aria-live="polite"
      hidden={!view()}
    >
      <span class="rescreen-mark" aria-hidden="true">
        <svg class="ic" viewBox="0 0 24 24">
          <path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5" />
          <path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5" />
        </svg>
      </span>
      <div>
        <div class="rescreen-heading">
          <strong id="rescreen-title">
            {view()
              ? "Policy v" + view()?.requiredPolicy + " rescreen in progress"
              : "Screening policy refresh"}
          </strong>
          <span class="rescreen-policy" id="rescreen-policy">
            {view() ? "LIVE API STATE" : ""}
          </span>
        </div>
        <p class="rescreen-copy" id="rescreen-copy">
          {view()
            ? "Existing submissions are being rechecked under the current screening policy. " +
              "Prior scores remain preserved, and validators may intentionally idle until " +
              "lower-score submissions clear screening. This is not data loss."
            : ""}
        </p>
        <div class="rescreen-facts" aria-label="Policy rescreen totals">
          <span>
            <b id="rescreen-count">{view()?.count ?? 0}</b> confirmed rescreens
          </span>
          <span>
            <b id="rescreen-scored">{view()?.scored ?? 0}</b> with scores on record
          </span>
        </div>
      </div>
    </div>
  );
}

export interface PipelineBoardProps {
  entries: PipelineEntryExt[];
  statusCounts: Record<string, number>;
  unavailable: boolean;
  /** True before the first snapshot lands (static "Loading…" placeholders). */
  loading: boolean;
  screeners: FleetReport | null;
  activeVersion: number | null;
  /** The Scoring card whose runs the page's detail panel is showing. */
  selectedScoringKey?: string | null;
  onSelectScoring?: (key: string) => void;
}

function cardClick(ev: MouseEvent, agentId: string): void {
  if (
    ev.defaultPrevented ||
    ev.button !== 0 ||
    ev.metaKey ||
    ev.ctrlKey ||
    ev.shiftKey ||
    ev.altKey
  )
    return;
  ev.preventDefault();
  pushEntityRoute("agent", agentId);
}

function codingShadowStatus(coding: CodingShadowScore): string {
  if (coding.status === "complete" && coding.score != null) {
    return coding.score === 0 ? "0.000 measured" : fx(coding.score);
  }
  if (coding.status === "collecting") {
    return "Collecting " + coding.result_count + "/" + coding.score_quorum;
  }
  if (coding.status === "scheduled") return "Scheduled";
  return "Stale";
}

function CodingShadowPipelineItem(props: { coding: CodingShadowScore }): JSX.Element {
  const result = () => props.coding;
  return (
    <span class="pipeline-coding-shadow" data-coding-status={result().status}>
      <span class="pipeline-coding-shadow-heading">
        <strong>Coding shadow</strong>
        <span>{codingShadowStatus(result())}</span>
      </span>
      <span class="pipeline-coding-shadow-detail">
        {result().result_count}/{result().score_quorum} validators · Coding v
        {result().coding_contract_version} · Bench v{result().bench_version}
        {result().status === "stale" ? " · not carried forward" : ""}
      </span>
      <span class="pipeline-coding-shadow-boundary">
        Parallel display only · does not delay Scored &amp; live
      </span>
    </span>
  );
}

function CodingShadowLane(props: {
  entries: PipelineEntryExt[];
  unavailable: boolean;
  loading: boolean;
}): JSX.Element {
  const results = createMemo(() =>
    props.entries.flatMap((entry) => (entry.coding_shadow ? [entry.coding_shadow] : [])),
  );
  const active = createMemo(
    () =>
      results().filter((result) => result.status === "scheduled" || result.status === "collecting")
        .length,
  );
  const complete = createMemo(
    () => results().filter((result) => result.status === "complete").length,
  );
  const stale = createMemo(() => results().filter((result) => result.status === "stale").length);
  const status = (): string => {
    if (props.unavailable) return "Coding shadow status unavailable.";
    if (props.loading) return "Loading Coding shadow status…";
    if (!results().length) return "No Coding shadow evaluations in this snapshot.";
    return active() + " active · " + complete() + " complete · " + stale() + " stale";
  };
  return (
    <div class="pipeline-coding-lane" role="status" aria-live="polite">
      <span class="pipeline-coding-lane-heading">
        <strong>Coding shadow</strong>
        <span>Parallel · weight zero</span>
      </span>
      <span class="pipeline-coding-lane-status">{status()}</span>
      <span class="pipeline-coding-lane-boundary">Never blocks the core scoring pipeline.</span>
    </div>
  );
}

/** A plain click on a Scoring card selects it for the run-progress panel and
 * opens the agent modal. Modified clicks keep the link's native new-tab
 * behaviour. */
function selectClick(ev: MouseEvent, onSelect: () => void, agentId: string): void {
  if (
    ev.defaultPrevented ||
    ev.button !== 0 ||
    ev.metaKey ||
    ev.ctrlKey ||
    ev.shiftKey ||
    ev.altKey
  )
    return;
  ev.preventDefault();
  onSelect();
  pushEntityRoute("agent", agentId);
}

function PipelineCard(props: {
  item: IndexedEntry;
  column: string;
  screeners: FleetReport | null;
  activeVersion: number | null;
  /** Present only where the page renders the run-progress panel. */
  onSelect?: () => void;
  selected?: boolean;
}): JSX.Element {
  const entry = () => props.item.entry;
  // Rank 1 is not the same as "next". A row in a gated lane holds the top of
  // a line that is not moving (#458): the gate is authoritative and rank
  // only orders within it.
  const queueGate = () => queueGateLabel(entry());
  const isUpNext = () =>
    props.column === "waiting_validator" &&
    Number(entry().validator_queue_rank) === 1 &&
    !queueGate();
  const rescore = () =>
    props.column === "evaluating" ? pipelineRescoreState(entry(), props.activeVersion) : null;
  // Frozen inherited-cohort rollout position (weekend drift, 8276–8310).
  const rolloutVersion = () => Number(entry().queue_bench_version) || null;
  const rolloutPosition = () => Number(entry().rollout_position) || null;
  const meta = () => {
    if (
      props.column === "waiting_validator" ||
      props.column === "evaluating" ||
      entry().status === "below_score_floor"
    ) {
      const version = rolloutVersion();
      if (version) return "Bench v" + version + " · " + validationProgress(entry());
      const state = rescore();
      if (state) {
        return state.isQualification
          ? "Bench v" + state.targetVersion + " qualification"
          : "Bench v" + state.targetVersion + " rescore";
      }
      return validationProgress(entry());
    }
    return relTime(
      props.column === "scored" && entry().last_scored_at
        ? entry().last_scored_at
        : entry().submitted_at,
    );
  };
  const screener = () =>
    props.column === "admission" && entry().status === "screening"
      ? activeScreenerFor(props.screeners, entry().agent_id)
      : null;
  const screeningLabel = () => {
    const active = screener();
    return active ? screenerStageLabel(active.screening_progress?.stage) : "";
  };
  // The screener heartbeat's L4 bucket is its final adjudication. It is an
  // automated decision step, not an operator hold or a serial manual queue.
  const automatedFinalAdjudication = () =>
    reviewStageIndex(screener()?.screening_progress?.stage) === 3;
  const admissionLabel = () => {
    if (props.column !== "admission") return "";
    if (entry().status === "waiting_screening") return "Waiting for admission";
    return screeningLabel() || "Building image & admission";
  };
  const policyLabel = () => (props.column === "admission" ? policyScreeningLabel(entry()) : "");
  const provisionalScore = () =>
    props.column === "waiting_validator" && entry().provisional_composite != null
      ? "Provisional " + fx(Number(entry().provisional_composite))
      : "";
  const accessibleName = () => agentName(entry().name) + ", " + agentVersionLabel(entry().version);
  const codingAria = () => {
    if (props.column !== "evaluating" && props.column !== "scored") return "";
    const coding = entry().coding_shadow;
    return coding
      ? ", Coding shadow " + codingShadowStatus(coding) + ", parallel display only"
      : "";
  };
  const ariaLabel = () =>
    "View " +
    accessibleName() +
    " details" +
    (isUpNext() ? ", up next for validator assignment" : "") +
    (queueGate() ? ", " + queueGate()?.aria : "") +
    (rescore()?.isQualification ? ", inherited benchmark cohort qualification in progress" : "") +
    (admissionLabel() ? ", " + admissionLabel() : "") +
    codingAria();
  const benchmarks = (): BenchmarkProgress[] =>
    props.column === "evaluating" ? entry().active_benchmarks || [] : [];
  // Waiting vs in-progress is a state the card must wear, not just say: the
  // attribute drives the muted queued treatment and the active gold rail.
  const admissionState = () =>
    props.column === "admission"
      ? entry().status === "waiting_screening"
        ? "waiting"
        : "active"
      : undefined;
  return (
    <a
      class="pipeline-item"
      href={entityHref("agent", String(entry().agent_id || ""))}
      data-entity-link="agent"
      data-pipeline-i={props.item.index}
      data-admission={admissionState()}
      aria-label={
        props.onSelect ? "Show run progress and details for " + accessibleName() : ariaLabel()
      }
      aria-current={props.selected ? "true" : undefined}
      onClick={(ev) => {
        const select = props.onSelect;
        const agentId = String(entry().agent_id || "");
        if (select) selectClick(ev, select, agentId);
        else cardClick(ev, agentId);
      }}
    >
      <span class="pipeline-item-heading">
        <span class="pipeline-item-identity">
          <span class="pipeline-item-name">
            <MinerAvatar url={entry().avatar_url} />
            {agentName(entry().name)}
            <HandleBadge handle={entry().name_handle} />
          </span>
          <span class="pipeline-item-version">{pipelineAgentVersionLabel(entry().version)}</span>
        </span>
        <span class="pipeline-item-meta">{meta()}</span>
      </span>
      <Show when={props.selected}>
        <span class="pipeline-selected-chip">Selected</span>
      </Show>
      <Show when={props.column === "admission"}>
        <AdmissionStepTrack
          steps={admissionSteps(entry().screening_build_only)}
          stage={screener()?.screening_progress?.stage ?? null}
          waiting={entry().status === "waiting_screening"}
        />
        {/* Source review is one segment of the track above and most of its
            wall-clock; the ladder opens that segment into the four stages
            the screener actually runs. */}
        <ReviewStageLadder stage={screener()?.screening_progress?.stage ?? null} />
        <Show when={automatedFinalAdjudication()}>
          <span
            class="pipeline-automated-adjudication"
            role="status"
            title="The screener is automatically completing its final adjudication."
          >
            Automated final adjudication · running
          </span>
        </Show>
      </Show>
      <Show when={isUpNext() || queueGate() || rolloutPosition() || rescore()?.isQualification}>
        <span class="pipeline-item-badges">
          <Show when={isUpNext()}>
            <span
              class="pipeline-next-badge"
              title="Highest current priority; validator eligibility can vary"
            >
              Up next
            </span>
          </Show>
          <Show when={queueGate()}>
            {(gate) => (
              <span class="pipeline-gate-badge" title={gate().title}>
                {gate().label}
              </span>
            )}
          </Show>
          <Show when={rolloutPosition()}>
            <span
              class="pipeline-qualification-badge"
              title="Frozen inherited benchmark rollout position"
            >
              Cohort #{rolloutPosition()} → v{rolloutVersion()}
            </span>
          </Show>
          <Show when={!rolloutPosition() && rescore()?.isQualification}>
            <span
              class="pipeline-qualification-badge"
              title="Existing score remains authoritative while this inherited cohort agent qualifies on the next benchmark"
            >
              Cohort → v{rescore()?.targetVersion}
            </span>
          </Show>
        </span>
      </Show>
      <Show when={rolloutPosition()}>
        <span class="pipeline-item-qualification-detail">
          v{props.activeVersion} score stays live until v{rolloutVersion()} quorum
        </span>
      </Show>
      <Show when={!rolloutPosition() && rescore()?.isQualification}>
        <span class="pipeline-item-qualification-detail">
          v{rescore()?.sourceVersion} score stays live until v{rescore()?.targetVersion} quorum
        </span>
      </Show>
      <Show when={provisionalScore()}>
        <span class="pipeline-item-priority-detail">{provisionalScore()}</span>
      </Show>
      <Show
        when={
          props.column === "evaluating" || props.column === "scored" ? entry().coding_shadow : null
        }
      >
        {(coding) => <CodingShadowPipelineItem coding={coding()} />}
      </Show>
      {/* One stage line per card: when a screener is reporting, the progress
          view below says the same thing plus how long it has been there, so
          rendering this too printed the stage twice. */}
      <Show when={admissionLabel() && !screener()?.screening_progress}>
        <span class="pipeline-admission-state">{admissionLabel()}</span>
      </Show>
      <Show when={policyLabel()}>
        <span class="pipeline-item-policy">{policyLabel()}</span>
      </Show>
      <RetryChip entry={entry()} />
      <Show when={screener()?.screening_progress}>
        {(progress) => <PipelineScreenerProgressView progress={progress()} />}
      </Show>
      <For each={benchmarks()}>
        {(progress) => <BenchmarkProgressView progress={progress} showAgent={false} />}
      </For>
    </a>
  );
}

/** Flag a waiting submission whose retry state needs a human (exhausted) or
 * is waiting out a cooldown; other states advance on their own (7926–7935). */
function RetryChip(props: { entry: PipelineEntryExt }): JSX.Element {
  return (
    <>
      <Show when={props.entry.retry_state === "exhausted"}>
        <span
          class="retry-chip exhausted"
          title="Every remaining validator spent its retry budget. Grant a retry after verified infrastructure failure, or withdraw an agent-attributable exhaustion from the validator queue."
        >
          Stuck · needs operator
        </span>
      </Show>
      <Show when={props.entry.retry_state === "cooling_down"}>
        <span
          class="retry-chip cooling"
          title="A validator failed here and is waiting out the retry cooldown; it will retry automatically."
        >
          Cooling down
          {props.entry.retry_after ? " · " + relTime(props.entry.retry_after) : ""}
        </span>
      </Show>
    </>
  );
}

/** Quorum progress line (validationProgress 6790–6798). */
function validationProgress(e: PipelineEntryExt): string {
  const count = Math.max(0, Number(e.score_count) || 0);
  const quorum = Math.max(1, Number(e.quorum) || 3);
  if (e.status === "below_score_floor") return count + " of " + quorum + " · queued last";
  if (e.status === "not_queued") return "Not in active benchmark queue";
  if (e.status === "retired") return count + " of " + quorum + " · benchmark closed";
  const hasStarted =
    count > 0 ||
    ["waiting_validator", "evaluating", "scored", "live", "under_review"].indexOf(
      String(e.status),
    ) >= 0;
  return hasStarted ? count + " of " + quorum : "Not started";
}

export function PipelineBoard(props: PipelineBoardProps): JSX.Element {
  const [showStuck, setShowStuck] = createSignal(false);
  const columns = createMemo<PipelineColumnView[]>(() =>
    pipelineColumnViews(
      props.unavailable || props.loading ? [] : props.entries,
      props.statusCounts,
      showStuck(),
      props.activeVersion,
    ),
  );
  return (
    <div class="pipeline-overview" id="pipeline-overview" aria-label="Live submission queues">
      {/* Index, not For: the board is the four fixed pipeline stages and what
          changes every 5s tick is each stage's contents, not the set of
          stages. Keyed by reference, <For> saw a wholly new view object per
          column on every /public/operations poll and rebuilt all four
          <section>s — which threw away the .pipeline-items scroll container
          itself, so a reader looking down a queue was returned to the top
          every few seconds. */}
      <Index each={columns()}>
        {(column) => {
          // Reconciled per lane so a poll that re-reports the same submission
          // keeps that card's node — otherwise the lane's <For> replaces
          // every card each tick, which clamps the container's scrollTop back
          // to the top even though the container itself now survives.
          const items = reconciledList(() => column().items, "key");
          // Where the admission lane's queued group starts (the lane is
          // sorted active-first); a divider only makes sense between groups.
          const firstWaitingIndex = () =>
            column().def.status === "admission"
              ? items().findIndex((item) => item.entry.status === "waiting_screening")
              : -1;
          const admissionSplit = () => {
            if (column().def.status !== "admission" || props.unavailable || props.loading) {
              return "";
            }
            const active = Number(props.statusCounts.screening || 0);
            const queued = Number(props.statusCounts.waiting_screening || 0);
            if (active + queued <= 0) return "";
            return active + " in progress · " + queued + " queued";
          };
          // The count and the item window are reconciled independently. Keep
          // active admission work visible if a delayed snapshot has the count
          // before its submission record; a blank lane would falsely read as
          // no work. Once the record arrives this notice is replaced by its
          // normal card (and, during L4, the automated-adjudication status).
          const unlistedAdmissionWork = () => {
            if (column().def.status !== "admission" || props.unavailable || props.loading) {
              return 0;
            }
            const listed = items().filter((item) => item.entry.status === "screening").length;
            return Math.max(0, Number(props.statusCounts.screening || 0) - listed);
          };
          return (
            <section
              class="pipeline-column"
              data-pipeline-stage={column().def.status}
              aria-labelledby={column().def.titleId}
              data-active={
                props.unavailable || props.loading ? undefined : column().active ? "true" : "false"
              }
            >
              <div class="pipeline-node" aria-hidden="true">
                {column().def.node}
              </div>
              <div class="pipeline-column-head">
                <h3 id={column().def.titleId}>{column().def.title}</h3>
                <span class="pipeline-count" id={column().def.countId}>
                  <Show when={!props.unavailable && !props.loading} fallback={"–"}>
                    {String(column().displayedCount)}
                    <Show when={column().stuckCount > 0}>
                      <button
                        type="button"
                        class="pipeline-stuck-count"
                        data-pipeline-stuck-filter
                        aria-pressed={showStuck() ? "true" : "false"}
                        title={
                          (showStuck() ? "Show actionable queue. " : "Show stuck submissions. ") +
                          column().stuckCount +
                          " submission" +
                          (column().stuckCount === 1 ? "" : "s") +
                          " exhausted the validator retry budget and need an operator retry grant."
                        }
                        onClick={() => setShowStuck((value) => !value)}
                      >
                        {column().stuckCount} stuck
                      </button>
                    </Show>
                  </Show>
                </span>
                <Show when={admissionSplit()}>
                  <span class="pipeline-count-detail">{admissionSplit()}</span>
                </Show>
              </div>
              <Show
                when={
                  column().def.status === "scored" &&
                  !props.unavailable &&
                  !props.loading &&
                  items().length
                }
              >
                <div class="pipeline-items-caption">Recent completions</div>
              </Show>
              <div class="pipeline-items" id={column().def.bodyId}>
                <Show when={column().def.status === "evaluating"}>
                  <CodingShadowLane
                    entries={props.entries}
                    unavailable={props.unavailable}
                    loading={props.loading}
                  />
                </Show>
                <Show
                  when={!props.unavailable}
                  fallback={<div class="pipeline-empty">Queue unavailable.</div>}
                >
                  <Show when={!props.loading} fallback={<div class="pipeline-empty">Loading…</div>}>
                    <Show
                      when={items().length}
                      fallback={
                        <Show
                          when={unlistedAdmissionWork()}
                          fallback={<div class="pipeline-empty">{column().def.empty}</div>}
                        >
                          <div class="pipeline-unlisted-admission" role="status">
                            {unlistedAdmissionWork()} active admission
                            {unlistedAdmissionWork() === 1 ? " is" : "s are"} in progress.
                            Submission details are awaiting the next pipeline snapshot.
                          </div>
                        </Show>
                      }
                    >
                      <For each={items()}>
                        {(item, index) => (
                          <>
                            <Show when={index() === firstWaitingIndex() && index() > 0}>
                              {/* Cards above are being screened right now;
                                  everything below is queued. The per-card
                                  aria-labels already say so. */}
                              <div class="pipeline-lane-divider" aria-hidden="true">
                                Waiting for a screener
                              </div>
                            </Show>
                            <PipelineCard
                              item={item}
                              column={column().def.status}
                              screeners={props.screeners}
                              activeVersion={props.activeVersion}
                              selected={
                                column().def.status === "evaluating" &&
                                props.selectedScoringKey === item.key
                              }
                              onSelect={
                                column().def.status === "evaluating" && props.onSelectScoring
                                  ? () => props.onSelectScoring?.(item.key)
                                  : undefined
                              }
                            />
                          </>
                        )}
                      </For>
                      <Show when={column().hiddenCount > 0}>
                        <div class="pipeline-more">
                          {column().hiddenCount +
                            " older " +
                            (column().hiddenCount === 1 ? "submission" : "submissions") +
                            " in Activity"}
                        </div>
                      </Show>
                    </Show>
                  </Show>
                </Show>
              </div>
            </section>
          );
        }}
      </Index>
    </div>
  );
}

/** The conditional post-scoring source-integrity branch (weekend drift
 * #623/#635; markup 2833–2838, renderIntegrityReviewBranch 8330–8359). Only
 * leaderboard qualifiers and robust anomaly holds enter it — the aside says
 * so instead of implying every submission passes through review. */
export function IntegrityReviewBranch(props: {
  entries: PipelineEntryExt[];
  statusCounts: Record<string, number>;
  unavailable: boolean;
  loading: boolean;
}): JSX.Element {
  const view = createMemo(() => integrityReviewView(props.entries, props.statusCounts));
  // Same 5s snapshot as the lanes: reconciled so a held submission that is
  // still held keeps its row rather than being replaced under the reader.
  const shown = reconciledList(() => view().shown, "key");
  return (
    <details class="pipeline-review-branch" aria-labelledby="pipeline-review-title">
      <summary class="pipeline-review-summary">
        <span class="pipeline-review-mark" aria-hidden="true">
          <svg class="ic" viewBox="0 0 24 24">
            <path d="M12 3 4 6v6c0 4.6 3.3 7.8 8 9 4.7-1.2 8-4.4 8-9V6Z" />
            <path d="m8.5 12 2.5 2.5 4.5-5" />
          </svg>
        </span>
        <span class="pipeline-review-heading">
          <span class="pipeline-review-eyebrow">Conditional after scoring</span>
          <strong class="pipeline-review-title" id="pipeline-review-title">
            Source integrity review
          </strong>
        </span>
        <span class="pipeline-review-count" id="pipeline-review-count">
          {props.unavailable || props.loading ? "–" : String(view().count)}
        </span>
        <span class="pipeline-review-toggle" aria-hidden="true">
          <span class="pipeline-review-toggle-open">Expand</span>
          <span class="pipeline-review-toggle-close">Collapse</span>
          <svg class="ic" viewBox="0 0 24 24">
            <path d="m9 6 6 6-6 6" />
          </svg>
        </span>
      </summary>
      <p class="pipeline-review-copy">
        Only leaderboard qualifiers and robust anomaly holds enter this branch. Other admitted
        submissions go directly through validator scoring.
      </p>
      <div class="pipeline-review-items" id="pipeline-review-items">
        <Show
          when={!props.unavailable}
          fallback={<div class="pipeline-empty">Review state unavailable.</div>}
        >
          <Show when={!props.loading} fallback={<div class="pipeline-empty">Loading…</div>}>
            <Show
              when={shown().length > 0}
              fallback={
                <div class="pipeline-empty">No submissions are held for integrity review.</div>
              }
            >
              <For each={shown()}>
                {(item) => (
                  <a
                    class="pipeline-item"
                    href={entityHref("agent", String(item.entry.agent_id || ""))}
                    data-entity-link="agent"
                    data-pipeline-i={item.index}
                    aria-label={
                      "View " +
                      agentName(item.entry.name) +
                      ", " +
                      agentVersionLabel(item.entry.version) +
                      " integrity review details"
                    }
                    onClick={(ev) => cardClick(ev, String(item.entry.agent_id || ""))}
                  >
                    <span class="pipeline-item-heading">
                      <span class="pipeline-item-identity">
                        <span class="pipeline-item-name">{agentName(item.entry.name)}</span>
                        <span class="pipeline-item-version">
                          {pipelineAgentVersionLabel(item.entry.version)}
                        </span>
                      </span>
                      <span class="pipeline-item-meta">
                        {relTime(
                          (item.entry as { review_opened_at?: string | null }).review_opened_at ||
                            item.entry.submitted_at,
                        )}
                      </span>
                    </span>
                    <span class="pipeline-item-priority-detail">
                      {integrityReviewReason(item.entry)}
                    </span>
                  </a>
                )}
              </For>
              <Show when={view().moreCount > 0}>
                <div class="pipeline-more">{view().moreCount} more in Activity</div>
              </Show>
            </Show>
          </Show>
        </Show>
      </div>
    </details>
  );
}

/** The selected Scoring submission's runs, one row per active validator slot.
 * Runs are numbered, never attributed: validator identity stays off the
 * public board, exactly as it does in the cards above. */
export function ScoringDetail(props: {
  item: IndexedEntry | null;
  onClose: () => void;
}): JSX.Element {
  return (
    <Show when={props.item}>
      {(item) => {
        const entry = () => item().entry;
        const runs = (): BenchmarkProgress[] => entry().active_benchmarks || [];
        const lead = () => runs().find((run) => run.stage) ?? runs()[0] ?? null;
        const bench = () =>
          runs().find((run) => run.bench_version)?.bench_version ??
          entry().queue_bench_version ??
          null;
        const count = () => Math.max(0, Number(entry().score_count) || 0);
        const quorum = () => Math.max(1, Number(entry().quorum) || 3);
        const agentId = () => String(entry().agent_id || "");
        return (
          <section class="scoring-detail" aria-labelledby="scoring-detail-title">
            <header class="scoring-detail-head">
              <MinerAvatar url={entry().avatar_url} size="lg" />
              <div class="scoring-detail-identity">
                <h3 id="scoring-detail-title">{agentName(entry().name)}</h3>
                <span class="scoring-detail-sub">
                  {pipelineAgentVersionLabel(entry().version)}
                  <Show when={bench()}>
                    <span class="scoring-detail-bench">Bench v{bench()}</span>
                  </Show>
                </span>
              </div>
              <span class="scoring-detail-stage">{benchmarkStageLabel(lead()?.stage)}</span>
              <div class="scoring-detail-quorum">
                <span>Validator quorum</span>
                <strong>
                  {count()} of {quorum()}
                </strong>
              </div>
              <a
                class="scoring-detail-open"
                href={entityHref("agent", agentId())}
                data-entity-link="agent"
                onClick={(ev) => cardClick(ev, agentId())}
              >
                Full history
              </a>
              <button
                type="button"
                class="scoring-detail-close"
                aria-label="Close run progress"
                onClick={() => props.onClose()}
              >
                <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M6 6l12 12M18 6 6 18" />
                </svg>
              </button>
            </header>
            <Show
              when={runs().length}
              fallback={<p class="scoring-detail-empty">No run progress reported yet.</p>}
            >
              <ol class="scoring-detail-runs">
                <For each={runs()}>
                  {(run, index) => {
                    const determinate = () => run.percent != null && !run.stalled;
                    const label = () => "Run " + (index() + 1) + ": " + benchmarkProgressText(run);
                    return (
                      <li
                        class="scoring-run"
                        classList={{
                          stalled: Boolean(run.stalled),
                          failed: run.stage === "failed_retrying",
                        }}
                      >
                        <span class="scoring-run-label">Run {index() + 1}</span>
                        <Show
                          when={determinate()}
                          fallback={
                            <span class="bench-bar indeterminate" role="img" aria-label={label()}>
                              <i />
                            </span>
                          }
                        >
                          <progress
                            max="100"
                            value={Math.max(0, Math.min(100, Number(run.percent) || 0))}
                            aria-label={label()}
                          />
                        </Show>
                        <span class="scoring-run-pct">
                          {run.percent != null ? run.percent + "%" : ""}
                        </span>
                        <span class="scoring-run-checks">
                          {run.completed_checks != null && run.total_checks != null
                            ? run.completed_checks + " of " + run.total_checks + " checks"
                            : benchmarkStageLabel(run.stage)}
                        </span>
                        <Show when={run.started_at}>
                          {(startedAt) => (
                            <ElapsedTime class="scoring-run-elapsed" startedAt={startedAt()} />
                          )}
                        </Show>
                      </li>
                    );
                  }}
                </For>
              </ol>
            </Show>
          </section>
        );
      }}
    </Show>
  );
}
