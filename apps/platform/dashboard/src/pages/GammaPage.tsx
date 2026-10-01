import { For, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useEndpoint } from "../data/useEndpoint";
import { dashboardHref } from "../lib/router";

export interface TreasuryAllocation {
  policy_revision: number;
  allocation_version: 1 | 2;
  mode: "shadow";
  routing_status: "not_activated";
  denominator: "miner_emission" | "released_miner_emission";
  service_bps: number;
  forecast_service_share: number;
  forecast_burn_share: number;
  forecast_miner_share: number;
  effective_service_share: number;
  burn_revision: number;
  burn_of_miner_remainder: number;
  collector_hotkey: string | null;
  collector_coldkey: string | null;
  sweep_interval_hours: number;
  sweep_status: "not_activated";
  payment_observer_status: "not_activated";
  buckets: {
    bucket_id: string;
    purpose: string;
    allocation_bps: number;
    holding_coldkey: string | null;
    publish_payments: boolean;
    payee_rules: {
      rule_id: string;
      label: string;
      recipient_coldkey: string;
      asset: string;
      recipient_hotkey: string | null;
      enabled: boolean;
    }[];
  }[];
}

const percent = (share: number) =>
  new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 2 }).format(share);

export function GammaPage(): JSX.Element {
  const allocation = useEndpoint<TreasuryAllocation>("/public/treasury-allocation");
  const data = () => (allocation.error() ? undefined : allocation.data());
  return (
    <section class="page active gamma-page" data-page="gamma" aria-label="Gamma treasury">
      <header class="gamma-intro">
        <h2>
          Service treasury <span class="gamma-beta">Beta</span>
        </h2>
        <p>
          Service funding and the public wallet trail. Gamma token details will be published here
          when available.
        </p>
      </header>
      <Show when={allocation.loading()}>
        <p role="status">Loading allocation policy…</p>
      </Show>
      <Show when={allocation.error()}>
        <div role="alert">
          Treasury policy could not be loaded.{" "}
          <button class="btn" onClick={() => allocation.refresh()}>
            Try again
          </button>
        </div>
      </Show>
      <Show when={data()}>
        {(policy) => (
          <>
            <section class="gamma-policy" aria-label="Allocation policy">
              <div class="gamma-section-title">
                <h3>Allocation policy</h3>
                <span>Revision {policy().policy_revision} · Shadow</span>
              </div>
              <p class="gamma-notice">
                Funding is not activated. The configured service share is{" "}
                {percent(policy().service_bps / 10_000)}; the effective service share is{" "}
                {percent(policy().effective_service_share)}. Saving wallet settings does not move
                funds.
              </p>
              <Show
                when={policy().allocation_version === 2}
                fallback={
                  <p>
                    Legacy policy uses the released miner share. Configure service buckets in
                    Backroom to prepare the new allocation.
                  </p>
                }
              >
                <p>
                  One registered collector hotkey receives the service pool. Burn applies to the
                  remaining miner share, so service allocations continue even at 100% miner burn.
                </p>
              </Show>
              <dl class="gamma-split">
                <div>
                  <dt>Services, forecast</dt>
                  <dd>{percent(policy().forecast_service_share)}</dd>
                </div>
                <div>
                  <dt>Burn, forecast</dt>
                  <dd>{percent(policy().forecast_burn_share)}</dd>
                </div>
                <div>
                  <dt>Eligible miners, forecast</dt>
                  <dd>{percent(policy().forecast_miner_share)}</dd>
                </div>
              </dl>
              <p class="gamma-caption">
                Forecast of the full miner emission vector if this policy is activated and eligible
                miner weights exist. Empty miner capacity burns its remainder.
              </p>
              <Show
                when={policy().buckets.length}
                fallback={<p>No service wallets have been configured yet.</p>}
              >
                <div class="gamma-table-scroll">
                  <table class="gamma-table">
                    <caption>Configured service wallets</caption>
                    <thead>
                      <tr>
                        <th>Purpose</th>
                        <th>Allocation</th>
                        <th>Holding wallet</th>
                        <th>Payment visibility</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={policy().buckets}>
                        {(bucket) => (
                          <tr>
                            <td>
                              <strong>{bucket.purpose}</strong>
                              <small>{bucket.bucket_id}</small>
                            </td>
                            <td>
                              {percent(bucket.allocation_bps / 10_000)}
                              <small>{bucket.allocation_bps} bps</small>
                            </td>
                            <td class="gamma-address">
                              {bucket.holding_coldkey || "Wallet not configured"}
                            </td>
                            <td>{bucket.publish_payments ? "Public" : "Publication disabled"}</td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
              </Show>
            </section>
            <section class="gamma-wallets" aria-label="Collector and distribution">
              <h3>Collector and distribution</h3>
              <dl>
                <div>
                  <dt>Collector hotkey</dt>
                  <dd class="gamma-address">{policy().collector_hotkey || "Not configured"}</dd>
                </div>
                <div>
                  <dt>Collector coldkey</dt>
                  <dd class="gamma-address">{policy().collector_coldkey || "Not configured"}</dd>
                </div>
                <div>
                  <dt>Distribution interval</dt>
                  <dd>Every {policy().sweep_interval_hours} hours, once activated</dd>
                </div>
                <div>
                  <dt>Automatic distribution</dt>
                  <dd>Not activated</dd>
                </div>
                <div>
                  <dt>Payment observer</dt>
                  <dd>Not activated</dd>
                </div>
              </dl>
              <p>
                Service holders control their own wallets. Backroom stores public addresses and
                policy, never seed phrases.
              </p>
            </section>
            <section class="gamma-payees" aria-label="Payment transparency rules">
              <h3>Payment transparency rules</h3>
              <p>
                A finalized transfer from a holding wallet to its configured payee is a service
                payment. GM credits and their USD value are confirmed only with a matching GM
                receipt.
              </p>
              <For each={policy().buckets}>
                {(bucket) => (
                  <For each={bucket.payee_rules}>
                    {(rule) => (
                      <div class="gamma-rule">
                        <strong>{rule.label}</strong>
                        <span>
                          {rule.enabled ? "Enabled" : "Disabled"} · {rule.asset}
                        </span>
                        <p class="gamma-address">
                          {bucket.holding_coldkey || "Unconfigured sender"} →{" "}
                          {rule.recipient_coldkey}
                        </p>
                        <Show when={rule.recipient_hotkey}>
                          <p class="gamma-address">Stake hotkey: {rule.recipient_hotkey}</p>
                        </Show>
                      </div>
                    )}
                  </For>
                )}
              </For>
              <Show when={!policy().buckets.some((bucket) => bucket.payee_rules.length)}>
                <p>No payees configured. GM, Bitsec, and Bitcast can each use separate rules.</p>
              </Show>
              <a class="btn" href={dashboardHref("activity")}>
                View wallet receipts and admin activity
              </a>
            </section>
          </>
        )}
      </Show>
    </section>
  );
}
