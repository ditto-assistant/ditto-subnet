// The public miner-facing ATH review queue (monolith markup 2948–2976,
// renderAthReviews 9548–9597, loadAthReviews 9610–9645, 15s label re-age
// 9647–9649): intro + outcomes explainer, the three summary metrics, and
// the held-submission cards. Scores stay preserved; emission eligibility
// pauses until the review resolves. #622: each card leads with the CURRENT
// operator reason (a reopened review names itself), keeping the initial
// hold reason as history when it differs. Nothing here carries admin state
// or auth — it is the same public activity feed every miner can read.
import { For, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { agentCardOpen, hydrateOnAgentCardClose } from "../../data/useEndpoint";
import { REFRESH_MS } from "../../lib/config";
import { agentLabel, athDate, fx, relTime, shortKey } from "../../lib/format";
import type { AthReview, AthSnapshot } from "../../types/pipeline";
import { CopyButton } from "../shell/CopyButton";
import { EntityButton } from "../ui/EntityButton";
import { HandleBadge } from "../ui/HandleBadge";
import { MinerAvatar } from "../ui/MinerAvatar";
import { athReviewSnapshot, athSnapshotLabel } from "./ath";

interface QueueState {
  snapshot: AthSnapshot | null;
  refreshFailed: boolean;
  loading: boolean;
}

/** Wire fields the drifted card reads beyond the base ATH row (#622). */
type AthReviewEntry = AthReview & {
  review_event?: string | null;
  review_original_reason?: string | null;
};

function ReviewCard(props: { entry: AthReviewEntry }): JSX.Element {
  const e = () => props.entry;
  const agentId = () => String(e().agent_id || "");
  const hotkey = () => String(e().miner_hotkey || "");
  const compositeLabel = () => {
    const composite = e().preserved_composite == null ? NaN : Number(e().preserved_composite);
    return Number.isFinite(composite) ? fx(composite) : "–";
  };
  const scoreCount = () => Number(e().score_count) || 0;
  const reason = () => e().review_reason || "This submission was routed to ATH review.";
  const eventLabel = () =>
    e().review_event === "reopened" ? "ATH review reopened" : "ATH review pending";
  // The initial hold reason is history, not the live reason — shown only
  // when an operator has since replaced it (#622).
  const initialHold = () => {
    const original = e().review_original_reason;
    return original && original !== reason() ? original : null;
  };
  return (
    <article
      class="ath-review-card"
      aria-label={"ATH review for " + agentLabel(e().name, e().version)}
    >
      <div class="ath-review-main">
        <div>
          <div class="ath-agent-name">
            <MinerAvatar url={e().avatar_url} />
            <EntityButton kind="agent" id={agentId()} label={agentLabel(e().name, e().version)} />
            <HandleBadge handle={e().name_handle} />
          </div>
          <div class="ath-id-line">
            <code>{agentId()}</code>
            <CopyButton value={agentId()} label="agent ID" />
          </div>
          <div class="ath-hotkey">
            <span>Miner</span>
            <span title={hotkey()}>
              <EntityButton kind="miner" id={hotkey()} label={shortKey(hotkey())} />
            </span>
            <CopyButton value={hotkey()} label="miner hotkey" />
          </div>
        </div>
        <div>
          <span class="ath-review-status">{eventLabel()}</span>
          <dl class="ath-time-grid">
            <dt>Submitted</dt>
            <dd>
              {athDate(e().submitted_at)} · {relTime(e().submitted_at)}
            </dd>
            <dt>Held</dt>
            <dd>
              {athDate(e().review_opened_at)}
              {e().review_opened_at ? " · " + relTime(e().review_opened_at) : ""}
            </dd>
          </dl>
        </div>
        <div class="ath-score">
          <div>
            <span>Scores recorded</span>
            <strong>{scoreCount()}</strong>
          </div>
          <div>
            <span>Composite</span>
            <strong>{compositeLabel()}</strong>
          </div>
        </div>
      </div>
      <div class="ath-hold-reason">
        <b>Current operator reason</b>
        <span>{reason()}</span>
      </div>
      <Show when={initialHold()}>
        {(original) => (
          <div class="ath-hold-reason">
            <b>Initial hold reason</b>
            <span>{original()}</span>
          </div>
        )}
      </Show>
    </article>
  );
}

export function AthQueue(): JSX.Element {
  const [state, setState] = createSignal<QueueState>({
    snapshot: null,
    refreshFailed: false,
    loading: false,
  });
  const [manualNote, setManualNote] = createSignal(false);
  // Re-ages the snapshot label (the monolith's 15s interval).
  const [now, setNow] = createSignal(Date.now());

  let loading = false;
  let requestId = 0;

  function load(manual: boolean): void {
    if (loading) return;
    loading = true;
    const id = ++requestId;
    if (manual || state().snapshot) setManualNote(true);
    setState((prev) => ({ ...prev, loading: true }));
    athReviewSnapshot()
      .then((data) => {
        if (id !== requestId) return;
        setState({ snapshot: data, refreshFailed: false, loading: false });
        setManualNote(false);
      })
      .catch(() => {
        if (id !== requestId) return;
        setState((prev) => ({
          snapshot: prev.snapshot,
          refreshFailed: true,
          loading: false,
        }));
        setManualNote(false);
      })
      .finally(() => {
        if (id === requestId) loading = false;
      });
  }

  onMount(() => {
    load(false);
    // Paused while an agent card is open, like every other global read (#648);
    // closing the card re-reads the queue once.
    const refresh = setInterval(() => {
      if (document.hidden || agentCardOpen()) return;
      load(false);
    }, REFRESH_MS);
    hydrateOnAgentCardClose(() => load(false));
    const reAge = setInterval(() => setNow(Date.now()), 15000);
    onCleanup(() => {
      clearInterval(refresh);
      clearInterval(reAge);
    });
  });

  const entries = createMemo<AthReview[]>(() => state().snapshot?.entries || []);
  const oldestHold = createMemo(() => {
    const opened = entries()
      .map((entry) => entry.review_opened_at)
      .filter(Boolean)
      .sort() as string[];
    return opened.length ? relTime(opened[0]) : "–";
  });
  const scoreTotal = createMemo(() =>
    entries().reduce((total, entry) => total + (Number(entry.score_count) || 0), 0),
  );
  const failedWithoutCache = () => state().refreshFailed && !state().snapshot;
  const label = createMemo(() => {
    now();
    const snapshot = state().snapshot;
    if (manualNote()) return { text: "Refreshing public snapshot…", stale: false };
    if (!snapshot) {
      return failedWithoutCache()
        ? { text: "Public review snapshot unavailable", stale: true }
        : { text: "Loading public snapshot…", stale: false };
    }
    return athSnapshotLabel(snapshot, state().refreshFailed, now());
  });

  return (
    <section aria-labelledby="page-title">
      <div class="ath-intro">
        <div>
          <span class="ath-eyebrow">Public review queue</span>
          <h2>High scores get a second look.</h2>
          <p>
            ATH reviews protect a competitive subnet from copied or benchmark-tuned submissions
            without discarding completed work. Recorded scores stay preserved, while emission
            eligibility pauses until the review is resolved.
          </p>
        </div>
        <div class="ath-outcomes" aria-label="Possible review outcomes">
          <div class="ath-outcome">
            <b>Clear</b>
            <span>A clear restores eligibility for emissions.</span>
          </div>
          <div class="ath-outcome">
            <b>Reject</b>
            <span>The submission closes with a public status update.</span>
          </div>
          <div class="ath-outcome">
            <b>Rerun</b>
            <span>A fresh evaluation replaces the held result.</span>
          </div>
        </div>
      </div>
      <div class="ath-metrics" aria-label="ATH review summary">
        <div class="ath-metric">
          <span>Active reviews</span>
          <strong id="ath-count">{state().snapshot ? String(entries().length) : "–"}</strong>
        </div>
        <div class="ath-metric">
          <span>Oldest hold</span>
          <strong id="ath-oldest">{state().snapshot ? oldestHold() : "–"}</strong>
        </div>
        <div class="ath-metric">
          <span>Scores preserved</span>
          <strong id="ath-scores">{state().snapshot ? String(scoreTotal()) : "–"}</strong>
        </div>
      </div>
      <div class="ath-review-head">
        <h2>Agents awaiting review</h2>
        <div class="ath-review-tools">
          <span
            class="ath-snapshot"
            classList={{ stale: label().stale }}
            id="ath-snapshot"
            role="status"
            aria-live="polite"
          >
            {label().text}
          </span>
        </div>
      </div>
      <div
        id="ath-review-state"
        class="ath-state"
        classList={{ error: failedWithoutCache() }}
        hidden={Boolean(state().snapshot && entries().length)}
      >
        <Show
          when={failedWithoutCache()}
          fallback={
            <Show
              when={state().snapshot && !entries().length}
              fallback={
                <>
                  <strong>Loading active reviews…</strong>Reading the public submission snapshot.
                </>
              }
            >
              <strong>No active ATH reviews.</strong>Every submission has cleared the public review
              queue.
            </Show>
          }
        >
          <strong>Could not load active reviews.</strong>Try refreshing in a moment. No example or
          private data is shown.
        </Show>
      </div>
      <div id="ath-review-list" class="ath-review-list" aria-live="polite">
        <Show when={state().snapshot && entries().length}>
          <For each={entries()}>{(entry) => <ReviewCard entry={entry} />}</For>
        </Show>
      </div>
    </section>
  );
}
