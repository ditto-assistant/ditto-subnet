import { describe, expect, it } from "vitest";

import { parkedReading } from "./pipeline";
import type { PipelineEntryExt } from "./pipeline";

function entry(overrides: Partial<PipelineEntryExt>): PipelineEntryExt {
  return { agent_id: "a", status: "waiting_validator", ...overrides } as PipelineEntryExt;
}

describe("parkedReading", () => {
  it("names Ditto only when the API published an agreed no-fault cause", () => {
    const read = parkedReading(
      entry({
        retry_state: "exhausted",
        retry_disposition: "operator_hold",
        hold_failure_code: "provider_outage_parked",
      }),
    );
    expect(read?.tone).toBe("hold");
    expect(read?.label).toBe("On hold · Ditto-side failure");
    expect(read?.title).toContain("provider outage");
    expect(read?.title).toContain("not anything in this submission");
  });

  it("asserts no fault at all on a hold with no agreed cause", () => {
    // The classifier sends mixed, unnamed, stale and unactionable rows here.
    // Those are unattributed, not proven fleet failures, and the copy has to
    // say only that much.
    const read = parkedReading(
      entry({ retry_state: "exhausted", retry_disposition: "operator_hold" }),
    );
    expect(read?.tone).toBe("hold");
    expect(read?.label).toBe("On hold · needs operator review");
    expect(read?.title).toContain("has not attributed this to either side");
    for (const claim of ["Ditto-side", "not anything in this submission", "fleet failure"]) {
      expect(read?.title).not.toContain(claim);
    }
    expect(read?.label).not.toContain("Ditto");
  });

  it("names the code and the next step on a terminal artifact failure", () => {
    const read = parkedReading(
      entry({
        retry_state: "exhausted",
        retry_disposition: "terminal_artifact_failure",
        terminal_failure_code: "inference_request_rejected",
      }),
    );
    expect(read?.tone).toBe("terminal");
    expect(read?.label).toContain("inference_request_rejected");
    expect(read?.title).toContain("cannot finish scoring");
    expect(read?.title).toContain("submit a new version");
  });

  it("never implies a refund or a payment outcome", () => {
    const rows = [
      { retry_disposition: "operator_hold", hold_failure_code: "provider_outage_parked" },
      { retry_disposition: "operator_hold" },
      {
        retry_disposition: "terminal_artifact_failure",
        terminal_failure_code: "inference_allowance_exhausted",
      },
    ];
    for (const row of rows) {
      const read = parkedReading(entry({ retry_state: "exhausted", ...row }));
      const text = (read?.label || "") + " " + (read?.title || "");
      for (const word of ["refund", "fee", "TAO", "credit", "reimburse"]) {
        expect(text.toLowerCase()).not.toContain(word.toLowerCase());
      }
    }
  });

  it("falls back to the unattributed reading when the wire carries no disposition", () => {
    const read = parkedReading(entry({ retry_state: "exhausted" }));
    expect(read?.tone).toBe("hold");
    expect(read?.label).toBe("On hold · needs operator review");
  });

  it("says nothing about a row that is still advancing", () => {
    expect(parkedReading(entry({ retry_state: "queued" }))).toBeNull();
    expect(parkedReading(entry({ retry_state: "cooling_down" }))).toBeNull();
    expect(parkedReading(entry({ retry_state: "running" }))).toBeNull();
  });
});
