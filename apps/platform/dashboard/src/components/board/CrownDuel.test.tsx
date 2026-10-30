// The crown is explained from the fold's own King and challenger standings:
// one King, then each challenger's evidence ("N of 3"), paired lead, and the
// one thing it still needs. Nothing here may re-derive the fold.
import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, it } from "vitest";

import type { EmissionsFold } from "../../types/leaderboard";
import { CrownDuel } from "./CrownDuel";
import type { BoardEntry, LeaderboardStore } from "./leaderboard-data";

afterEach(() => cleanup());

const KING = "00000000-0000-0000-0000-000000000001";
const entries = [
  { agent_id: KING, agent_name: "dan0l", agent_version: 30, rank: 2 },
  {
    agent_id: "00000000-0000-0000-0000-000000000002",
    agent_name: "DrPresident",
    agent_version: 22,
    rank: 1,
  },
  {
    agent_id: "00000000-0000-0000-0000-000000000003",
    agent_name: "crown-v195",
    agent_version: 1,
    rank: 3,
  },
  {
    agent_id: "00000000-0000-0000-0000-000000000004",
    agent_name: "horizon",
    agent_version: 5,
    rank: 4,
  },
] as unknown as BoardEntry[];

function store(emissions: EmissionsFold): LeaderboardStore {
  return {
    emissions: () => emissions,
    entries: () => entries,
    unavailable: () => false,
  } as unknown as LeaderboardStore;
}

const challenger = (
  index: number,
  fields: Partial<NonNullable<EmissionsFold["challengers"]>[number]>,
): NonNullable<EmissionsFold["challengers"]>[number] => ({
  agent_id: String(entries[index]?.agent_id),
  miner_hotkey: "5H" + index,
  official_rank: Number(entries[index]?.rank),
  official_score: 0.93,
  shared_seed_confirmations: 0,
  required_confirmations: 3,
  within_standing: true,
  paired_lead: null,
  required_lead: null,
  lead_needed: null,
  status: "needs_confirmations",
  ...fields,
});

const emissions: EmissionsFold = {
  champion_agent_id: KING,
  crown_rules: {
    active: false,
    required_protocol: 34,
    min_dethrone_confirmations: 3,
    champion_standing_rank: 3,
  },
  king: {
    agent_id: KING,
    miner_hotkey: "5H0",
    official_rank: 2,
    official_score: 0.9347,
    shared_seed_confirmations: 15,
  },
  challengers: [
    challenger(1, {
      shared_seed_confirmations: 2,
      paired_lead: 0.021,
      status: "needs_confirmations",
    }),
    challenger(2, {
      shared_seed_confirmations: 7,
      paired_lead: -0.013,
      required_lead: 0.007,
      lead_needed: 0.02,
      status: "dueling",
    }),
    challenger(3, { official_rank: 4, within_standing: false, status: "outside_standing" }),
  ],
};

describe("CrownDuel", () => {
  it("names one King and says what each challenger needs to dethrone it", () => {
    render(() => <CrownDuel store={store(emissions)} />);
    const king = document.getElementById("crown-duel-king")?.textContent ?? "";
    expect(king).toContain("King");
    expect(king).toContain("dan0l");
    expect(king).toContain("0.935");
    expect(king).toContain("15 shared-seed confirmations");

    const rows = [...document.querySelectorAll(".crown-duel-row")].map((row) => row.textContent);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain("DrPresident");
    expect(rows[0]).toContain("2 of 3 confirmations");
    expect(rows[0]).toContain("needs 1 more shared-seed confirmation");
    expect(rows[1]).toContain("3 of 3 confirmations");
    expect(rows[1]).toContain("−0.013");
    expect(rows[1]).toContain("needs +0.020 more paired lead");
    expect(rows[2]).toContain("must reach the top 3 first");

    // Configured but not yet folded by the fleet: said once, quietly.
    expect(document.getElementById("crown-duel-rollout")?.textContent).toContain("protocol 34");
  });

  it("drops the King line on the overview mount and hides without crown rules", () => {
    render(() => <CrownDuel store={store(emissions)} showKing={false} />);
    expect(document.getElementById("crown-duel-king")).toBeNull();
    expect(document.getElementById("crown-duel")).not.toBeNull();
    cleanup();
    render(() => <CrownDuel store={store({ ...emissions, crown_rules: null })} />);
    expect(document.getElementById("crown-duel")).toBeNull();
  });
});
