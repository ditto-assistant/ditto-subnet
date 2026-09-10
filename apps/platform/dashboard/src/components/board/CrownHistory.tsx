// Per-epoch history of the pinned ledger: what the fleet actually folded,
// epoch by epoch, and whether the crown moved between pins. This is the
// column that turns "the leaderboard looks unstable" into a checkable record:
// the board above is live and moves inside an epoch; weights only move at a
// pin, and every pin is an immutable row here. A run of unchanged crowns
// across retest waves is the stability the pin and the incumbency policy
// exist to produce, and a `crown_changed` row is the exact epoch to look at.
import { For, Show, createMemo } from "solid-js";
import type { JSX } from "solid-js";

import { REFRESH_MS } from "../../lib/config";
import { agentName, relTime, shortKey } from "../../lib/format";
import { pinLabel } from "../../lib/scoring";
import { useEndpoint } from "../../data/useEndpoint";
import type { ResourceState } from "../../data/useEndpoint";
import { EntityButton } from "../ui/EntityButton";
import type { LedgerEpoch, LedgerEpochActor, LedgerEpochsPayload } from "../../types/leaderboard";

const PATH = "/public/ledger-epochs?limit=24";

function ActorLink(props: { actor: LedgerEpochActor | null | undefined }): JSX.Element {
  return (
    <Show when={props.actor} fallback={<span class="text-faint">—</span>}>
      {(actor) => (
        <EntityButton
          kind="agent"
          id={actor().agent_id}
          label={
            actor().agent_name ? agentName(actor().agent_name) : shortKey(actor().miner_hotkey)
          }
        />
      )}
    </Show>
  );
}

function latest(resource: ResourceState<LedgerEpochsPayload>): LedgerEpochsPayload | null {
  if (resource.error()) return null;
  try {
    return resource.data() ?? null;
  } catch {
    return null;
  }
}

export function CrownHistory(props: {
  /** Injected for tests; defaults to the live endpoint. */
  resource?: ResourceState<LedgerEpochsPayload>;
  /** Overview mount trims the table; the Leaderboard page shows every pin. */
  mode?: "overview" | "page";
}): JSX.Element {
  const resource = props.resource ?? useEndpoint<LedgerEpochsPayload>(PATH, { pollMs: REFRESH_MS });
  const payload = createMemo(() => latest(resource));
  const unavailable = (): boolean => Boolean(resource.error()) && payload() == null;
  const epochs = createMemo<LedgerEpoch[]>(() => {
    const rows = payload()?.epochs ?? [];
    return props.mode === "overview" ? rows.slice(0, 6) : rows;
  });
  const changes = createMemo(() => epochs().filter((row) => row.crown_changed).length);
  const live = (): boolean => payload()?.mode === "live";
  return (
    <section class="crown-history" id="crown-history" aria-labelledby="crown-history-title">
      <div class="crown-history-head">
        <h3 id="crown-history-title">Pinned ledger history</h3>
        <span class="crown-history-sub">
          <Show when={payload()} fallback={unavailable() ? "History unavailable" : "Loading…"}>
            {(p) => (
              <>
                {live()
                  ? "Validators are folding the live ledger; pins below are historical."
                  : (p().count ?? 0) === 0
                    ? "No pin has been taken yet."
                    : epochs().length +
                      " pins · crown moved " +
                      changes() +
                      (changes() === 1 ? " time" : " times") +
                      " in this window"}
              </>
            )}
          </Show>
        </span>
      </div>
      <Show when={epochs().length}>
        <table class="crown-history-table">
          <thead>
            <tr>
              <th scope="col">Pin</th>
              <th scope="col">Taken</th>
              <th scope="col">Champion</th>
              <th scope="col">Incumbent</th>
              <th scope="col">Crown</th>
              <th scope="col" class="hide-sm">
                Entries
              </th>
            </tr>
          </thead>
          <tbody>
            <For each={epochs()}>
              {(row) => (
                <tr classList={{ "crown-changed": Boolean(row.crown_changed) }}>
                  <td class="mono" title={row.ledger_digest ? "digest " + row.ledger_digest : ""}>
                    {pinLabel(row)}
                  </td>
                  <td title={row.pinned_at ?? ""}>{row.pinned_at ? relTime(row.pinned_at) : "—"}</td>
                  <td>
                    <ActorLink actor={row.champion} />
                  </td>
                  <td>
                    <Show
                      when={row.crown_mode === "incumbent"}
                      fallback={<span class="text-faint">classic walk</span>}
                    >
                      <ActorLink actor={row.incumbent} />
                    </Show>
                  </td>
                  <td>
                    <span
                      class={"pin-badge " + (row.crown_changed ? "changed" : "held")}
                      title={
                        row.crown_changed
                          ? "The champion differs from the previous pin: weights moved at this epoch."
                          : "Same champion as the previous pin."
                      }
                    >
                      {row.crown_changed ? "moved" : "held"}
                    </span>
                  </td>
                  <td class="hide-sm mono">{row.entry_count ?? "—"}</td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </section>
  );
}
