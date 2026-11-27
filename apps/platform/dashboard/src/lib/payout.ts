import type { EmissionRecipient } from "../types/leaderboard";

/** The fraction of a recipient's slot validators actually pay, when the
 * clean-board burn (#2911) cuts it: an unattested slot past its grace window
 * under `clean_board_mode: "enforce"`. Undefined when the slot is paid in
 * full. A shadow fraction is published but never applied, so it never
 * reaches the payout. A provisional incumbent (`paid: false`) is handled by
 * its own "unpaid" copy instead. */
export function cleanBoardPaidFraction(
  recipient: EmissionRecipient | null | undefined,
  mode: string | null | undefined,
): number | undefined {
  if (!recipient || mode !== "enforce" || recipient.paid === false) return undefined;
  const fraction = recipient.paid_fraction;
  return typeof fraction === "number" && Number.isFinite(fraction) && fraction >= 0 && fraction < 1
    ? fraction
    : undefined;
}

/** The share of the miner pool validators actually pay this recipient. */
export function paidShare(
  recipient: EmissionRecipient | null | undefined,
  mode: string | null | undefined,
): number | undefined {
  const share = recipient?.share_of_miner_pool;
  if (typeof share !== "number" || !Number.isFinite(share)) return undefined;
  const fraction = cleanBoardPaidFraction(recipient, mode);
  return fraction === undefined ? share : share * fraction;
}

/** The paid shares of a group of recipients (a score-ceiling joint crown) as
 * `[lowest, highest]`. Under enforcement an unattested member is paid less
 * than an attested one, so a single "each receives" figure is only true when
 * the two match. Undefined when no member has a finite share. */
export function paidShareRange(
  recipients: readonly EmissionRecipient[],
  mode: string | null | undefined,
): [number, number] | undefined {
  const shares = recipients
    .map((recipient) => paidShare(recipient, mode))
    .filter((share): share is number => share !== undefined);
  if (shares.length === 0) return undefined;
  return [Math.min(...shares), Math.max(...shares)];
}

/** `"12.5%"`, or `"10.0%–12.5%"` when the group's paid shares differ. */
export function shareRangeText(range: [number, number], pct: (value: number) => string): string {
  const [low, high] = range;
  return low === high ? pct(low) : pct(low) + "–" + pct(high);
}
