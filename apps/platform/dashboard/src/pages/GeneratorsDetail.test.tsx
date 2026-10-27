import { cleanup, fireEvent, render, screen, waitFor, within } from "@solidjs/testing-library";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getJSON } from "../lib/api";
import { loadFixture } from "../test-fixtures";
import type {
  GeneratorBoard,
  GeneratorRoundDetail,
  GeneratorSubmissionDetail,
} from "../components/generators/generator-data";
import { refreshAllEndpoints } from "../data/useEndpoint";
import { showGenerator } from "../components/generators/GeneratorIdentity";
import { GeneratorsPage } from "./GeneratorsPage";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));

const TOP = "7241781f-99d0-4d96-a908-ac1281cca36f";
const TOP_HOTKEY = "5CXEMm6u6onoMpFPbpSqFLdeDzkG68nyTirSg3SVQfiMVhJa";
const AVATAR = "/api/v1/public/miners/" + TOP_HOTKEY + "/avatar";

/** The live board with the identity Platform adds for a named generator. */
function namedBoard(): GeneratorBoard {
  const board = loadFixture<GeneratorBoard>("generator-board");
  const top = board.entries[0];
  if (!top) throw new Error("fixture has no entries");
  Object.assign(top, {
    name: "Needle Forge",
    avatar_url: AVATAR,
    version: 3,
    status: "active",
    rounds_in_window: 4,
    recent: [0.21, 0.3, 0.28, top.score].map((score, i) => ({
      round_index: 25960 + i,
      submission_id: TOP,
      score,
      rank: 1,
      columns: 5,
      scored_at: top.scored_at,
    })),
  });
  return board;
}

function roundDetail(): GeneratorRoundDetail {
  const detail = loadFixture<GeneratorRoundDetail>("generator-round-25967");
  const column = detail.columns.find((c) => c.submission_id === TOP);
  if (column) Object.assign(column, { name: "Needle Forge", avatar_url: AVATAR, version: 3 });
  // The public default fills a column while too few miner generators
  // qualify; it has no owner, so it renders without a panel link.
  detail.columns.push({
    ...(detail.columns[0] as GeneratorRoundDetail["columns"][number]),
    column_id: "default",
    submission_id: "72b1ed56-f86a-4076-b61e-a4c6179cfd73",
    miner_hotkey: null,
    public_default: true,
    counted: true,
    name: null,
    avatar_url: null,
    version: null,
  });
  const standing = detail.standings[0];
  if (standing) Object.assign(standing, { agent_name: "dittobench-champ", agent_version: 7 });
  return detail;
}

function submissionDetail(): GeneratorSubmissionDetail {
  const base = {
    submission_id: TOP,
    miner_hotkey: TOP_HOTKEY,
    source_sha256: "ab".repeat(32),
    tree_sha256: "cd".repeat(32),
    module: "example.com/needle",
    go_version: "1.23",
    file_count: 6,
    size_bytes: 20480,
    status: "active" as const,
    status_reason: null,
    next_run_at: null,
    submitted_at: "2026-10-26T05:12:23.222328Z",
    latest_run: null,
    qualified_at: "2026-10-26T05:40:00Z",
    public_default: false,
    round_score: 0.347,
    round_scored_index: 25963,
    name: "Needle Forge",
    avatar_url: AVATAR,
    version: 3,
  };
  return {
    submission: base,
    versions: [
      base,
      {
        ...base,
        submission_id: "11111111-2222-4333-8444-555555555555",
        name: null,
        version: 2,
        status: "superseded",
        round_score: 0.12,
      },
    ],
    rounds: [25963, 25962, 25961].map((round_index, i) => ({
      round_index,
      scored_at: "2026-10-26T20:11:42Z",
      column_id: "c" + i,
      counted: true,
      score: [0.347, 0.3, 0.21][i] as number,
      rank: i + 1,
      columns: 5,
      discriminating: 26 - i,
      trivial: 3,
      unsolved: 2,
      duplicate: 1,
      panel_size: 5,
    })),
  };
}

let routes: Record<string, unknown>;

function serve(overrides: Record<string, unknown> = {}): void {
  routes = {
    "/generator/board": namedBoard(),
    "/generator/rounds": loadFixture("generator-rounds"),
    "/generator/rounds/25967": roundDetail(),
    "/generator/upload-terms": loadFixture("generator-upload-terms"),
    "/public/treasury-allocation": loadFixture("treasury-allocation"),
    "/public/weights": { epoch: null },
    ["/generator/submissions/" + TOP]: submissionDetail(),
    ...overrides,
  };
  vi.mocked(getJSON).mockImplementation((path: string) => {
    const body = routes[path];
    if (body instanceof Promise) return body as Promise<never>;
    if (body instanceof Error) return Promise.reject(body);
    return body === undefined
      ? Promise.reject(new Error("HTTP 404 " + path))
      : Promise.resolve(structuredClone(body));
  });
}

beforeEach(() => {
  vi.mocked(getJSON).mockReset();
  history.replaceState(null, "", "/generators");
});
afterEach(cleanup);

it("shows each generator's name, avatar, version, owner and recent form", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const board = await screen.findByRole("table", { name: "Projected generator payouts" });
  const rows = within(board).getAllByRole("row").slice(1);
  const top = rows[0] as HTMLElement;
  expect(within(top).getByRole("link", { name: "Needle Forge" })).toHaveAttribute(
    "href",
    expect.stringContaining("generator=" + TOP),
  );
  expect(top).toHaveTextContent("v3");
  expect(top.querySelector("img.miner-avatar")).toHaveAttribute("src", AVATAR);
  expect(within(top).getByTitle("Open the miner profile")).toHaveTextContent("5CXEMm6u");
  expect(
    within(top).getByRole("img", { name: /Round score trend over 4 scored rounds/ }),
  ).toBeTruthy();
  // An unnamed generator still reads as an identifiable row.
  expect(
    within(rows[1] as HTMLElement).getByRole("link", { name: /^Generator [0-9a-f]{8}$/ }),
  ).toBeTruthy();
});

