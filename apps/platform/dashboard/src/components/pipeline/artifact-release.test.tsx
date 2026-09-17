import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArtifactReleaseCard } from "../evidence/ArtifactRelease";
import { artifactReleaseCopy, artifactReleaseNote } from "./artifact-release";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("completed winner earnings source release", () => {
  it.each([undefined, null, "", "invalid"])(
    "withholds old available responses without payout proof (%s)",
    (emission_confirmed_at) => {
      const release = {
        status: "available",
        download_available: true,
        weight_confirmed_at: "2026-09-01T12:00:00Z",
        available_at: "2026-09-03T12:00:00Z",
        emission_confirmed_at,
        embargo_hours: 48,
      };
      render(() => <ArtifactReleaseCard agentId="agent" release={release} />);
      expect(screen.getByText("Awaiting winner earnings")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Download submitted source" })).toBeNull();
      expect(artifactReleaseNote(release)).toBeNull();
    },
  );
  it("does not show an old weight-based embargo timer", () => {
    expect(
      artifactReleaseCopy({
        status: "embargoed",
        weight_confirmed_at: "2026-09-01T12:00:00Z",
        available_at: "2026-09-03T12:00:00Z",
        embargo_hours: 48,
      }),
    ).toMatchObject({
      state: "unavailable",
      label: "Awaiting winner earnings",
      detail: expect.stringContaining("48-hour privacy window"),
    });
  });
  it("shows the authoritative 48-hour deadline after completed winner earnings", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-17T12:00:00Z"));
    const release = {
      status: "embargoed",
      emission_confirmed_at: "2026-09-17T12:00:00Z",
      available_at: "2026-09-19T12:00:00Z",
      embargo_hours: 48,
    };
    expect(artifactReleaseCopy(release)).toMatchObject({
      state: "embargoed",
      detail: expect.stringContaining(
        "48 hours after this submission earned winner emissions in a completed tempo",
      ),
    });
    expect(artifactReleaseNote(release)?.text).toContain("Privacy window ·");
  });
  it("offers the download when the server confirms earnings and release availability", () => {
    render(() => (
      <ArtifactReleaseCard
        agentId="agent"
        release={{
          status: "available",
          emission_confirmed_at: "2026-09-15T12:00:00Z",
          embargo_hours: 48,
          download_available: true,
        }}
      />
    ));
    expect(screen.getByRole("button", { name: "Download submitted source" })).toBeTruthy();
    expect(
      screen.getByText(/48-hour privacy window after this submission earned winner emissions/),
    ).toBeTruthy();
  });
  it("does not promise immediate post-review disclosure using an old weight deadline", () => {
    expect(
      artifactReleaseCopy({
        status: "under_review",
        weight_confirmed_at: "2020-01-01T00:00:00Z",
        available_at: "2020-01-03T00:00:00Z",
      })?.detail,
    ).not.toContain("public immediately");
  });
});
