import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, expect, it, vi } from "vitest";
import { GammaReceiptFeed } from "./GammaReceiptFeed";
import { getJSON } from "../lib/api";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);

it("shows the actual chain transaction and keeps GM credit unproven", async () => {
  vi.mocked(getJSON).mockResolvedValue({
    items: [
      {
        payment_id: "receipt",
        bucket_id: "gm",
        event_kind: "service_distribution",
        event_at: "2026-10-06T09:37:36Z",
        state: "chain_finalized",
        public_sender: "collector",
        public_recipient: "holding",
        deposit_amount_atomic: "100000000",
        deposit_asset: "SN118_ALPHA",
        credited_usd_nano: null,
        block_hash: "canonical-block",
        extrinsic_index: 26,
        extrinsic_hash: "verified-transaction",
      },
    ],
  });
  render(() => <GammaReceiptFeed />);
  await screen.findByText("Transaction: verified-transaction");
  expect(screen.getByText("GM credits not proven")).toBeInTheDocument();
  expect(screen.getByText(/0.1 SN118_ALPHA/)).toBeInTheDocument();
  expect(screen.queryByText(/No finalized/)).not.toBeInTheDocument();
});

it("separates unavailable receipts from a verified empty feed", async () => {
  vi.mocked(getJSON).mockRejectedValue(new Error("offline"));
  render(() => <GammaReceiptFeed />);
  await screen.findByText(/Payment status is unknown/);
  expect(screen.queryByText(/No finalized/)).not.toBeInTheDocument();
});
