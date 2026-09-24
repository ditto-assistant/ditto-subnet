// The current miner submission fee and its public change history, read from
// /public/submission-fee. The fee is operator policy (fixed TAO, revisioned in
// Backroom); the quote a miner actually pays is the one `ditto upload`
// reserves, which keeps its fee for the quote lifetime even if this changes.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import type { SubmissionFeePayload } from "../../types/submission-fee";
import { feeChangeText, feeDate, feeDirection, isFixedTao, raoToTao } from "./submission-fee";

const FEE_POLL_MS = 60_000;

export function SubmissionFee(): JSX.Element {
  const fee = useEndpoint<SubmissionFeePayload>("/public/submission-fee", {
    pollMs: FEE_POLL_MS,
  });
  const payload = (): SubmissionFeePayload | undefined => {
    const value = fee.error() ? undefined : fee.data();
    return value && isFixedTao(value.fee_denomination) ? value : undefined;
  };
  const lifetimeHours = (): number => Math.round((payload()?.quote_lifetime_seconds ?? 0) / 3600);

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
                Fixed TAO · revision {current().fee_revision} · since{" "}
                {feeDate(current().fee_effective_at)}
              </span>
              <span class="submission-fee-note">
                A reserved quote keeps its fee for {lifetimeHours()} hours.
              </span>
            </div>
            <details class="submission-fee-history">
              <summary>
                Fee history
                <span class="submission-fee-count">
                  {current().history.length}
                  {current().history_truncated ? "+" : ""}
                </span>
              </summary>
              <ol aria-label="Submission fee changes, newest first">
                <For each={current().history}>
                  {(change) => (
                    <li data-fee-revision={change.revision}>
                      <time datetime={change.effective_at ?? undefined}>
                        {feeDate(change.effective_at)}
                      </time>
                      <span class="submission-fee-change">
                        {feeChangeText(change.previous_fee_amount_rao, change.fee_amount_rao)}
                      </span>
                      <span class="submission-fee-direction">
                        {feeDirection(change.previous_fee_amount_rao, change.fee_amount_rao) ||
                          "initial"}
                      </span>
                      <span class="submission-fee-revision">rev {change.revision}</span>
                    </li>
                  )}
                </For>
              </ol>
            </details>
          </>
        )}
      </Show>
    </section>
  );
}
