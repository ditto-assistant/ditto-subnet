import { For, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useEndpoint } from "../data/useEndpoint";

interface LedgerHolding {
  bucket_id: string;
  coldkey: string;
  alpha_total_rao: string;
  tao_free_rao: string;
}

interface LedgerInterval {
  epoch_index: number;
  previous_epoch_index: number;
  block_number: number;
  block_at: string;
  earned_rao: string | null;
  estimated: boolean;
  balance_change_rao: string;
  outflow_rao: string;
  attributed_rao: string;
  unattributed_rao: string;
  receipt_count: number;
  receipt_ids: number[];
}

export interface TreasuryLedger {
  current: {
    epoch_index: number;
    block_number: number;
    block_at: string;
    collector_uid: number | null;
    collector_incentive: number | null;
    earned_per_epoch_rao: string | null;
    total_rao: string;
    locked_rao: string;
    available_rao: string;
    lock_status: "none" | "perpetual" | "decaying";
    owner_conviction_rao: string | null;
    owner_holds_most_conviction: boolean | null;
    holdings: LedgerHolding[];
  } | null;
  totals: {
    window_epochs: number;
    earned_rao: string;
    outflow_rao: string;
    attributed_rao: string;
    unattributed_rao: string;
  };
  intervals: LedgerInterval[];
  sampler: { enabled: boolean; state: string; last_sampled_at: string | null };
}

const RAO_PER_ALPHA = 1_000_000_000n;

/** Exact rao decimal string → alpha with up to 6 decimals; never rounds a positive amount to 0. */
export function alpha(rao: string | null): string {
  if (rao === null || !/^-?[0-9]+$/.test(rao)) return "Unknown";
  const value = BigInt(rao);
  const sign = value < 0n ? "-" : "";
  const magnitude = value < 0n ? -value : value;
  const whole = new Intl.NumberFormat().format(magnitude / RAO_PER_ALPHA);
  const fraction = (magnitude % RAO_PER_ALPHA).toString().padStart(9, "0");
  let shown = fraction.slice(0, 6).replace(/0+$/, "");
  if (!shown && magnitude > 0n && magnitude < RAO_PER_ALPHA) shown = fraction.replace(/0+$/, "");
  return `${sign}${whole}${shown ? "." + shown : ""} α`;
}

const positive = (rao: string) => /^[0-9]+$/.test(rao) && BigInt(rao) > 0n;

const lockText: Record<"none" | "perpetual" | "decaying", string> = {
  none: "No conviction lock",
  perpetual: "Perpetual conviction lock",
  decaying: "Decaying conviction lock",
};

export function GammaLedger(): JSX.Element {
  const ledger = useEndpoint<TreasuryLedger>("/public/treasury-ledger?limit=48", {
    pollMs: 60_000,
  });
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
                <Show when={!view().sampler.enabled || view().sampler.state !== "running"}>
                  <p role="status" class="gamma-caption">
                    The ledger sampler is {view().sampler.enabled ? view().sampler.state : "off"};
                    this reading may be out of date.
                  </p>
                </Show>
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
                <Show when={positive(view().totals.unattributed_rao)}>
                  <p role="alert">
                    Some collector outflow has no verified receipt yet. Each one stays listed below
                    until an operator records its receipt.
                  </p>
                </Show>
                <Show when={view().intervals.some((item) => positive(item.outflow_rao))}>
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
                        <For each={view().intervals.filter((item) => positive(item.outflow_rao))}>
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
                          {alpha(holding.tao_free_rao).replace(" α", "")} TAO
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
