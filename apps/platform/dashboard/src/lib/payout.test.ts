import { describe, expect, it } from "vitest";

import { cleanBoardPaidFraction, paidShare, paidShareRange, shareRangeText } from "./payout";

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

  it("ranges a joint crown whose members are paid differently", () => {
    const attested = { share_of_miner_pool: 0.14 };
    const joint = [attested, halfPaid, attested];
    expect(paidShareRange(joint, "enforce")).toEqual([0.07, 0.14]);
    expect(paidShareRange(joint, "shadow")).toEqual([0.14, 0.14]);
    expect(paidShareRange([], "enforce")).toBeUndefined();
    const pct = (value: number) => (value * 100).toFixed(1) + "%";
    expect(shareRangeText([0.07, 0.14], pct)).toBe("7.0%–14.0%");
    expect(shareRangeText([0.14, 0.14], pct)).toBe("14.0%");
  });
});
