import { describe, expect, it } from "vitest";

import { confirmationSampleComposites } from "./confirmation-samples";

describe("confirmationSampleComposites", () => {
  it("keeps retest samples visible when the agent is absent from the leaderboard", () => {
    expect(confirmationSampleComposites(undefined, [0.94, 0.95])).toEqual([0.94, 0.95]);
  });

  it("prefers the current leaderboard fold when present", () => {
    expect(confirmationSampleComposites([0.92], [0.94, 0.95])).toEqual([0.92]);
  });
});
