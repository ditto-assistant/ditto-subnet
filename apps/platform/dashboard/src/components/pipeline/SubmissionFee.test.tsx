import { cleanup, render, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { installFixtureFetch, loadFixture } from "../../test-fixtures";
import type { SubmissionFeePayload } from "../../types/submission-fee";
import { SubmissionFee } from "./SubmissionFee";
import {
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
    expect(revisions).toEqual(["5", "3", "1"]);
    expect(rows[0]).toContain("0.2 → 0.1 TAO");
    expect(rows[0]).toContain("down 50%");
    expect(rows[1]).toContain("0.04 → 0.2 TAO");
    expect(rows[1]).toContain("up 400%");
    expect(rows[2]).toContain("0.04 TAO");
    expect(rows[2]).toContain("initial");
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
    const [first, second, third] = fixture.history;
    serve({
      ...fixture,
      history: [first, { ...second, fee_denomination: "usd_indexed" }, third],
      history_truncated: false,
    });
    render(() => <SubmissionFee />);
    await screen.findByText("0.1 TAO");
    const revisions = Array.from(document.querySelectorAll(".submission-fee-history li")).map(
      (row) => row.getAttribute("data-fee-revision"),
    );
    expect(revisions).toEqual(["5", "1"]);
    expect(document.querySelector(".submission-fee-count")?.textContent).toBe("2+");
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
    expect(document.querySelector(".submission-fee-count")?.textContent).toBe("0");
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
    expect(isFixedTao("fixed_tao")).toBe(true);
    expect(isFixedTao("usd_indexed")).toBe(false);
    expect(quoteLifetimeText(86_400)).toBe("24 hours");
    expect(quoteLifetimeText(3_600)).toBe("1 hour");
    expect(quoteLifetimeText(5_400)).toBe("90 minutes");
    expect(quoteLifetimeText(0)).toBe("the quote lifetime");
  });
});
