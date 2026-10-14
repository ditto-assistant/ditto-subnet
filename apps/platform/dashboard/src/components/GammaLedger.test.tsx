import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, expect, it, vi } from "vitest";
import { GammaLedger } from "./GammaLedger";
import type { TreasuryLedger } from "./GammaLedger";
import { getJSON } from "../lib/api";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);

const ALPHA = 1_000_000_000;
const rao = (alphaAmount: number) => String(BigInt(alphaAmount) * BigInt(ALPHA));

const ledger = (unattributed: number): TreasuryLedger => ({
  // unattributed is in alpha
  current: {
    epoch_index: 25710,
    block_number: 9279067,
    block_at: "2026-10-14T04:00:00Z",
    collector_uid: 21,
    collector_incentive: 0.75,
    earned_per_epoch_rao: rao(110),
    total_rao: rao(8549),
    locked_rao: rao(8000),
    available_rao: rao(549),
    lock_status: "perpetual",
    owner_conviction_rao: rao(102039),
    owner_holds_most_conviction: true,
    holdings: [{ bucket_id: "gm", coldkey: "gm-wallet", alpha_total_rao: "0", tao_free_rao: "0" }],
  },
  totals: {
    window_epochs: 2,
    earned_rao: rao(220),
    outflow_rao: rao(60 + unattributed),
    attributed_rao: rao(60),
    unattributed_rao: rao(unattributed),
  },
  intervals: [
    {
      epoch_index: 25710,
      previous_epoch_index: 25709,
      block_number: 9279067,
      block_at: "2026-10-14T04:00:00Z",
      earned_rao: rao(110),
      estimated: false,
      balance_change_rao: rao(50 - unattributed),
      outflow_rao: rao(60 + unattributed),
      attributed_rao: rao(60),
      unattributed_rao: rao(unattributed),
      receipt_count: 1,
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
  vi.mocked(getJSON).mockResolvedValue(ledger(150));
  render(() => <GammaLedger />);
  expect(await screen.findByRole("alert")).toHaveTextContent("has no verified receipt");
});

it("says when no reading exists yet", async () => {
  vi.mocked(getJSON).mockResolvedValue({ ...ledger(0), current: null, intervals: [] });
  render(() => <GammaLedger />);
  expect(await screen.findByText(/No collector readings/)).toBeInTheDocument();
});

it("formats exact rao strings without hiding small amounts", async () => {
  const { alpha } = await import("./GammaLedger");
  expect(alpha("1000001")).toBe("0.001 α");
  expect(alpha("1")).toBe("0.000000001 α");
  expect(alpha("9007199254740993123")).toBe(
    `${new Intl.NumberFormat().format(9007199254n)}.740993 α`,
  );
  expect(alpha("-2500000000")).toBe("-2.5 α");
  expect(alpha(null)).toBe("Unknown");
});

it("warns when the sampler is not running", async () => {
  vi.mocked(getJSON).mockResolvedValue({
    ...ledger(0),
    sampler: { enabled: true, state: "blocked", last_sampled_at: null },
  });
  render(() => <GammaLedger />);
  expect(await screen.findByText(/ledger sampler is blocked/)).toBeInTheDocument();
});
