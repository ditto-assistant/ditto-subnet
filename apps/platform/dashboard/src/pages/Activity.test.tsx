import { cleanup, fireEvent, render, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActivityPage } from "./ActivityPage";
import { getJSON } from "../lib/api";
vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
const request = vi.mocked(getJSON);
const item = {
  id: 12,
  recorded_at: "2026-09-18T12:30:00Z",
  action: "inference-concurrency-settings",
  method: "POST",
  status: "succeeded",
  http_status: 200,
  details: { settings: { chat_global_concurrency: 96 } },
  source: "request",
};
const treasuryDeposit = {
  id: 2,
  payment_id: "payment-2",
  event_kind: "gm_token_deposit",
  state: "chain_finalized",
  finalized_event_id: null,
  event_at: "2026-09-25T12:00:00Z",
  policy_revision: 3,
  burn_revision: 8,
  burn_share_micros: 0,
  denominator: "released_miner_emission",
  maintenance_bps: 25,
  gm_bps: 25,
  allocation_bps: 25,
  allocated_alpha_rao: "1000",
  source_alpha_rao: "500",
  route: "alpha_to_tao",
  deposit_asset: "TAO",
  deposit_amount_atomic: "99",
  credited_usd_nano: null,
  bounty_award_id: null,
  accepted_work_ref: null,
  public_sender: "public-sender",
  public_recipient: "public-recipient",
  block_hash: "0xabc",
  extrinsic_index: 2,
  event_index: 3,
  actor_provenance: "treasury_signer",
  actor_public_id: "signer-1",
  verification_source: "finalized_chain_rpc",
};
beforeEach(() => {
  history.replaceState(null, "", "/activity");
  request.mockReset();
  request.mockImplementation(async (path) =>
    path.startsWith("/public/treasury-activity")
      ? { items: [], next_before: null }
      : { items: [item], next_before: 12 },
  );
});
afterEach(cleanup);
it("searches server-side, filters outcomes, and paginates", async () => {
  render(() => <ActivityPage />);
  await screen.findByText("inference concurrency settings");
  expect(screen.getByText("96")).toBeInTheDocument();
  fireEvent.input(screen.getByLabelText("Search activity"), { target: { value: "canary" } });
  fireEvent.click(screen.getByRole("button", { name: /^Search$/ }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/public/admin-activity?limit=50&q=canary"),
  );
  expect(location.search).toContain("activity_q=canary");
  fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "failed" } });
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/public/admin-activity?limit=50&q=canary&status=failed"),
  );
  await waitFor(() =>
    expect(screen.getAllByRole("button", { name: "Older" })[1]).not.toBeDisabled(),
  );
  fireEvent.click(screen.getAllByRole("button", { name: "Older" })[1]!);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/public/admin-activity?limit=50&q=canary&status=failed&before=12",
    ),
  );
  await waitFor(() =>
    expect(screen.getAllByRole("button", { name: "Newer" })[1]).not.toBeDisabled(),
  );
  fireEvent.click(screen.getAllByRole("button", { name: "Newer" })[1]!);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("/public/admin-activity?limit=50&q=canary&status=failed"),
  );
});
it("shows empty results without implying the history is complete", async () => {
  request.mockResolvedValue({ items: [], next_before: null });
  render(() => <ActivityPage />);
  await screen.findByText("No activity found");
  expect(screen.getAllByRole("button", { name: "Older" })[1]).toBeDisabled();
});
it("recovers from an unavailable API", async () => {
  request.mockImplementation(async (path) => {
    if (path.startsWith("/public/treasury-activity")) return { items: [], next_before: null };
    if (request.mock.calls.filter(([url]) => url.startsWith("/public/admin-activity")).length === 1)
      throw new Error("offline");
    return { items: [item], next_before: null };
  });
  render(() => <ActivityPage />);
  await screen.findByText(/Activity could not be loaded/);
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await screen.findByText("inference concurrency settings");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("shows verified treasury receipts separately from admin requests", async () => {
  request.mockImplementation(async (path) =>
    path.startsWith("/public/treasury-activity")
      ? {
          items: [
            treasuryDeposit,
            {
              ...treasuryDeposit,
              id: 3,
              event_kind: "gm_credit_purchase",
              state: "reconciled",
              finalized_event_id: 2,
              credited_usd_nano: "3000000000",
              actor_provenance: "gm_reconciler",
              actor_public_id: "reconciler-1",
              verification_source: "chain_and_provider_reconciliation",
            },
          ],
          next_before: null,
        }
      : { items: [item], next_before: null },
  );
  render(() => <ActivityPage />);
  await screen.findByText("GM token deposit finalized");
  expect(screen.getAllByText("GM credits confirmed").length).toBeGreaterThan(0);
  expect(screen.queryByText("GM credit purchase")).not.toBeInTheDocument();
  expect(screen.getAllByText("public-recipient")).toHaveLength(2);
  expect(screen.getByText("inference concurrency settings")).toBeInTheDocument();
});
