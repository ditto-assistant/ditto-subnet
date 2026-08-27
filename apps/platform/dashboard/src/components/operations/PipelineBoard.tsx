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
import type { BenchmarkProgress } from "../../types/pipeline";
import {
  BenchmarkProgressView,
  PipelineScreenerProgressView,
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

function PipelineCard(props: {
  item: IndexedEntry;
  column: string;
  screeners: FleetReport | null;
  activeVersion: number | null;
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
  const ariaLabel = () =>
    "View " +
    accessibleName() +
    " details" +
    (isUpNext() ? ", up next for validator assignment" : "") +
    (queueGate() ? ", " + queueGate()?.aria : "") +
    (rescore()?.isQualification ? ", inherited benchmark cohort qualification in progress" : "") +
    (admissionLabel() ? ", " + admissionLabel() : "");
  const benchmarks = (): BenchmarkProgress[] =>
    props.column === "evaluating" ? entry().active_benchmarks || [] : [];
  return (
    <a
      class="pipeline-item"
      href={entityHref("agent", String(entry().agent_id || ""))}
      data-entity-link="agent"
      data-pipeline-i={props.item.index}
      aria-label={ariaLabel()}
      onClick={(ev) => cardClick(ev, String(entry().agent_id || ""))}
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
      <Show when={admissionLabel()}>
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
              </div>
              <div class="pipeline-items" id={column().def.bodyId}>
                <Show
                  when={!props.unavailable}
                  fallback={<div class="pipeline-empty">Queue unavailable.</div>}
                >
                  <Show when={!props.loading} fallback={<div class="pipeline-empty">Loading…</div>}>
                    <Show
                      when={items().length}
                      fallback={<div class="pipeline-empty">{column().def.empty}</div>}
                    >
                      <For each={items()}>
                        {(item) => (
                          <PipelineCard
                            item={item}
                            column={column().def.status}
                            screeners={props.screeners}
                            activeVersion={props.activeVersion}
                          />
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
  return (
    <details class="pipeline-review-branch" aria-labelledby="pipeline-review-title">
      <summary class="pipeline-review-summary">
        <span>
          <span class="pipeline-review-eyebrow">Conditional after scoring</span>
          <strong class="pipeline-review-title" id="pipeline-review-title">
            Source integrity review
          </strong>
        </span>
        <span class="pipeline-review-count" id="pipeline-review-count">
          {props.unavailable || props.loading ? "–" : String(view().count)}
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
              when={view().shown.length > 0}
              fallback={
                <div class="pipeline-empty">No submissions are held for integrity review.</div>
              }
            >
              <For each={view().shown}>
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
