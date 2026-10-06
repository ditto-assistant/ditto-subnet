import { cleanup, render, screen } from "@solidjs/testing-library";
import { afterEach, expect, it, vi } from "vitest";
import { GammaPage } from "./GammaPage";
import { getJSON } from "../lib/api";

vi.mock("../lib/api", () => ({ getJSON: vi.fn() }));
afterEach(cleanup);

it("shows a shadow allocation without claiming active payments or confirmed GM credits", async () => {
  vi.mocked(getJSON).mockResolvedValue({
    policy_revision: 7,
    allocation_version: 2,
    mode: "shadow",
    routing_status: "not_activated",
    service_bps: 1000,
    effective_service_share: 0,
    forecast_service_share: 0.1,
    forecast_burn_share: 0.9,
    forecast_miner_share: 0,
    collector_hotkey: "one-hotkey",
    collector_coldkey: "collector-coldkey",
    sweep_interval_hours: 24,
    buckets: [
      {
        bucket_id: "gm_credits",
        purpose: "GM inference credits",
        allocation_bps: 1000,
        holding_coldkey: "peyton-holding-wallet",
        publish_payments: true,
        payee_rules: [
          {
            rule_id: "gm",
            label: "GM credit payment",
            recipient_coldkey: "gm-treasury",
            asset: "TAO",
            enabled: true,
          },
        ],
      },
    ],
  });
  render(() => <GammaPage />);
  await screen.findByText("GM inference credits");
  expect(screen.getByText("Beta")).toBeInTheDocument();
  expect(screen.getByText(/Funding is not activated/)).toHaveTextContent(
    "effective service share is 0%",
  );
  expect(screen.getByText("peyton-holding-wallet")).toBeInTheDocument();
  expect(screen.getByText(/GM credits and their USD value are confirmed only/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /View wallet receipts/ })).toHaveAttribute(
    "href",
    "/activity",
  );
});

it("reports unavailable policy instead of rendering default funding", async () => {
  vi.mocked(getJSON).mockRejectedValue(new Error("offline"));
  render(() => <GammaPage />);
  await screen.findByRole("alert");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText(/effective service share/)).not.toBeInTheDocument();
});

it.each([
  ["enforce", "Gamma enforcement is armed for epoch 25549."],
  ["observe", "Gamma is observing the approved policy; enforcement is not armed."],
  ["pause", "Gamma enforcement is paused. Earlier chain actions may remain effective."],
  ["unavailable", "Gamma runtime control could not be verified."],
])("shows %s control separately from finalized funding", async (mode, message) => {
  vi.mocked(getJSON).mockResolvedValue({
    policy_revision: 2,
    allocation_version: 2,
    service_bps: 1000,
    effective_service_share: 0,
    forecast_service_share: 0.1,
    forecast_burn_share: 0.9,
    forecast_miner_share: 0,
    buckets: [],
    runtime: {
      revision: 2,
      mode,
      activation_epoch: mode === "enforce" ? 25549 : null,
      allocation_matches: mode === "unavailable" ? null : true,
    },
  });
  render(() => <GammaPage />);
  await screen.findByText(message);
  expect(screen.getByText(/Recorded control does not prove/)).toBeInTheDocument();
  expect(screen.queryByText(/Funding is not activated/)).not.toBeInTheDocument();
  expect(screen.queryByText(/effective service share is 0%/)).not.toBeInTheDocument();
});

it("flags an allocation that differs from the recorded runtime", async () => {
  vi.mocked(getJSON).mockResolvedValue({
    policy_revision: 3,
    allocation_version: 2,
    service_bps: 1000,
    forecast_service_share: 0.1,
    forecast_burn_share: 0.9,
    forecast_miner_share: 0,
    buckets: [],
    runtime: { revision: 2, mode: "enforce", activation_epoch: 25549, allocation_matches: false },
  });
  render(() => <GammaPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "This allocation differs from the approved runtime policy.",
  );
});
