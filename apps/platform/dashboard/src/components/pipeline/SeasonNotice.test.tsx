import { cleanup, render, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RolloutState } from "../../types/leaderboard";
import { SeasonNotice, closedSeason } from "./SeasonNotice";

let restoreFetch: (() => void) | null = null;

function serveRollout(body: RolloutState): void {
  const original = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
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
  vi.useRealTimers();
});

describe("dueling-season notice", () => {
  it("announces only a closed season", () => {
    expect(closedSeason({ dueling_season: { open: true, bench_version: 18 } })).toBeNull();
    expect(closedSeason({ dueling_season: null })).toBeNull();
    expect(closedSeason({})).toBeNull();
    expect(closedSeason({ dueling_season: { open: false, bench_version: 18 } })).toEqual({
      open: false,
      bench_version: 18,
    });
  });

  it("says when scoring resumes while no season is open", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-10T22:14:00Z"));
    serveRollout({
      active_version: 18,
      desired_version: 18,
      dueling_season: {
        open: false,
        bench_version: 18,
        next_opens_at: "2026-11-11T00:45:00Z",
        next_round_epoch: 44,
      },
    });
    const { container } = render(() => <SeasonNotice />);
    await waitFor(() => expect(container.querySelector("#season-notice")).not.toBeNull());
    expect(container.querySelector("#season-notice")).toHaveTextContent(
      "No dueling season is open for bench 18. Scoring resumes when the next season opens, " +
        "about 00:45 UTC (in 2h 31m).",
    );
  });

  it("stays hidden while a season is open", async () => {
    serveRollout({ dueling_season: { open: true, bench_version: 18 } });
    const { container } = render(() => <SeasonNotice />);
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container.querySelector("#season-notice")).toBeNull();
  });
});
