import { cleanup, fireEvent, render, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, expect, it, vi } from "vitest";

import { setMinerSession } from "../../stores/sessionStore";
import { EvaluationBudgets } from "./EvaluationBudgets";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("keeps Pay with TAO visible while evaluation billing is inactive", async () => {
  setMinerSession({
    token: "inactive-miner",
    hotkey: "own-hotkey",
    scopes: ["read"],
    expiresAt: "2099-01-01T00:00:00Z",
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ enabled: false, runs: [], payer_candidates: [] })),
  );
  render(() => <EvaluationBudgets />);
  expect(await screen.findByRole("link", { name: "Pay with TAO or alpha" })).toBeTruthy();
  expect(await screen.findByText(/Evaluation credit billing is not enabled/)).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Review and approve in Ditto" })).toBeNull();
});

it("prepares a candidate's whole comparison, then links to separate Ditto approval", async () => {
  setMinerSession({
    token: "isolated-miner",
    hotkey: "own-hotkey",
    scopes: ["read", "upload"],
    expiresAt: "2099-01-01T00:00:00Z",
  });
  let proposed = false;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url.endsWith("/me/evaluation-budget")) {
        proposed = true;
        return Response.json({});
      }
      return Response.json({
        enabled: true,
        payer_candidates: proposed
          ? []
          : [{ agent_id: "candidate", bundle_id: "whole-comparison" }],
        runs: proposed
          ? [
              {
                run_id: "fresh-run",
                agent_id: "candidate",
                phase: "confirmation",
                state: "proposed",
                estimate_microusd: 14_000_000,
                cap_microusd: 14_000_000,
                approval_url: "https://app.heyditto.ai/settings/usage?evaluation=fresh-run",
              },
            ]
          : [],
      });
    }),
  );
  render(() => <EvaluationBudgets />);
  fireEvent.click(await screen.findByRole("button", { name: "Prepare whole comparison budget" }));
  await waitFor(() => expect(screen.getByText(/Whole confirmation comparison/)).toBeTruthy());
  expect(screen.getByText(/Estimated \$14.00 · maximum \$14.00/)).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "Review and approve in Ditto" }).getAttribute("href"),
  ).toContain("evaluation=fresh-run");
  const writes = calls.filter((c) => c.init?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0]!.init?.body))).toEqual({
    agent_id: "candidate",
    bundle_id: "whole-comparison",
    phase: "confirmation",
  });
  expect(calls.every((c) => !c.url.includes("/approve") && !c.url.includes("/upload"))).toBe(true);
});
