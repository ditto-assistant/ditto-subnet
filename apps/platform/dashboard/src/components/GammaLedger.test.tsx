import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, expect, it, vi } from "vitest";
import { GammaLedger } from "./GammaLedger";
import type { TreasuryLedger } from "./GammaLedger";
import { getJSON } from "../lib/api";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);

const ALPHA = 1e9;

const ledger = (unattributed: number): TreasuryLedger => ({
  current: {
    epoch_index: 25710,
    block_number: 9279067,
    block_at: "2026-10-14T04:00:00Z",
    collector_uid: 21,
    collector_incentive: 0.75,
    earned_per_epoch_rao: 110 * ALPHA,
    total_rao: 8549 * ALPHA,
    locked_rao: 8000 * ALPHA,
    available_rao: 549 * ALPHA,
    lock_status: "perpetual",
    owner_conviction_rao: 102039 * ALPHA,
    owner_holds_most_conviction: true,
    holdings: [{ bucket_id: "gm", coldkey: "gm-wallet", alpha_total_rao: 0, tao_free_rao: 0 }],
  },
  totals: {
    window_epochs: 2,
    earned_rao: 220 * ALPHA,
    outflow_rao: 60 * ALPHA + unattributed,
    attributed_rao: 60 * ALPHA,
    unattributed_rao: unattributed,
  },
  intervals: [
    {
      epoch_index: 25710,
      previous_epoch_index: 25709,
      block_number: 9279067,
      block_at: "2026-10-14T04:00:00Z",
      earned_rao: 110 * ALPHA,
      estimated: false,
      balance_change_rao: 50 * ALPHA - unattributed,
      outflow_rao: 60 * ALPHA + unattributed,
      attributed_rao: 60 * ALPHA,
      unattributed_rao: unattributed,
      receipt_ids: [2],
    },
  ],
  sampler: { enabled: true, state: "running", last_sampled_at: null },
});

it("shows the locked balance, conviction and receipted outflow", async () => {
  vi.mocked(getJSON).mockResolvedValue(ledger(0));
  render(() => <GammaLedger />);
  expect(await screen.findByText(/Perpetual conviction lock/)).toBeInTheDocument();
  expect(screen.getByText(/subnet owner hotkey holds the most conviction/)).toBeInTheDocument();
  expect(screen.getByText("Epochs where alpha left the collector")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("flags outflow that has no verified receipt", async () => {
  vi.mocked(getJSON).mockResolvedValue(ledger(150 * ALPHA));
  render(() => <GammaLedger />);
  expect(await screen.findByRole("alert")).toHaveTextContent("has no verified receipt");
});

it("says when no reading exists yet", async () => {
  vi.mocked(getJSON).mockResolvedValue({ ...ledger(0), current: null, intervals: [] });
  render(() => <GammaLedger />);
  expect(await screen.findByText(/No collector readings/)).toBeInTheDocument();
});
