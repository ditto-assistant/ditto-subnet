import { cleanup, render, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { installFixtureFetch, loadFixture } from "../../test-fixtures";
import type { SubmissionFeePayload } from "../../types/submission-fee";
import { SubmissionFee } from "./SubmissionFee";
import {
  changeLabel,
  feeChangeText,
  feeDirection,
  isFixedTao,
  quoteLifetimeText,
  raoToTao,
} from "./submission-fee";

let restoreFetch: (() => void) | null = null;

function serve(body: unknown, status = 200): void {
  const original = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    )) as typeof fetch;
  restoreFetch = () => {
    globalThis.fetch = original;
  };
}

afterEach(() => {
  cleanup();
  restoreFetch?.();
  restoreFetch = null;
});

describe("SubmissionFee", () => {
  beforeEach(() => {
    restoreFetch = installFixtureFetch();
  });

  it("shows the current fixed-TAO fee, its revision, and a compact history", async () => {
    render(() => <SubmissionFee />);
    const amount = await screen.findByText("0.1 TAO");
    expect(amount.getAttribute("data-fee-rao")).toBe("100000000");
    expect(screen.getByText(/Fixed TAO · revision 5 · since/)).toBeTruthy();
    expect(screen.getByText(/within 24 hours of reserving keeps the reserved fee/)).toBeTruthy();

    const history = document.querySelector("details.submission-fee-history");
    expect(history).not.toBeNull();
    // Collapsed by default: the history is a dropdown, not a table.
    expect((history as HTMLDetailsElement).open).toBe(false);
    const rows = Array.from(document.querySelectorAll(".submission-fee-history li")).map(
      (row) => row.textContent ?? "",
    );
    const revisions = Array.from(document.querySelectorAll(".submission-fee-history li")).map(
      (row) => row.getAttribute("data-fee-revision"),
    );
    // Platform's real shape: revision 1 is the first published fee, with no
    // previous fee (the built-in default is never shown as one); later
    // changes report the revision they replaced.
    expect(revisions).toEqual(["5", "3", "1"]);
    expect(rows[0]).toContain("0.2 → 0.1 TAO");
    expect(rows[0]).toContain("down 50%");
    expect(rows[1]).toContain("0.04 → 0.2 TAO");
    expect(rows[1]).toContain("up 400%");
    expect(rows[2]).toContain("0.04 TAO");
    expect(rows[2]).not.toContain("→");
    expect(rows[2]).toContain("first published fee");
    expect(document.body.textContent).not.toContain("initial");
  });

  it("never shows operator identity or reasons", async () => {
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/actor|reason|@/i);
  });
});

describe("SubmissionFee unavailable states", () => {
  it("says the fee is unavailable when the API fails, rather than guessing", async () => {
    serve({ detail: "down" }, 503);
    render(() => <SubmissionFee />);
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toBe("Submission fee is unavailable."),
    );
  });

  it("refuses to render an unreviewed denomination as a TAO fee", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({ ...fixture, fee_denomination: "usd_indexed", fee_amount_rao: 5 });
    render(() => <SubmissionFee />);
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toBe("Submission fee is unavailable."),
    );
    expect(document.body.textContent).not.toContain("TAO");
  });
});

describe("SubmissionFee edge payloads", () => {
  it("never renders an unreviewed-denomination history row as TAO and marks history incomplete", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    const [first, second] = fixture.history;
    serve({
      ...fixture,
      history: [{ ...first, fee_denomination: "usd_indexed" }, second],
      history_truncated: false,
    });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    const revisions = Array.from(document.querySelectorAll(".submission-fee-history li")).map(
      (row) => row.getAttribute("data-fee-revision"),
    );
    expect(revisions).toEqual(["3"]);
    expect(document.querySelector(".submission-fee-count")?.textContent).toBe("1+");
    expect(document.querySelector(".submission-fee-incomplete")?.textContent).toBe(
      "Some fee changes are not shown.",
    );
  });

  it("says in words when Platform truncated the history", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({ ...fixture, history_truncated: true });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    expect(document.querySelectorAll(".submission-fee-history li")).toHaveLength(3);
    expect(screen.getByText("Some fee changes are not shown.")).toBeTruthy();
  });

  it("adds no truncation note to a complete history", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({ ...fixture, history_truncated: false });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    expect(document.querySelector(".submission-fee-incomplete")).toBeNull();
  });

  it("omits the effective date when the API has none, and handles empty history", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({ ...fixture, fee_effective_at: null, history: [], history_truncated: false });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    const meta = document.querySelector(".submission-fee-meta")?.textContent ?? "";
    expect(meta).toContain("revision 5");
    expect(meta).not.toContain("since");
    expect(meta).not.toContain("Not recorded");
    // No changes to list: the empty history control is not rendered.
    expect(document.querySelector("details.submission-fee-history")).toBeNull();
  });
});

