// The public terminal screening-review card (monolith renderScreeningReview
// 7036–7068): findings, source locations in the served path, policy
// observations — digest-verified, with no source text or private challenge
// data. Renders nothing when the attempt carries neither a finding nor
// evidence.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { screeningReviewCategoryLabel } from "./labels";
import type { ScreeningAttempt } from "../../types/pipeline";

export function ScreeningReview(props: { attempt: ScreeningAttempt }): JSX.Element {
  const finding = () => props.attempt.review_finding || null;
  const evidence = () =>
    Array.isArray(props.attempt.review_evidence) ? props.attempt.review_evidence : [];
  const confidence = () => {
    const f = finding();
    return f && Number.isFinite(Number(f.confidence))
      ? Math.round(Number(f.confidence) * 100) + "% confidence"
      : "Verified finding";
  };
  return (
    <Show when={finding() || evidence().length}>
      <section class="screening-review" aria-label="Detailed screening rejection">
        <div class="screening-review-head">
          <h5 class="screening-review-title">Why this submission was rejected</h5>
          <span class="screening-review-verdict">{confidence()}</span>
        </div>
        <Show when={finding()}>{(f) => <p class="screening-review-summary">{f().summary}</p>}</Show>
        <Show
          when={finding() && Array.isArray(finding()?.categories) && finding()?.categories?.length}
        >
          <ul class="screening-review-categories" aria-label="Policy categories">
            <For each={finding()?.categories || []}>
              {(category) => (
                <li class="screening-review-category">{screeningReviewCategoryLabel(category)}</li>
              )}
            </For>
          </ul>
        </Show>
        <Show
          when={finding() && Array.isArray(finding()?.locations) && finding()?.locations?.length}
        >
          <div class="screening-review-block">
            <h6>Source locations in the served path</h6>
            <ul class="screening-review-list">
              <For each={finding()?.locations || []}>
                {(location) => (
                  <li class="screening-review-location">
                    <code>
                      {location.path}:{location.line}
                    </code>
                    <span>{screeningReviewCategoryLabel(location.category)}</span>
                  </li>
                )}
              </For>
            </ul>
          </div>
        </Show>
        <Show when={evidence().length}>
          <div class="screening-review-block">
            <h6>Policy observations</h6>
            <ul class="screening-review-list">
              <For each={evidence()}>
                {(item) => (
                  <li>
                    <b>{screeningReviewCategoryLabel(item.code)}.</b> {item.summary}
                  </li>
                )}
              </For>
            </ul>
          </div>
        </Show>
        <p class="screening-review-meta">
          Digest-verified public review · no source text or private challenge data
          <Show when={finding()?.reviewer_revision}>
            {(revision) => <> · reviewer {revision()}</>}
          </Show>
        </p>
      </section>
    </Show>
  );
}
