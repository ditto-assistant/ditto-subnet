import { describe, expect, it } from "vitest";

import type { PublicNeuron } from "../types/chain";
import { isMiningUid } from "./MetagraphPage";

function neuron(overrides: Partial<PublicNeuron>): PublicNeuron {
  return {
    uid: 1,
    hotkey: "5Hotkey",
    coldkey: "5Coldkey",
    stake: 0,
    validator_permit: false,
    is_active: false,
    incentive: 0,
    dividends: 0,
    trust: 0,
    consensus: 0,
    emission: 0,
    last_update: 0,
    validator_trust: 0,
    axon: { ip: null, port: null, version: null },
    ...overrides,
  };
}

describe("Metagraph miner classification", () => {
  it("keeps a validator-permit UID when it receives miner incentive", () => {
    expect(isMiningUid(neuron({ uid: 16, validator_permit: true, incentive: 0.65 }))).toBe(true);
  });

  it("hides a validator-only UID with no miner incentive", () => {
    expect(isMiningUid(neuron({ validator_permit: true, incentive: 0 }))).toBe(false);
  });

  it("keeps ordinary miner registrations even before they receive incentive", () => {
    expect(isMiningUid(neuron({ validator_permit: false, incentive: 0 }))).toBe(true);
  });
});
