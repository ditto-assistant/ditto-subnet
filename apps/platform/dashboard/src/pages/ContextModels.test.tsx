import { cleanup, render, screen, within } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getJSON } from "../lib/api";
import { loadFixture } from "../test-fixtures";
import type { ContextModelSeasonList } from "../components/context-models/context-model-data";
import {
  exactSeeds,
  hfRevisionUrl,
  sizeText,
} from "../components/context-models/context-model-data";
import { ContextModelsPage } from "./ContextModelsPage";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);

const SHADOW: Record<string, unknown> = {
  "/model-context/board": loadFixture("model-context-board"),
  "/model-context/seasons": loadFixture("model-context-seasons"),
  "/model-context/upload-terms": loadFixture("model-context-upload-terms"),
};
const OFF: Record<string, unknown> = {
  "/model-context/board": loadFixture("model-context-board-off"),
  "/model-context/seasons": loadFixture("model-context-seasons-off"),
  "/model-context/upload-terms": loadFixture("model-context-upload-terms-off"),
};

function serve(routes: Record<string, unknown>): void {
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

describe("Context models page", () => {
  it("shows the shadow badge, season seeds and floors, and the ranked board", async () => {
    serve(SHADOW);
    render(() => <ContextModelsPage />);
    const board = await screen.findByRole("table", { name: "Context model board" });
    expect(screen.getByText("Shadow · unpaid")).toBeInTheDocument();
    expect(await screen.findByText("Season 1")).toBeInTheDocument();
    expect(screen.getByText("#5,000,000")).toBeInTheDocument();
    expect(screen.getByText("v18")).toBeInTheDocument();
    expect(screen.getByText("L40S")).toBeInTheDocument();
    // uint64 seeds, exact: the string form, never a rounded double.
    expect(screen.getByText("9879830438737723427")).toBeInTheDocument();
    expect(screen.getByText("9185052217827815145")).toBeInTheDocument();

    const floors = screen.getByRole("table", { name: "Base floors this season" });
    const floorRow = within(floors).getByText("qwen3.5-0.8b").closest("tr");
    expect(floorRow).toHaveTextContent("0.311");
    expect(floorRow).toHaveTextContent("0.321");

    const rows = within(board).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const [first, second] = rows as [HTMLElement, HTMLElement];
    expect(first).toHaveTextContent("#1");
    expect(first).toHaveTextContent("5FHneW46");
    expect(first).toHaveTextContent("0.451");
    expect(first).toHaveTextContent("Eligible");
    expect(first).toHaveTextContent("5,012,340");
    expect(first).toHaveTextContent("LoRA on qwen3.5-0.8b");
    expect(first).toHaveTextContent("Memory 0.610");
    expect(second).toHaveTextContent("Below floor");
    expect(second).toHaveClass("unpaid");

    const link = within(first).getByRole("link", { name: "miner/cpm" });
    expect(link).toHaveAttribute(
      "href",
      "https://huggingface.co/miner/cpm/tree/301af1331a4fcde3cf5480add68210da2dbf8346",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(within(first).queryByRole("link", { name: "mirror" })).not.toBeInTheDocument();
    expect(screen.getByText(/Validators pay this track nothing yet/)).toBeInTheDocument();

    // Upload terms: open, with the allowlisted bases at their pinned revisions.
    expect(screen.getByText("Uploads").nextElementSibling).toHaveTextContent("Open");
    expect(screen.getByText("Max size").nextElementSibling).toHaveTextContent("6 GiB");
    expect(screen.getByText("Cooldown per hotkey").nextElementSibling).toHaveTextContent("1m");
    const bases = screen.getByRole("table", { name: "Allowlisted bases" });
    expect(within(bases).getByRole("link", { name: "Qwen/Qwen3.5-2B" })).toHaveAttribute(
      "href",
      "https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc",
    );
    expect(bases).toHaveTextContent("2.27B");
  });

  it("says the lane is off on purpose and still shows the upload terms", async () => {
    serve(OFF);
    render(() => <ContextModelsPage />);
    expect(await screen.findByRole("heading", { name: "Not open yet" })).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Context model board" })).not.toBeInTheDocument();
    expect(screen.queryByText("Shadow · unpaid")).not.toBeInTheDocument();
    expect(await screen.findByText("Closed")).toBeInTheDocument();
    expect(
      screen.getByText("the model_context competition is not accepting submissions"),
    ).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Allowlisted bases" })).toBeInTheDocument();
  });

  it("explains an open lane with no season yet", async () => {
    const board = {
      ...(SHADOW["/model-context/board"] as object),
      entries: [],
      count: 0,
      season: null,
    };
    serve({
      ...SHADOW,
      "/model-context/board": board,
      "/model-context/seasons": OFF["/model-context/seasons"],
    });
    render(() => <ContextModelsPage />);
    expect(await screen.findByText(/No season is open yet/)).toBeInTheDocument();
    expect(
      await screen.findByText("No season is open, so nothing is ranked yet."),
    ).toBeInTheDocument();
  });

  it("keeps sections independent: a failed board read offers a retry", async () => {
    serve({ ...SHADOW, "/model-context/board": new Error("HTTP 503") });
    render(() => <ContextModelsPage />);
    expect(await screen.findByText("Board could not be loaded.")).toBeInTheDocument();
    expect(await screen.findByText("Season 1")).toBeInTheDocument();
  });

  it("links a mirror when Ditto serves one", async () => {
    const board = structuredClone(SHADOW["/model-context/board"]) as {
      entries: [{ model_url: string }, ...{ model_url: string }[]];
    };
    board.entries[0].model_url = "https://huggingface.co/ditto-mirror/cpm-1";
    serve({ ...SHADOW, "/model-context/board": board });
    render(() => <ContextModelsPage />);
    const table = await screen.findByRole("table", { name: "Context model board" });
    expect(within(table).getByRole("link", { name: "mirror" })).toHaveAttribute(
      "href",
      "https://huggingface.co/ditto-mirror/cpm-1",
    );
  });
});

describe("context model data helpers", () => {
  it("never shows a seed a browser rounded", () => {
    const list = loadFixture<ContextModelSeasonList>("model-context-seasons");
    const season = list.seasons[0];
    if (!season) throw new Error("the seasons fixture has no season");
    expect(exactSeeds(season)).toEqual(season.seed_strings);
    const { seed_strings: _absent, ...older } = season;
    expect(exactSeeds(older)).toBeNull();
    expect(exactSeeds({ ...older, seeds: [1, 2, 3] })).toEqual(["1", "2", "3"]);
  });

  it("encodes repo path segments and sizes caps", () => {
    expect(hfRevisionUrl("a b/c", "f".repeat(40))).toBe(
      "https://huggingface.co/a%20b/c/tree/" + "f".repeat(40),
    );
    expect(sizeText(6 * 1024 ** 3)).toBe("6 GiB");
    expect(sizeText(512 * 1024 ** 2)).toBe("512 MiB");
  });
});