it("names generators and panel agents in the latest round", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const results = await screen.findByRole("table", { name: "Generators in this round" });
  expect(await within(results).findByRole("link", { name: "Needle Forge" })).toBeTruthy();
  expect(within(results).getByText("Public default generator")).toBeTruthy();
  const panel = screen.getByRole("table", { name: "Panel agents on this round's cases" });
  expect(within(panel).getByText("dittobench-champ")).toBeTruthy();
  expect(within(panel).getByText("v7")).toBeTruthy();
});

it("loads the latest round from the board before the round list answers", async () => {
  serve({ "/generator/rounds": new Promise(() => undefined) });
  render(() => <GeneratorsPage />);
  expect(await screen.findByText("Round 25967 results")).toBeInTheDocument();
  expect(vi.mocked(getJSON)).toHaveBeenCalledWith("/generator/rounds/25967");
  // The schedule is still loading, shown as a placeholder, not a blank.
  expect(screen.getByRole("status", { name: "Loading rounds" })).toBeInTheDocument();
});

it("shows skeletons while the board loads instead of an empty section", async () => {
  serve({ "/generator/board": new Promise(() => undefined) });
  render(() => <GeneratorsPage />);
  expect(await screen.findByRole("status", { name: "Loading the generator board" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Generator board" })).toBeInTheDocument();
});

it("opens a generator's panel with its rounds, lifecycle and other versions", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const board = await screen.findByRole("table", { name: "Projected generator payouts" });
  fireEvent.click(within(board).getByRole("link", { name: "Needle Forge" }));
  const panel = await screen.findByRole("dialog", { name: "Needle Forge" });
  expect(location.search).toContain("generator=" + TOP);
  await within(panel).findByText("Payout position");
  expect(within(panel).getByText("Payout position").nextElementSibling).toHaveTextContent("#1");
  expect(within(panel).getByText("Rounds scored").nextElementSibling).toHaveTextContent("3");
  expect(within(panel).getByText("Best rank").nextElementSibling).toHaveTextContent("#1");
  expect(within(panel).getByRole("img", { name: /Score in 3 scored rounds/ })).toBeTruthy();
  const table = within(panel).getByRole("table", { name: "Results in each scored round" });
  expect(within(table).getAllByRole("row")).toHaveLength(4);
  expect(within(panel).getByText("example.com/needle")).toBeInTheDocument();
  expect(within(panel).getByText(/Generator source stays private/)).toBeInTheDocument();
  // The miner's earlier, unnamed version links to its own panel.
  expect(within(panel).getByRole("link", { name: "Generator 11111111" })).toBeTruthy();
  expect(within(panel).getByText("Superseded")).toBeInTheDocument();

  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(location.search).not.toContain("generator=");
});

it("opens straight from a shared link and retries a failed panel load", async () => {
  history.replaceState(null, "", "/generators?generator=" + TOP);
  serve({ ["/generator/submissions/" + TOP]: new Error("offline") });
  render(() => <GeneratorsPage />);
  const alert = await screen.findByText(/This generator could not be loaded/);
  serve();
  fireEvent.click(within(alert.parentElement as HTMLElement).getByRole("button"));
  expect(await screen.findByRole("dialog", { name: "Needle Forge" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Close generator" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("labels the round detail by the round it shows while a newer one loads", async () => {
  serve();
  render(() => <GeneratorsPage />);
  expect(await screen.findByText("Round 25967 results")).toBeInTheDocument();
  // The next poll names a newer scored round whose detail is still loading.
  const board = namedBoard();
  board.entries.forEach((entry) => (entry.round_index = 25968));
  routes["/generator/board"] = board;
  routes["/generator/rounds/25968"] = new Promise(() => undefined);
  refreshAllEndpoints();
  await waitFor(() => expect(vi.mocked(getJSON)).toHaveBeenCalledWith("/generator/rounds/25968"));
  expect(screen.getByText("Round 25967 results")).toBeInTheDocument();
  expect(screen.queryByText("Round 25968 results")).toBeNull();
});

it("adds no history entry when the open generator is clicked again", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const board = await screen.findByRole("table", { name: "Projected generator payouts" });
  const link = within(board).getByRole("link", { name: "Needle Forge" });
  fireEvent.click(link);
  await screen.findByRole("dialog", { name: "Needle Forge" });
  const depth = history.length;
  showGenerator(TOP);
  expect(history.length).toBe(depth);
});

it("omits the rounds count when the board does not report it", async () => {
  const board = namedBoard();
  delete (board.entries[0] as { rounds_in_window?: number }).rounds_in_window;
  serve({ "/generator/board": board });
  render(() => <GeneratorsPage />);
  const table = await screen.findByRole("table", { name: "Projected generator payouts" });
  const top = within(table).getAllByRole("row")[1] as HTMLElement;
  expect(top).not.toHaveTextContent("undefined");
  expect(within(top).getByRole("img", { name: /Round score trend/ })).toBeTruthy();
});

it("ignores a malformed generator id in the URL", async () => {
  history.replaceState(null, "", "/generators?generator=../../etc");
  serve();
  render(() => <GeneratorsPage />);
  await screen.findByRole("table", { name: "Projected generator payouts" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(vi.mocked(getJSON).mock.calls.some(([path]) => path.includes("etc"))).toBe(false);
});
