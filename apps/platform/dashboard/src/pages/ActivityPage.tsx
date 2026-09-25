import { For, Show, createMemo, createSignal, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import { useEndpoint } from "../data/useEndpoint";
import { spaQuery } from "../lib/router";

interface Activity {
  id: number;
  recorded_at: string;
  action: string;
  method: string;
  status: "succeeded" | "failed" | "recorded" | "unknown";
  http_status: number | null;
  details: Record<string, unknown>;
  source: string;
}
interface ActivityPageData {
  items: Activity[];
  next_before: number | null;
}
interface TreasuryEvent {
  id: number;
  payment_id: string;
  event_kind: "gm_credit_purchase" | "maintenance_bounty";
  state: "chain_finalized" | "reconciled";
  event_at: string;
  policy_revision: number;
  burn_revision: string;
  denominator: "miner_emission" | "released_miner_emission";
  allocation_bps: number;
  allocated_alpha_rao: number;
  route: string;
  asset: string;
  gross_amount_atomic: number;
  realized_amount_atomic: number | null;
  public_sender: string;
  public_recipient: string;
  block_hash: string;
  extrinsic_index: number;
  event_index: number;
  actor_provenance: string;
  verification_source: string;
}
interface TreasuryPageData {
  items: TreasuryEvent[];
  next_before: number | null;
}
const outcomes = ["succeeded", "failed", "recorded", "unknown"];
const labels: Record<string, string> = {
  succeeded: "Succeeded",
  failed: "Failed",
  recorded: "Historical record",
  unknown: "Outcome unknown",
};
function title(value: string): string {
  return value
    .split("/")
    .filter((part) => !part.startsWith("{"))
    .join(" · ")
    .replaceAll(/[-_]/g, " ");
}
function detailRows(details: Record<string, unknown>, prefix = ""): [string, string][] {
  return Object.entries(details).flatMap(([key, value]) => {
    const label = prefix + title(key);
    if (value !== null && typeof value === "object" && !Array.isArray(value))
      return detailRows(value as Record<string, unknown>, label + " / ");
    return [[label, Array.isArray(value) ? value.join(", ") : String(value)]];
  });
}
export function ActivityPage(): JSX.Element {
  const [treasuryCursors, setTreasuryCursors] = createSignal<number[]>([]);
  const treasuryPath = createMemo(() => {
    const before = treasuryCursors().at(-1);
    return "/public/treasury-activity?limit=20" + (before ? `&before=${before}` : "");
  });
  const treasury = useEndpoint<TreasuryPageData>(treasuryPath);
  const treasuryData = () => (treasury.error() ? undefined : treasury.data());
  const initial = () => new URLSearchParams(location.search);
  const [query, setQuery] = createSignal(initial().get("activity_q") ?? "");
  const [draft, setDraft] = createSignal(query());
  const [status, setStatus] = createSignal(
    outcomes.includes(initial().get("activity_status") ?? "")
      ? initial().get("activity_status")!
      : "",
  );
  const [cursors, setCursors] = createSignal<number[]>([]);
  const path = createMemo(() => {
    const params = new URLSearchParams({ limit: "50" });
    if (query()) params.set("q", query());
    if (status()) params.set("status", status());
    const before = cursors().at(-1);
    if (before) params.set("before", String(before));
    return "/public/admin-activity?" + params;
  });
  const activity = useEndpoint<ActivityPageData>(path);
  const data = () => (activity.error() ? undefined : activity.data());
  function apply(): void {
    setQuery(draft().trim());
    setCursors([]);
    const params = spaQuery();
    params.delete("activity_q");
    params.delete("activity_status");
    if (query()) params.set("activity_q", query());
    if (status()) params.set("activity_status", status());
    history.replaceState(history.state, "", location.pathname + (params.size ? "?" + params : ""));
  }
  const restore = () => {
    setQuery(initial().get("activity_q") ?? "");
    setDraft(query());
    setStatus(
      outcomes.includes(initial().get("activity_status") ?? "")
        ? initial().get("activity_status")!
        : "",
    );
    setCursors([]);
  };
  window.addEventListener("popstate", restore);
  onCleanup(() => window.removeEventListener("popstate", restore));
  return (
    <section class="page active admin-activity" data-page="activity" aria-label="Admin activity">
      <section aria-label="Treasury spending" class="treasury-activity">
        <h2>Treasury spending</h2>
        <p>
          Finalized public chain receipts for GM credit purchases and maintenance bounties. A
          reconciled event confirms the payment outcome; entries with the same payment ID describe
          one payment.
        </p>
        <Show when={treasury.error()}>
          <div role="alert" class="activity-state">
            Treasury receipts could not be loaded.{" "}
            <button class="btn" onClick={() => treasury.refresh()}>
              Try again
            </button>
          </div>
        </Show>
        <Show when={treasury.loading()}>
          <p role="status">Loading treasury receipts…</p>
        </Show>
        <Show when={!treasury.loading() && !treasury.error()}>
          <Show
            when={treasuryData()?.items.length}
            fallback={<p>No verified treasury spending is recorded yet.</p>}
          >
            <div class="activity-list" aria-label="Treasury receipts">
              <For each={treasuryData()?.items}>
                {(item) => (
                  <details class="activity-record">
                    <summary>
                      <time datetime={item.event_at}>
                        {new Date(item.event_at).toLocaleString()}
                      </time>
                      <span class="activity-action">
                        {item.event_kind === "gm_credit_purchase"
                          ? "GM credit purchase"
                          : "Maintenance bounty"}
                        <small>
                          Payment {item.payment_id} ·{" "}
                          {item.state === "reconciled" ? "Reconciled" : "Chain finalized"}
                        </small>
                      </span>
                    </summary>
                    <div class="activity-details">
                      <dl>
                        <div>
                          <dt>Policy revision</dt>
                          <dd>{item.policy_revision}</dd>
                        </div>
                        <div>
                          <dt>Burn revision</dt>
                          <dd>{item.burn_revision}</dd>
                        </div>
                        <div>
                          <dt>Allocation</dt>
                          <dd>
                            {item.allocation_bps} bps of {item.denominator.replaceAll("_", " ")} ·{" "}
                            {item.allocated_alpha_rao} alpha rao
                          </dd>
                        </div>
                        <div>
                          <dt>Route</dt>
                          <dd>{item.route}</dd>
                        </div>
                        <div>
                          <dt>Gross amount</dt>
                          <dd>
                            {item.gross_amount_atomic} atomic {item.asset}
                          </dd>
                        </div>
                        <Show when={item.realized_amount_atomic !== null}>
                          <div>
                            <dt>Realized amount</dt>
                            <dd>
                              {item.realized_amount_atomic} atomic {item.asset}
                            </dd>
                          </div>
                        </Show>
                        <div>
                          <dt>Public sender</dt>
                          <dd>{item.public_sender}</dd>
                        </div>
                        <div>
                          <dt>Public recipient</dt>
                          <dd>{item.public_recipient}</dd>
                        </div>
                        <div>
                          <dt>Chain reference</dt>
                          <dd>
                            {item.block_hash} / extrinsic {item.extrinsic_index} / event{" "}
                            {item.event_index}
                          </dd>
                        </div>
                        <div>
                          <dt>Actor provenance</dt>
                          <dd>{item.actor_provenance}</dd>
                        </div>
                        <div>
                          <dt>Verification source</dt>
                          <dd>{item.verification_source}</dd>
                        </div>
                      </dl>
                    </div>
                  </details>
                )}
              </For>
            </div>
          </Show>
        </Show>
        <nav class="activity-pagination" aria-label="Treasury pagination">
          <button
            class="btn"
            disabled={!treasuryCursors().length || treasury.loading()}
            onClick={() => setTreasuryCursors((values) => values.slice(0, -1))}
          >
            Newer
          </button>
          <span>Page {treasuryCursors().length + 1}</span>
          <button
            class="btn"
            disabled={
              !treasuryData()?.next_before || treasury.loading() || Boolean(treasury.error())
            }
            onClick={() => {
              const next = treasuryData()?.next_before;
              if (next) setTreasuryCursors((values) => [...values, next]);
            }}
          >
            Older
          </button>
        </nav>
      </section>
      <p class="activity-intro">
        Backroom actions, settings changes, and canary requests. Open a record to inspect its public
        details.
      </p>
      <form
        class="admin-activity-filters"
        onSubmit={(event) => {
          event.preventDefault();
          apply();
        }}
      >
        <label>
          Search activity
          <input
            type="search"
            value={draft()}
            maxLength={120}
            placeholder="Action, agent ID, canary ID, or record number"
            onInput={(event) => setDraft(event.currentTarget.value)}
          />
        </label>
        <label>
          Outcome
          <select
            value={status()}
            onChange={(event) => {
              setStatus(event.currentTarget.value);
              apply();
            }}
          >
            <option value="">All outcomes</option>
            <For each={outcomes}>{(value) => <option value={value}>{labels[value]}</option>}</For>
          </select>
        </label>
        <button type="submit" class="btn">
          Search
        </button>
      </form>
      <p class="activity-disclosure">
        Operator identities, credentials, private evidence, and internal notes are withheld.
        Settings shown are requested values; a successful request does not confirm fleet adoption.
        Historical records retain their original timestamps. An unknown outcome means completion was
        not recorded.
      </p>
      <Show when={activity.error()}>
        <div role="alert" class="activity-state">
          Activity could not be loaded.{" "}
          <button class="btn" onClick={() => activity.refresh()}>
            Try again
          </button>
        </div>
      </Show>
      <Show when={activity.loading()}>
        <p role="status" class="activity-state">
          Loading activity…
        </p>
      </Show>
      <Show when={!activity.loading() && !activity.error()}>
        <Show
          when={data()?.items.length}
          fallback={
            <div class="activity-state">
              <h2>No activity found</h2>
              <p>
                Try another action or clear the outcome filter. Older actions without retained audit
                records cannot be reconstructed.
              </p>
            </div>
          }
        >
          <div class="activity-list" aria-label="Activity records">
            <For each={data()?.items}>
              {(item) => (
                <details class="activity-record">
                  <summary>
                    <time datetime={item.recorded_at}>
                      {new Date(item.recorded_at).toLocaleString(undefined, {
                        dateStyle: "medium",
                        timeStyle: "medium",
                      })}
                    </time>
                    <span class="activity-action">
                      {title(item.action)}
                      <small>
                        Record #{item.id} ·{" "}
                        {item.method === "HISTORY" ? "Retained audit history" : "Request"}
                      </small>
                    </span>
                    <span class="activity-outcome" data-outcome={item.status}>
                      {labels[item.status]}
                    </span>
                  </summary>
                  <div class="activity-details">
                    <h3>Public details</h3>
                    <dl>
                      <For each={detailRows(item.details)}>
                        {([key, value]) => (
                          <div>
                            <dt>{key}</dt>
                            <dd>{value}</dd>
                          </div>
                        )}
                      </For>
                    </dl>
                    <Show when={!Object.keys(item.details).length}>
                      <p>This action has no additional public parameters.</p>
                    </Show>
                    <p>
                      Recorded {new Date(item.recorded_at).toISOString()}
                      {item.http_status !== null ? ` · HTTP ${item.http_status}` : ""}
                    </p>
                  </div>
                </details>
              )}
            </For>
          </div>
        </Show>
      </Show>
      <nav class="activity-pagination" aria-label="Activity pagination">
        <button
          class="btn"
          disabled={!cursors().length || activity.loading()}
          onClick={() => setCursors((values) => values.slice(0, -1))}
        >
          Newer
        </button>
        <span>Page {cursors().length + 1}</span>
        <button
          class="btn"
          disabled={!data()?.next_before || activity.loading() || Boolean(activity.error())}
          onClick={() => {
            const next = data()?.next_before;
            if (next) setCursors((values) => [...values, next]);
          }}
        >
          Older
        </button>
      </nav>
    </section>
  );
}
