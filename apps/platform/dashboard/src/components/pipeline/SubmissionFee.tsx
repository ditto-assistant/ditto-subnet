// The current miner submission fee and its public change history, read from
// /public/submission-fee. The fee is operator policy (fixed TAO, revisioned in
// Backroom); the quote a miner actually pays is the one `ditto upload`
// reserves: a payment made within the quote lifetime keeps that fee even if
// this changes.
import { For, Show } from "solid-js";
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
  const payload = (): SubmissionFeePayload | undefined => {
    const value = fee.error() ? undefined : fee.data();
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
            {fee.loading() && !fee.error()
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
