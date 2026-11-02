import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getJSON } from "../../lib/api";
import { loadFixture } from "../../test-fixtures";
import { GeneratorOverviewCard } from "./GeneratorOverviewCard";

vi.mock("../../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);
beforeEach(() => {
  vi.mocked(getJSON).mockReset();
});

const epoch = () => ({
  next_epoch_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  epoch_seconds: 4320,
});

it("summarises the scored round, the leader and the next round", async () => {
  const routes: Record<string, unknown> = {
    "/generator/board": loadFixture("generator-board"),
    "/generator/rounds": loadFixture("generator-rounds"),
  };
  vi.mocked(getJSON).mockImplementation((path: string) =>
    Promise.resolve(structuredClone(routes[path])),
  );
  render(() => <GeneratorOverviewCard epoch={epoch} />);
  const link = await screen.findByRole("link", { name: /Generator competition/ });
  expect(link).toHaveAttribute("href", "/generators");
  expect(link).toHaveTextContent("Round 25967 scored");
  expect(link).toHaveTextContent("Leader 5CXEMm6u…iMVhJa · 0.368");
  expect(link).toHaveTextContent("Next round in 10m");
});

it("stays hidden when the generator board is unavailable", async () => {
  vi.mocked(getJSON).mockRejectedValue(new Error("HTTP 404"));
  render(() => <GeneratorOverviewCard epoch={epoch} />);
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.queryByRole("link", { name: /Generator competition/ })).not.toBeInTheDocument();
});

it("names the leading generator when its miner named it", async () => {
  const board = loadFixture<{ entries: Record<string, unknown>[] }>("generator-board");
  (board.entries[0] as Record<string, unknown>).name = "Needle Forge";
  const routes: Record<string, unknown> = {
    "/generator/board": board,
    "/generator/rounds": loadFixture("generator-rounds"),
  };
  vi.mocked(getJSON).mockImplementation((path: string) =>
    Promise.resolve(structuredClone(routes[path])),
  );
  render(() => <GeneratorOverviewCard epoch={epoch} />);
  const link = await screen.findByRole("link", { name: /Generator competition/ });
  expect(link).toHaveTextContent("Leader Needle Forge · 0.368");
});
