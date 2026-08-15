import { describe, expect, it } from "vitest";

import type { FleetEntry, ValidatorUpdaterStatus } from "../../types/fleet";
import { updaterModeLine, updaterView } from "./updater";

const DESCRIPTOR = `ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:a62c6be5${"0".repeat(56)}`;

function status(overrides: Partial<ValidatorUpdaterStatus> = {}): ValidatorUpdaterStatus {
  return {
    enabled: true,
    channel: "compat-2",
    state: "idle",
    transaction_phase: null,
    current_descriptor: DESCRIPTOR,
    current_version: "0.69.0",
    candidate_descriptor: null,
    candidate_version: null,
    failed_candidate_count: 0,
    retry_after: null,
    suppressed: false,
    last_success_at: null,
    last_failure_at: null,
    last_failure_reason: null,
    observed_at: 1_786_820_512,
    ...overrides,
  };
}

function entry(updater: ValidatorUpdaterStatus | null | undefined): FleetEntry {
  return {
    validator_hotkey: "5ManagedValidator",
    stack: { mode: "managed" },
    updater_status: updater,
  };
}

describe("managed updater display", () => {
  it("keeps an idle managed updater compact", () => {
    const validator = entry(status());
    expect(updaterModeLine(validator)).toBe("Managed · compat-2");
    expect(updaterView(validator)).toBeNull();
  });

  it("explains a safe drain and falls back to the signed digest target", () => {
    const validator = {
      ...entry(
        status({
          state: "draining",
          transaction_phase: "prepared",
          candidate_descriptor: DESCRIPTOR,
        }),
      ),
      active_benchmarks: [{ slot_id: "slot-2" }, { slot_id: "slot-4" }],
    } satisfies FleetEntry;

    expect(updaterView(validator)).toMatchObject({
      label: "Safe drain",
      tone: "progress",
      target: "Target a62c6be5…",
      summary: "Finishing 2 active runs before restart · no new work.",
    });
    expect(updaterView(validator)?.title).toContain("resumes the old stack");
  });

  it("names a reported candidate version when available", () => {
    expect(
      updaterView(
        entry(
          status({
            state: "prefetched",
            candidate_descriptor: DESCRIPTOR,
            candidate_version: "0.70.0",
          }),
        ),
      )?.target,
    ).toBe("Target v0.70.0");
  });

  it("makes failed and suppressed candidates actionable", () => {
    expect(
      updaterView(
        entry(
          status({
            state: "suppressed",
            candidate_descriptor: DESCRIPTOR,
            failed_candidate_count: 3,
            suppressed: true,
            retry_after: 1_786_821_000,
            last_failure_reason: "candidate_readiness_failed",
          }),
        ),
      ),
    ).toMatchObject({
      label: "Update blocked",
      tone: "bad",
      summary: "3 failed attempts; automatic retries are suppressed.",
      title: "Last failure: candidate_readiness_failed",
    });
  });

  it("does not silently hide missing managed telemetry", () => {
    expect(updaterModeLine(entry(null))).toBe("Updater not reported");
    expect(updaterView(entry(null))).toMatchObject({
      label: "Updater not reported",
      tone: "unknown",
    });
    expect(updaterView({ stack: { mode: "source" } })).toBeNull();
  });
});
