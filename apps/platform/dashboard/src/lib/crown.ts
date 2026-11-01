// King and challengers copy (protocol 34 crown rules). Every number comes
// from `emissions.king`, `emissions.challengers`, and `emissions.crown_rules`;
// nothing here re-derives the fold.
import type { CrownChallenger, CrownRules, ForkStatus } from "../types/leaderboard";

/** "+0.012" / "−0.013" at three decimals, with a true minus sign. */
export function signedLead(value: number): string {
  const text = Math.abs(value).toFixed(3);
  if (Number(text) === 0) return "±0.000";
  return (value > 0 ? "+" : "−") + text;
}

/** "2 of 3": confirmations counted toward the duel, capped at the requirement
 * so a long-confirmed challenger reads "3 of 3", not "15 of 3". */
export function confirmationProgress(challenger: CrownChallenger): {
  counted: number;
  required: number;
  total: number;
  fraction: number;
} {
  const required = Math.max(1, challenger.required_confirmations);
  const total = Math.max(0, challenger.shared_seed_confirmations);
  const counted = Math.min(total, required);
  return { counted, required, total, fraction: counted / required };
}

function plural(n: number, one: string, many: string): string {
  return n + " " + (n === 1 ? one : many);
}

/** The plain-language next step for one challenger. */
export function challengerNeeds(challenger: CrownChallenger, rules: CrownRules | null): string {
  switch (challenger.status) {
    case "needs_confirmations": {
      const missing = Math.max(
        1,
        challenger.required_confirmations - challenger.shared_seed_confirmations,
      );
      return (
        "needs " +
        plural(missing, "more shared-seed confirmation", "more shared-seed confirmations")
      );
    }
    case "dueling": {
      const needed = challenger.lead_needed;
      return needed != null && Number.isFinite(needed)
        ? "needs " + signedLead(Math.max(needed, 0.0005)) + " more paired lead"
        : "behind the King on shared seeds";
    }
    case "would_dethrone":
      return rules?.active === false
        ? "clears the King once the crown rules are enforced"
        : "clears the King at the next epoch pin";
    case "outside_standing":
      return "must reach the top " + (rules?.champion_standing_rank ?? 3) + " first";
    default:
      return "";
  }
}

/** The paired lead cell: "—" until the two share a paired comparison. */
export function pairedLeadText(challenger: CrownChallenger): string {
  return challenger.paired_lead != null && Number.isFinite(challenger.paired_lead)
    ? signedLead(challenger.paired_lead)
    : "—";
}

/** "fork of dan0l v29 — must beat base: 2/3 confirmations, lead −0.004".
 * Once it has beaten the base: "fork of dan0l v29 — beat its base". */
export function forkLine(fork: ForkStatus): string {
  const base =
    (fork.base_agent_name ?? fork.base_agent_id.slice(0, 8)) +
    (fork.base_agent_version != null ? " v" + fork.base_agent_version : "");
  if (fork.beats_base) return "fork of " + base + " — beat its base";
  const counted = Math.min(fork.shared_seed_confirmations, fork.required_confirmations);
  const lead =
    fork.paired_lead != null && Number.isFinite(fork.paired_lead)
      ? signedLead(fork.paired_lead)
      : "—";
  return (
    "fork of " +
    base +
    " — must beat base: " +
    counted +
    "/" +
    fork.required_confirmations +
    " confirmations, lead " +
    lead +
    (fork.enforcement === "enforce" ? " · unpaid until it does" : "")
  );
}