describe("SubmissionFee revision states", () => {
  it("names the built-in default when no operator revision exists", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({
      ...fixture,
      policy_revision: 0,
      fee_revision: 0,
      fee_amount_rao: 40_000_000,
      fee_amount_tao: "0.040000000",
      fee_effective_at: null,
      history: [],
      history_truncated: false,
    });
    render(() => <SubmissionFee />);
    await screen.findByText("0.04 TAO");
    const meta = document.querySelector(".submission-fee-meta")?.textContent ?? "";
    expect(meta).toBe("Built-in default (no operator revision yet)");
    expect(document.querySelector("details.submission-fee-history")).toBeNull();
  });

  it("says the built-in fee was never changed when only cooldowns were revised", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({
      ...fixture,
      policy_revision: 4,
      fee_revision: 0,
      fee_amount_rao: 40_000_000,
      fee_amount_tao: "0.040000000",
      fee_effective_at: null,
      history: [],
      history_truncated: false,
    });
    render(() => <SubmissionFee />);
    await screen.findByText("0.04 TAO");
    expect(document.querySelector(".submission-fee-meta")?.textContent).toBe(
      "Built-in default fee (never changed by an operator)",
    );
  });

  it("says when the revision is outside the scanned history", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    serve({ ...fixture, fee_revision: null, fee_effective_at: null, history_truncated: true });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    expect(document.querySelector(".submission-fee-meta")?.textContent).toBe(
      "Fixed TAO · revision not in the scanned history",
    );
  });

  it("labels a change whose previous fee was not published", async () => {
    const fixture = loadFixture<SubmissionFeePayload>("submission-fee");
    const [newest, second] = fixture.history;
    serve({
      ...fixture,
      history: [
        newest,
        { ...second, previous_fee_amount_rao: null, previous_fee_amount_tao: null },
      ],
      history_truncated: true,
    });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    const labels = Array.from(document.querySelectorAll(".submission-fee-direction")).map(
      (node) => node.textContent,
    );
    expect(labels).toEqual(["down 50%", "previous fee not shown"]);
  });
});

describe("submission fee helpers", () => {
  it("renders rao as exact TAO without floating point", () => {
    expect(raoToTao(37_271_710)).toBe("0.03727171");
    expect(raoToTao(100_000_000)).toBe("0.1");
    expect(raoToTao(1)).toBe("0.000000001");
    expect(raoToTao(1_000_000_000)).toBe("1");
    expect(raoToTao(10_000_000_001)).toBe("10.000000001");
    expect(raoToTao(Number.NaN)).toBe("—");
    expect(raoToTao(-1)).toBe("—");
  });

  it("describes changes and direction", () => {
    expect(feeChangeText(null, 40_000_000)).toBe("0.04 TAO");
    expect(feeChangeText(100_000_000, 37_271_710)).toBe("0.1 → 0.03727171 TAO");
    expect(feeDirection(null, 40_000_000)).toBe("");
    expect(feeDirection(40_000_000, 40_000_000)).toBe("");
    expect(feeDirection(100_000_000, 37_271_710)).toBe("down 63%");
    expect(feeDirection(1_000_000_000, 1_000_000_001)).toBe("up <1%");
    // The <1% boundary is exact, before rounding.
    expect(feeDirection(100_000_000, 100_500_000)).toBe("up <1%"); // +0.5%
    expect(feeDirection(100_000_000, 100_990_000)).toBe("up <1%"); // +0.99%
    expect(feeDirection(100_000_000, 101_000_000)).toBe("up 1%"); // +1%
    expect(feeDirection(100_000_000, 99_500_000)).toBe("down <1%"); // -0.5%
    expect(feeDirection(100_000_000, 99_000_000)).toBe("down 1%"); // -1%
    expect(feeDirection(100_000_000, 101_500_000)).toBe("up 2%"); // +1.5% rounds half up
    expect(feeDirection(100_000_000, 1_000_000)).toBe("down 99%");
    expect(feeDirection(40_000_000, 200_000_000)).toBe("up 400%");
    expect(feeDirection(1_000_000_000_000, 1)).toBe("down >99%");
    expect(isFixedTao("fixed_tao")).toBe(true);
    expect(isFixedTao("usd_indexed")).toBe(false);
    expect(quoteLifetimeText(86_400)).toBe("24 hours");
    expect(quoteLifetimeText(3_600)).toBe("1 hour");
    expect(quoteLifetimeText(5_400)).toBe("90 minutes");
    expect(quoteLifetimeText(0)).toBe("the quote lifetime");
    expect(quoteLifetimeText(59)).toBe("59 seconds");
    expect(quoteLifetimeText(5_430)).toBe("5430 seconds");
    expect(quoteLifetimeText(60)).toBe("1 minute");
    expect(changeLabel(null, 40_000_000, true)).toBe("first published fee");
    expect(changeLabel(null, 40_000_000, false)).toBe("previous fee not shown");
    expect(changeLabel(40_000_000, 40_000_000, false)).toBe("same amount");
  });
});
