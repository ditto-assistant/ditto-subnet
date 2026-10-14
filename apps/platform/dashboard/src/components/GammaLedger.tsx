import { For, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useEndpoint } from "../data/useEndpoint";

interface LedgerHolding {
  bucket_id: string;
  coldkey: string;
  alpha_total_rao: number;
  tao_free_rao: number;
}

interface LedgerInterval {
  epoch_index: number;
  previous_epoch_index: number;
  block_number: number;
  block_at: string;
  earned_rao: number | null;
  estimated: boolean;
  balance_change_rao: number;
  outflow_rao: number;
  attributed_rao: number;
  unattributed_rao: number;
  receipt_ids: number[];
}

export interface TreasuryLedger {
  current: {
    epoch_index: number;
    block_number: number;
    block_at: string;
    collector_uid: number | null;
    collector_incentive: number | null;
    earned_per_epoch_rao: number | null;
    total_rao: number;
    locked_rao: number;
    available_rao: number;
    lock_status: "none" | "perpetual" | "decaying";
    owner_conviction_rao: number | null;
    owner_holds_most_conviction: boolean | null;
    holdings: LedgerHolding[];
  } | null;
  totals: {
    window_epochs: number;
    earned_rao: number;
    outflow_rao: number;
    attributed_rao: number;
    unattributed_rao: number;
  };
  intervals: LedgerInterval[];
  sampler: { enabled: boolean; state: string; last_sampled_at: string | null };
}

const alpha = (rao: number | null) =>
  rao === null
    ? "Unknown"
    : new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(rao / 1e9) + " α";

const lockText: Record<"none" | "perpetual" | "decaying", string> = {
  none: "No conviction lock",
  perpetual: "Perpetual conviction lock",
  decaying: "Decaying conviction lock",
};

export function GammaLedger(): JSX.Element {
  const ledger = useEndpoint<TreasuryLedger>("/public/treasury-ledger?limit=48");
  const data = () => (ledger.error() ? undefined : ledger.data());
  return (
    <section class="gamma-policy" aria-label="Gamma ledger">
      <div class="gamma-section-title">
        <h3>Gamma ledger</h3>
        <span>Read from finalized chain state every epoch</span>
      </div>
      <Show when={ledger.loading()}>
        <p role="status">Loading the Gamma ledger…</p>
      </Show>
      <Show when={!ledger.loading() && !data()}>
        <p>The Gamma ledger could not be loaded.</p>
        <button class="btn" onClick={() => ledger.refresh()}>
          Retry ledger
        </button>
      </Show>
      <Show when={data()}>
        {(view) => (
          <Show
            when={view().current}
            fallback={<p>No collector readings have been recorded yet.</p>}
          >
            {(current) => (
              <>
                <dl class="gamma-split">
                  <div>
                    <dt>Collector balance</dt>
                    <dd>{alpha(current().total_rao)}</dd>
                  </div>
                  <div>
                    <dt>Locked</dt>
                    <dd>{alpha(current().locked_rao)}</dd>
                  </div>
                  <div>
                    <dt>Unlocked</dt>
                    <dd>{alpha(current().available_rao)}</dd>
                  </div>
                  <div>
                    <dt>Earning per epoch</dt>
                    <dd>{alpha(current().earned_per_epoch_rao)}</dd>
                  </div>
                </dl>
                <p class="gamma-caption">
                  {lockText[current().lock_status]}. Epoch {current().epoch_index}, block{" "}
                  {current().block_number}. Collector UID{" "}
                  {current().collector_uid ?? "unregistered"}.
                </p>
                <Show when={current().owner_holds_most_conviction !== null}>
                  <p class="gamma-caption">
                    {current().owner_holds_most_conviction
                      ? "The subnet owner hotkey holds the most conviction on SN118"
                      : "Another hotkey holds more conviction than the subnet owner hotkey"}{" "}
                    ({alpha(current().owner_conviction_rao)} locked toward the owner).
                  </p>
                </Show>
                <dl class="gamma-split" aria-label="Ledger window totals">
                  <div>
                    <dt>Earned, last {view().totals.window_epochs} epochs</dt>
                    <dd>{alpha(view().totals.earned_rao)}</dd>
                  </div>
                  <div>
                    <dt>Paid out with receipts</dt>
                    <dd>{alpha(view().totals.attributed_rao)}</dd>
                  </div>
                  <div>
                    <dt>Moved without a receipt</dt>
                    <dd>{alpha(view().totals.unattributed_rao)}</dd>
                  </div>
                </dl>
                <Show when={view().totals.unattributed_rao > 0}>
                  <p role="alert">
                    Some collector outflow has no verified receipt yet. Each one stays listed below
                    until an operator records its receipt.
                  </p>
                </Show>
                <Show when={view().intervals.some((item) => item.outflow_rao > 0)}>
                  <div class="gamma-table-scroll">
                    <table class="gamma-table">
                      <caption>Epochs where alpha left the collector</caption>
                      <thead>
                        <tr>
                          <th>Epoch</th>
                          <th>Earned</th>
                          <th>Left the collector</th>
                          <th>With receipt</th>
                          <th>Without receipt</th>
                        </tr>
                      </thead>
                      <tbody>
                        <For each={view().intervals.filter((item) => item.outflow_rao > 0)}>
                          {(item) => (
                            <tr>
                              <td>
                                {item.epoch_index}
                                <small>{item.block_at}</small>
                              </td>
                              <td>
                                {alpha(item.earned_rao)}
                                <Show when={item.estimated}>
                                  <small>estimated</small>
                                </Show>
                              </td>
                              <td>{alpha(item.outflow_rao)}</td>
                              <td>{alpha(item.attributed_rao)}</td>
                              <td>{alpha(item.unattributed_rao)}</td>
                            </tr>
                          )}
                        </For>
                      </tbody>
                    </table>
                  </div>
                </Show>
                <Show when={current().holdings.length}>
                  <ul class="gamma-caption">
                    <For each={current().holdings}>
                      {(holding) => (
                        <li>
                          {holding.bucket_id} holding wallet: {alpha(holding.alpha_total_rao)},{" "}
                          {new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(
                            holding.tao_free_rao / 1e9,
                          )}{" "}
                          TAO
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
              </>
            )}
          </Show>
        )}
      </Show>
    </section>
  );
}
