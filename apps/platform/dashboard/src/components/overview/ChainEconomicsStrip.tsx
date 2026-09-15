// Compact chain economics strip for Overview (and reusable on Metagraph).
import { Show, type JSX } from "solid-js";

import type { ResourceState } from "../../data/useEndpoint";
import { dashboardHref } from "../../lib/router";
import { navigateToPage } from "../../stores/routeStore";
import type { PublicChainResponse } from "../../types/chain";

function fmtUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return (
    "$" +
    value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

function fmtTao(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return (
    value.toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits,
    }) + " τ"
  );
}

function latestChain(
  resource: ResourceState<PublicChainResponse> | undefined,
): PublicChainResponse | undefined {
  if (!resource || resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

export function ChainEconomicsStrip(props: {
  chain?: ResourceState<PublicChainResponse>;
  /** When true, omit the Metagraph deep-link (already on that page). */
  hideLink?: boolean;
}): JSX.Element {
  const snap = () => latestChain(props.chain);
  return (
    <section class="chain-economics" aria-label="Chain economics">
      <header class="chain-economics-head">
        <div>
          <p class="chain-economics-eyebrow">On-chain · SN118</p>
          <h2>Subnet economics</h2>
          <p>
            Live metagraph totals and registration cost. Distinct from KOTH projected emissions on
            the leaderboard.
          </p>
        </div>
        <Show when={!props.hideLink}>
          <a
            class="chain-economics-link"
            href={dashboardHref("metagraph")}
            onClick={(event) => {
              event.preventDefault();
              navigateToPage("metagraph");
            }}
          >
            Full metagraph →
          </a>
        </Show>
      </header>
      <Show when={snap()} fallback={<p class="chain-economics-empty">Chain snapshot loading…</p>}>
        {(data) => (
          <div class="chain-economics-grid" role="list">
            <div class="chain-econ-cell" role="listitem">
              <span>TAO / USD</span>
              <strong>{fmtUsd(data().tao_usd)}</strong>
            </div>
            <div class="chain-econ-cell" role="listitem">
              <span>α / TAO</span>
              <strong>
                {data().market.alpha_tao == null ? "—" : data().market.alpha_tao!.toFixed(6)}
              </strong>
            </div>
            <div class="chain-econ-cell" role="listitem">
              <span>Total stake</span>
              <strong>
                {data().totals.total_stake == null || !Number.isFinite(data().totals.total_stake)
                  ? "—"
                  : data().totals.total_stake.toLocaleString(undefined, {
                      maximumFractionDigits: 1,
                    }) + " α"}
              </strong>
            </div>
            <div class="chain-econ-cell" role="listitem">
              <span>Registration</span>
              <strong>{fmtTao(data().registration.recycle_tao, 4)}</strong>
            </div>
            <div class="chain-econ-cell" role="listitem">
              <span>Neurons</span>
              <strong>
                {data().totals.neuron_count}
                <small>
                  {" "}
                  · {data().totals.validator_count} val · {data().totals.miner_count} miner
                </small>
              </strong>
            </div>
            <div class="chain-econ-cell" role="listitem">
              <span>Tempo</span>
              <strong>
                {data().epoch ? `${data().epoch!.tempo_blocks} blk` : "—"}
                <Show when={data().block != null}>
                  <small> · block {data().block!.toLocaleString()}</small>
                </Show>
              </strong>
            </div>
          </div>
        )}
      </Show>
      <Show when={snap()?.stale}>
        <p class="chain-economics-stale">Showing last known chain snapshot (stale).</p>
      </Show>
    </section>
  );
}
