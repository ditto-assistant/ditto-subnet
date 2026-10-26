import { cleanup, render, screen, waitFor, within } from "@solidjs/testing-library";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getJSON } from "../lib/api";
import { loadFixture } from "../test-fixtures";
import { GeneratorsPage } from "./GeneratorsPage";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);

const LIVE: Record<string, unknown> = {
  "/generator/board": loadFixture("generator-board"),
  "/generator/rounds": loadFixture("generator-rounds"),
  "/generator/rounds/25963": loadFixture("generator-round-25963"),
  "/generator/upload-terms": loadFixture("generator-upload-terms"),
  "/public/treasury-allocation": loadFixture("treasury-allocation"),
  "/public/weights": {
    epoch: { next_epoch_at: new Date(Date.now() + 25 * 60_000).toISOString(), epoch_seconds: 4320 },
  },
};

const SPLIT = {
  emission_allocation_revision: 1,
  emission_split_source: "epoch_pin",
  emission_split_epoch: 25963,
  emission_split: [
    { kind: "burn", id: null, share: 0.5, bps: 5000 },
    { kind: "track", id: "memory", share: 0.35, bps: 3500 },
    { kind: "track", id: "generator", share: 0.15, bps: 1500 },
    { kind: "gamma", id: "gm", share: 0, bps: 0 },
  ],
};

function serve(overrides: Record<string, unknown> = {}): void {
  const routes = { ...LIVE, ...overrides };
  vi.mocked(getJSON).mockImplementation((path: string) => {
    const body = routes[path];
    if (body instanceof Error) return Promise.reject(body);
    return body === undefined
      ? Promise.reject(new Error("HTTP 404 " + path))
      : Promise.resolve(structuredClone(body));
  });
}

beforeEach(() => {
  vi.mocked(getJSON).mockReset();
});

it("renders the live round, projected payouts and the unpublished split", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const payouts = await screen.findByRole("table", { name: "Projected generator payouts" });
  expect(screen.getByText("Paying")).toBeInTheDocument();
  expect(await screen.findByText("Round 25963 results")).toBeInTheDocument();
  // Four eligible generators split the first four rank shares over 0.92.
  const rows = within(payouts).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(4);
  expect(rows[0]).toHaveTextContent("#1");
  expect(rows[0]).toHaveTextContent("5CXEMm6u");
  expect(rows[0]).toHaveTextContent("0.347");
  expect(rows[0]).toHaveTextContent("43.48%");
  expect(rows[0]).toHaveTextContent("Split not published");
  expect(
    screen.getByText(/split between agent and generator miners is not yet published/),
  ).toBeInTheDocument();
  expect(screen.getByText("Burn, forecast").nextElementSibling).toHaveTextContent("50%");
  // Next round 25964 is committed and opens at the next epoch boundary.
  expect(screen.getByText("Next round").nextElementSibling).toHaveTextContent("25964 opens in 25m");
  expect(screen.getByText(/Published · 3 confirmations/)).toBeInTheDocument();
  expect(screen.getByText("Public default generator")).toBeInTheDocument();
  expect(screen.getByText("32 cases per generator", { exact: false })).toBeInTheDocument();
  expect(screen.getByText("0.05 TAO")).toBeInTheDocument();
});

it("shows the published split and each generator's share of miner emission", async () => {
  serve({
    "/public/treasury-allocation": { ...(LIVE["/public/treasury-allocation"] as object), ...SPLIT },
  });
  render(() => <GeneratorsPage />);
  const legend = await screen.findByText("Generator miners");
  expect(legend.nextElementSibling).toHaveTextContent("15%");
  expect(screen.getByText("Agent miners").nextElementSibling).toHaveTextContent("35%");
  // Zero-share recipients stay out of the bar and the legend.
  expect(screen.queryByText(/Gamma · gm/)).not.toBeInTheDocument();
  expect(screen.getByText("Epoch pin 25963")).toBeInTheDocument();
  // 0.4 / 0.92 of a 15% track.
  await waitFor(() => expect(screen.getByText("6.52%")).toBeInTheDocument());
  expect(screen.queryByText("Split not published")).not.toBeInTheDocument();
});

it("says the generator share pays agent miners when nothing is eligible", async () => {
  const board = loadFixture<{ entries: { weight_eligible: boolean }[] }>("generator-board");
  board.entries.forEach((row) => (row.weight_eligible = false));
  serve({ "/generator/board": board });
  render(() => <GeneratorsPage />);
  expect(
    await screen.findByText(
      /No generator is eligible right now, so the generator share pays agent miners/,
    ),
  ).toBeInTheDocument();
  expect(screen.getAllByText("Not weight-eligible")).toHaveLength(4);
});

it("reports unavailable data instead of inventing a schedule or payouts", async () => {
  serve({
    "/generator/board": new Error("offline"),
    "/generator/rounds": new Error("offline"),
  });
  render(() => <GeneratorsPage />);
  expect(await screen.findByText(/Round schedule could not be loaded/)).toBeInTheDocument();
  expect(await screen.findByText(/Generator board could not be loaded/)).toBeInTheDocument();
  expect(screen.queryByText("Projected payouts")).not.toBeInTheDocument();
  expect(screen.queryByText("Current round")).not.toBeInTheDocument();
});
