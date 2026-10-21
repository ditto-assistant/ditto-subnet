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
  const transaction = await screen.findByRole("link", { name: "verified-transaction" });
  expect(transaction).toHaveAttribute("href", "https://taostats.io/extrinsic/verified-transaction");
  expect(transaction).toHaveAttribute("target", "_blank");
  expect(transaction).toHaveAttribute("rel", "noopener noreferrer");
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

it("publishes verified delivery while clearly withholding source attribution", async () => {
  vi.mocked(getJSON).mockImplementation(async (path) => ({
    items: String(path).includes("manual-movements")
      ? [
          {
            payment_id: "movement-only",
            bucket_id: "gm",
            event_kind: "wallet_transfer",
            event_at: "2026-10-21T04:00:00Z",
            state: "chain_finalized",
            public_sender: "collector",
            public_recipient: "holding",
            deposit_amount_atomic: "500000000000",
            deposit_asset: "SN118_ALPHA",
            credited_usd_nano: null,
            source_attribution: "incomplete",
            block_hash: "canonical-block",
            extrinsic_index: 5,
            extrinsic_hash: "verified-500",
          },
        ]
      : [],
  }));
  render(() => <GammaReceiptFeed />);
  expect(await screen.findByRole("link", { name: "verified-500" })).toHaveAttribute(
    "href",
    "https://taostats.io/extrinsic/verified-500",
  );
  expect(screen.getByText(/500 SN118_ALPHA/)).toBeInTheDocument();
  expect(screen.getByText(/Source earnings attribution incomplete/)).toBeInTheDocument();
  expect(screen.getByText("GM credits not proven")).toBeInTheDocument();
  expect(screen.queryByText(/No finalized/)).not.toBeInTheDocument();
});
