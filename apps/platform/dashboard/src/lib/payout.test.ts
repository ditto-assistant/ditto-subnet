import { describe, expect, it } from "vitest";

import { cleanBoardPaidFraction, paidShare } from "./payout";

describe("clean-board payout", () => {
  const halfPaid = { share_of_miner_pool: 0.14, unpaid_fraction: 0.5, paid_fraction: 0.5 };

  it("scales an enforced slot by its paid fraction", () => {
    expect(cleanBoardPaidFraction(halfPaid, "enforce")).toBe(0.5);
    expect(paidShare(halfPaid, "enforce")).toBeCloseTo(0.07);
  });

  it("never applies a shadow fraction", () => {
    expect(cleanBoardPaidFraction(halfPaid, "shadow")).toBeUndefined();
    expect(paidShare(halfPaid, "shadow")).toBe(0.14);
    expect(paidShare(halfPaid, undefined)).toBe(0.14);
  });

  it("pays a full or provisional slot as before", () => {
    expect(paidShare({ share_of_miner_pool: 0.65 }, "enforce")).toBe(0.65);
    const provisional = { share_of_miner_pool: 0.65, paid: false, paid_fraction: 0 };
    expect(cleanBoardPaidFraction(provisional, "enforce")).toBeUndefined();
    expect(paidShare(provisional, "enforce")).toBe(0.65);
    expect(paidShare(undefined, "enforce")).toBeUndefined();
  });
});
