import { cleanup, fireEvent, render, screen, waitFor, within } from "@solidjs/testing-library";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getJSON } from "../lib/api";
import { loadFixture } from "../test-fixtures";
import type { GeneratorRoundDetail } from "../components/generators/generator-data";
import { refreshMinerSession, setMinerSession } from "../stores/sessionStore";
import { GeneratorsPage } from "./GeneratorsPage";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));

const TOP_HOTKEY = "5CXEMm6u6onoMpFPbpSqFLdeDzkG68nyTirSg3SVQfiMVhJa";
const SECOND_HOTKEY = "5HDmNaBQNVQoyApQbFQdSx6pD4ajcJdaYDJePufpWvoxCsrq";

function roundDetail(index: number): GeneratorRoundDetail {
  const detail = loadFixture<GeneratorRoundDetail>("generator-round-25967");
  detail.summary.round_index = index;
  return detail;
}

function serve(overrides: Record<string, unknown> = {}): void {
  const routes: Record<string, unknown> = {
    "/generator/board": loadFixture("generator-board"),
    "/generator/rounds": loadFixture("generator-rounds"),
    "/generator/rounds/25967": roundDetail(25967),
    "/generator/rounds/25966": roundDetail(25966),
    "/generator/rounds/25965": roundDetail(25965),
    "/generator/upload-terms": loadFixture("generator-upload-terms"),
    "/public/treasury-allocation": loadFixture("treasury-allocation"),
    "/public/weights": { epoch: null },
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
  localStorage.clear();
  refreshMinerSession();
  history.replaceState(null, "", "/generators");
});
afterEach(cleanup);

async function roundList(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: "Recent rounds" });
  return within(heading.closest("section") as HTMLElement).getByRole("table");
}

it("opens an older round from the round list and returns to the latest", async () => {
  serve();
  render(() => <GeneratorsPage />);
  expect(await screen.findByText("Round 25967 results")).toBeInTheDocument();
  const list = await roundList();
  fireEvent.click(within(list).getByRole("link", { name: "25966" }));
  expect(location.search).toContain("round=25966");
  expect(await screen.findByText("Round 25966 results")).toBeInTheDocument();
  expect(vi.mocked(getJSON)).toHaveBeenCalledWith("/generator/rounds/25966");
  expect(within(list).getByRole("link", { name: "25966" })).toHaveAttribute("aria-current", "true");
  // The heading takes focus so keyboard and screen-reader users land on the
  // results they asked for.
  expect(document.activeElement).toHaveTextContent("Round 25966 results");

  fireEvent.click(screen.getByRole("link", { name: "Show the latest round" }));
  expect(location.search).not.toContain("round=");
  expect(await screen.findByText("Round 25967 results")).toBeInTheDocument();
});

it("opens a round straight from a shared link", async () => {
  history.replaceState(null, "", "/generators?round=25965");
  serve();
  render(() => <GeneratorsPage />);
  expect(await screen.findByText("Round 25965 results")).toBeInTheDocument();
  expect(vi.mocked(getJSON)).not.toHaveBeenCalledWith("/generator/rounds/25967");
  // The newest round's link follows new rounds instead of pinning one.
  const list = await roundList();
  expect(within(list).getByRole("link", { name: "25967" }).getAttribute("href")).not.toContain(
    "round=",
  );
});

it("follows Back and Forward between opened rounds", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const list = await roundList();
  await screen.findByText("Round 25967 results");
  fireEvent.click(within(list).getByRole("link", { name: "25965" }));
  expect(await screen.findByText("Round 25965 results")).toBeInTheDocument();
  history.replaceState(null, "", "/generators");
  window.dispatchEvent(new PopStateEvent("popstate"));
  expect(await screen.findByText("Round 25967 results")).toBeInTheDocument();
});

