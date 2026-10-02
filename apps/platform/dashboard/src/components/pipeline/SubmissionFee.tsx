// The current miner submission fee and its public change history, read from
// /public/submission-fee. The fee is operator policy (fixed TAO, revisioned in
// Backroom); the quote a miner actually pays is the one `ditto upload`
// reserves: a payment made within the quote lifetime keeps that fee even if
// this changes.
import { createEffect, createSignal, For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import type { SubmissionFeePayload, SubmissionFeeRevision } from "../../types/submission-fee";
import {
  changeLabel,
  feeChangeText,
  feeDate,
  feeRevisionText,
  isFixedTao,
  quoteLifetimeText,
  raoToTao,
} from "./submission-fee";

const FEE_POLL_MS = 60_000;

export function SubmissionFee(): JSX.Element {
  const fee = useEndpoint<SubmissionFeePayload>("/public/submission-fee", {
    pollMs: FEE_POLL_MS,
  });
  // "Loading" belongs to the first fetch only. Once any response or error has
  // settled, a background poll keeps the rendered fee or status in place.
  const [settled, setSettled] = createSignal(false);
  // The last successfully loaded payload survives a failed poll; the fee it
  // shows is then marked stale until a later poll succeeds.
  const [lastLoaded, setLastLoaded] = createSignal<SubmissionFeePayload | undefined>();
  const [stale, setStale] = createSignal(false);
  createEffect(() => {
    if (fee.loading()) return;
    setSettled(true);
    if (fee.error()) {
      setStale(true);
      return;
    }
    const value = fee.data();
    if (value !== undefined) {
      setLastLoaded(value);
      setStale(false);
    }
  });
  // Unavailable when nothing has loaded yet, or when the last loaded fee is in
  // a denomination this build has not reviewed as TAO.
  const payload = (): SubmissionFeePayload | undefined => {
    const value = lastLoaded();
    return value && isFixedTao(value.fee_denomination) ? value : undefined;
  };
  // Never render a row in a denomination this build has not reviewed as TAO;
  // dropping one makes the visible history incomplete.
  const history = (): SubmissionFeeRevision[] =>
    (payload()?.history ?? []).filter((row) => isFixedTao(row.fee_denomination));
  const incomplete = (): boolean =>
    Boolean(payload()?.history_truncated) || history().length !== (payload()?.history.length ?? 0);

  return (
    <section class="submission-fee" aria-label="Submission fee">
      <Show
        when={payload()}
        fallback={
          <p class="submission-fee-status" role="status">
            {!settled() && fee.loading()
              ? "Loading submission fee…"
              : "Submission fee is unavailable."}
          </p>
        }
      >
        {(current) => (
          <>
            <div class="submission-fee-current">
              <span class="submission-fee-label">Submission fee</span>
              <strong class="submission-fee-amount" data-fee-rao={current().fee_amount_rao}>
                {raoToTao(current().fee_amount_rao)} TAO
              </strong>
              <span class="submission-fee-meta">
                {feeRevisionText(current().fee_revision, current().policy_revision)}
                <Show when={current().fee_effective_at}>
                  {(at) => <> · since {feeDate(at())}</>}
                </Show>
              </span>
              <Show when={stale()}>
                <span class="submission-fee-stale" role="status">
                  (couldn't refresh; showing the last loaded fee)
                </span>
              </Show>
              <span class="submission-fee-note">
                A payment made within {quoteLifetimeText(current().quote_lifetime_seconds)} of
                reserving keeps the reserved fee.
              </span>
            </div>
            <Show when={history().length > 0 || incomplete()}>
              <details class="submission-fee-history">
                <summary>
                  Fee history
                  <span class="submission-fee-count">
                    {history().length}
                    {incomplete() ? "+" : ""}
                  </span>
                </summary>
                <Show when={incomplete()}>
                  <p class="submission-fee-incomplete">Some fee changes are not shown.</p>
                </Show>
                <ol aria-label="Submission fee changes, newest first">
                  <For each={history()}>
                    {(change, index) => (
                      <li data-fee-revision={change.revision}>
                        <time datetime={change.effective_at ?? undefined}>
                          {feeDate(change.effective_at)}
                        </time>
                        <span class="submission-fee-change">
                          {feeChangeText(change.previous_fee_amount_rao, change.fee_amount_rao)}
                        </span>
                        <span class="submission-fee-direction">
                          {changeLabel(
                            change.previous_fee_amount_rao,
                            change.fee_amount_rao,
                            // Only the oldest row of a complete history is the
                            // genuine first fee.
                            index() === history().length - 1 && !incomplete(),
                          )}
                        </span>
                        <span class="submission-fee-revision">rev {change.revision}</span>
                      </li>
                    )}
                  </For>
                </ol>
              </details>
            </Show>
          </>
        )}
      </Show>
    </section>
  );
}
