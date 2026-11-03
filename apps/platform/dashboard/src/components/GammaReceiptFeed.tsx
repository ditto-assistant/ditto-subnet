import { For, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useEndpoint } from "../data/useEndpoint";
import { dashboardHref } from "../lib/router";

interface Receipt {
  payment_id: string;
  bucket_id: string | null;
  event_kind: string;
  event_at: string;
  state: string;
  public_sender: string;
  public_recipient: string;
  deposit_asset: string;
  deposit_amount_atomic: string;
  credited_usd_nano: string | null;
  block_hash: string;
  extrinsic_index: number;
  extrinsic_hash: string | null;
  source_attribution?: "incomplete";
}

function amount(value: string): string {
  if (!/^[0-9]+$/.test(value)) return "Amount unavailable";
  const digits = value.padStart(10, "0");
  const whole = digits.slice(0, -9).replace(/^0+(?=[0-9])/, "");
  const fraction = digits.slice(-9).replace(/0+$/, "");
  return whole + (fraction ? "." + fraction : "");
}

export function GammaReceiptFeed(): JSX.Element {
  const feed = useEndpoint<{ items: Receipt[] }>("/public/treasury-activity?limit=10");
  const movements = useEndpoint<{ items: Receipt[] }>("/public/treasury-activity/manual-movements");
  const verifiedFeed = () => !feed.error() && Array.isArray(feed.data()?.items);
  const verifiedMovements = () => !movements.error() && Array.isArray(movements.data()?.items);
  const receipts = () => {
    const items = [
      ...(verifiedFeed() ? feed.data()!.items : []),
      ...(verifiedMovements() ? movements.data()!.items : []),
    ];
    const seen = new Set<string>();
    return items
      .sort((a, b) => b.event_at.localeCompare(a.event_at))
      .filter((item) => {
        // One extrinsic can contain distinct finalized event receipts. The
        // canonical payment ID binds chain/block/extrinsic/event coordinates.
        const key = item.payment_id;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 10);
  };
  return (
    <section class="gamma-policy" aria-label="Finalized wallet receipts">
      <h3>Finalized wallet receipts</h3>
      <p>
        Collector distributions and service payments are separate receipts. A chain transfer does
        not confirm GM credits.
      </p>
      <Show when={feed.loading() || movements.loading()}>
        <p role="status">Loading wallet receipts…</p>
      </Show>
      <Show
        when={!feed.loading() && !movements.loading() && (!verifiedFeed() || !verifiedMovements())}
      >
        <p>
          Some wallet receipts could not be loaded. Payment status is unknown for unavailable
          records.
        </p>
        <button
          class="btn"
          onClick={() => {
            void feed.refresh();
            void movements.refresh();
          }}
        >
          Retry receipts
        </button>
      </Show>
      <Show when={verifiedFeed() || verifiedMovements()}>
        <Show
          when={receipts().length}
          fallback={
            <Show when={verifiedFeed() && verifiedMovements()}>
              <p>No finalized wallet receipts have been published.</p>
            </Show>
          }
        >
          <div class="gamma-table-scroll">
            <table class="gamma-table">
              <caption>Latest verified wallet movements</caption>
              <thead>
                <tr>
                  <th>Purpose and time</th>
                  <th>Wallets</th>
                  <th>Amount</th>
                  <th>Proof</th>
                </tr>
              </thead>
              <tbody>
                <For each={receipts()}>
                  {(item) => (
                    <tr>
                      <td>
                        {item.event_kind.replaceAll("_", " ")} · {item.bucket_id || "legacy"}
                        <small>{item.event_at}</small>
                      </td>
                      <td class="gamma-address">
                        {item.public_sender}
                        <small>→ {item.public_recipient}</small>
                      </td>
                      <td>
                        {amount(item.deposit_amount_atomic)} {item.deposit_asset}
                        <small>
                          {item.credited_usd_nano === null
                            ? "GM credits not proven"
                            : `${item.credited_usd_nano} USD nanos credited`}
                        </small>
                      </td>
                      <td class="gamma-address">
                        {item.state}
                        <Show when={item.source_attribution === "incomplete"}>
                          <small>
                            Source earnings attribution incomplete — wallet transfer verified.
                          </small>
                        </Show>
                        <small>
                          Transaction:{" "}
                          <Show
                            when={item.extrinsic_hash}
                            fallback="Not recorded in legacy receipt"
                          >
                            {(hash) => (
                              <a
                                href={`https://taostats.io/extrinsic/${encodeURIComponent(hash())}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                title="View transaction on Taostats (opens in a new tab)"
                              >
                                {hash()}
                              </a>
                            )}
                          </Show>
                        </small>
                        <small>
                          {item.block_hash} / extrinsic {item.extrinsic_index}
                        </small>
                      </td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </div>
        </Show>
      </Show>
      <a class="btn" href={dashboardHref("activity")}>
        View full wallet audit history
      </a>
    </section>
  );
}