it("ignores a malformed round in the URL and links only scored rounds", async () => {
  history.replaceState(null, "", "/generators?round=../1");
  serve();
  render(() => <GeneratorsPage />);
  expect(await screen.findByText("Round 25967 results")).toBeInTheDocument();
  expect(vi.mocked(getJSON).mock.calls.some(([path]) => path.includes(".."))).toBe(false);
  const list = await roundList();
  // Round 25968 is only committed, so it has no results to open.
  expect(within(list).queryByRole("link", { name: "25968" })).toBeNull();
  expect(within(list).getByText("25968")).toBeInTheDocument();
});

it("shows an error with a retry when an opened round cannot load", async () => {
  history.replaceState(null, "", "/generators?round=25964");
  serve({ "/generator/rounds/25964": new Error("offline") });
  render(() => <GeneratorsPage />);
  expect(await screen.findByText(/Round 25964 results/)).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
});

it("marks the reader's rows and says where their generator stands", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const board = await screen.findByRole("table", { name: "Projected generator payouts" });
  const results = await screen.findByRole("table", { name: "Generators in this round" });
  await within(results).findAllByRole("row");
  fireEvent.input(screen.getByLabelText(/Your hotkey/), { target: { value: " " + SECOND_HOTKEY } });

  const marked = within(board)
    .getAllByRole("row")
    .filter((row) => row.classList.contains("mine"));
  expect(marked).toHaveLength(1);
  expect(within(marked[0] as HTMLElement).getByText("You")).toBeInTheDocument();
  expect(marked[0]).toHaveTextContent("#2");
  expect(screen.getByText(/^Position #2 · score .* of the generator track$/)).toBeInTheDocument();
  expect(
    within(results)
      .getAllByRole("row")
      .filter((row) => row.classList.contains("mine")),
  ).toHaveLength(1);
  expect(localStorage.getItem("ditto:generators-hotkey")).toBe(" " + SECOND_HOTKEY);
});

it("says so when the hotkey has no generator on the board", async () => {
  serve();
  render(() => <GeneratorsPage />);
  const board = await screen.findByRole("table", { name: "Projected generator payouts" });
  fireEvent.input(screen.getByLabelText(/Your hotkey/), { target: { value: "5Fnot" } });
  expect(
    screen.getByText("No generator from this hotkey has a scored round in the window."),
  ).toBeInTheDocument();
  expect(board.querySelector("tr.mine")).toBeNull();
});

it("tags the reader's agent on the round's panel", async () => {
  const detail = roundDetail(25967);
  const standing = detail.standings[0];
  if (standing) Object.assign(standing, { agent_name: "dan0l", miner_hotkey: TOP_HOTKEY });
  serve({ "/generator/rounds/25967": detail });
  localStorage.setItem("ditto:generators-hotkey", TOP_HOTKEY);
  render(() => <GeneratorsPage />);
  const panel = await screen.findByRole("table", { name: "Panel agents on this round's cases" });
  const row = within(panel).getByText("dan0l").closest("tr") as HTMLElement;
  expect(row).toHaveClass("mine");
  expect(within(row).getByText("You")).toBeInTheDocument();
});

it("uses the signed-in miner's hotkey until the reader types another", async () => {
  setMinerSession({
    token: "t",
    hotkey: TOP_HOTKEY,
    scopes: [],
    expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
  });
  serve();
  render(() => <GeneratorsPage />);
  const board = await screen.findByRole("table", { name: "Projected generator payouts" });
  const input = screen.getByLabelText(/Your hotkey/) as HTMLInputElement;
  expect(input.value).toBe(TOP_HOTKEY);
  expect(screen.getByText("from sign-in")).toBeInTheDocument();
  await waitFor(() => expect(board.querySelector("tr.mine")).toHaveTextContent("#1"));

  // Clearing the field stays clear rather than falling back to the sign-in.
  fireEvent.input(input, { target: { value: "" } });
  expect(board.querySelector("tr.mine")).toBeNull();
  expect(input.value).toBe("");
  expect(screen.queryByText("from sign-in")).toBeNull();
});
