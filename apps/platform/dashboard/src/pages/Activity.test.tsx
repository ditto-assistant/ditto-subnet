import { cleanup, fireEvent, render, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActivityPage } from "./ActivityPage";
import { getJSON } from "../lib/api";
vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
const request = vi.mocked(getJSON);
const item = {
  id: 12,
  recorded_at: "2026-09-18T12:30:00Z",
  action: "inference-concurrency-settings",
  method: "POST",
  status: "succeeded",
  http_status: 200,
  details: { settings: { chat_global_concurrency: 96 } },
  source: "request",
};
beforeEach(() => {
  history.replaceState(null, "", "/activity");
  request.mockReset();
});
afterEach(cleanup);
it("searches server-side, filters outcomes, and paginates", async () => {
  request.mockResolvedValue({ items: [item], next_before: 12 });
  render(() => <ActivityPage />);
  await screen.findByText("inference concurrency settings");
  expect(screen.getByText("96")).toBeInTheDocument();
  fireEvent.input(screen.getByLabelText("Search activity"), { target: { value: "canary" } });
  fireEvent.click(screen.getByRole("button", { name: /^Search$/ }));
  await waitFor(() =>
    expect(request).toHaveBeenLastCalledWith("/public/admin-activity?limit=50&q=canary"),
  );
  expect(location.search).toContain("activity_q=canary");
  fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "failed" } });
  await waitFor(() =>
    expect(request).toHaveBeenLastCalledWith(
      "/public/admin-activity?limit=50&q=canary&status=failed",
    ),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "Older" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Older" }));
  await waitFor(() =>
    expect(request).toHaveBeenLastCalledWith(
      "/public/admin-activity?limit=50&q=canary&status=failed&before=12",
    ),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "Newer" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Newer" }));
  await waitFor(() =>
    expect(request).toHaveBeenLastCalledWith(
      "/public/admin-activity?limit=50&q=canary&status=failed",
    ),
  );
});
it("shows empty results without implying the history is complete", async () => {
  request.mockResolvedValue({ items: [], next_before: null });
  render(() => <ActivityPage />);
  await screen.findByText("No activity found");
  expect(screen.getByRole("button", { name: "Older" })).toBeDisabled();
});
it("recovers from an unavailable API", async () => {
  request
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue({ items: [item], next_before: null });
  render(() => <ActivityPage />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await screen.findByText("inference concurrency settings");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
