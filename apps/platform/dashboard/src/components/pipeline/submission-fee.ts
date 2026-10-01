// Pure presentation helpers for the public submission fee. Money stays in
// integer rao; TAO text is derived with BigInt so no float ever rounds a fee.

const RAO_PER_TAO = 1_000_000_000n;

/** Exact TAO text for integer rao, trailing zeros trimmed ("0.04", "1"). */
export function raoToTao(rao: number): string {
  if (!Number.isSafeInteger(rao) || rao < 0) return "—";
  const value = BigInt(rao);
  const whole = value / RAO_PER_TAO;
  const fraction = (value % RAO_PER_TAO).toString().padStart(9, "0").replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : `${whole}`;
}

/** Only fixed_tao is a reviewed denomination; anything else is not shown as TAO. */
export function isFixedTao(denomination: string | null | undefined): boolean {
  return denomination === "fixed_tao";
}

/** "0.1 → 0.04 TAO" for a change. A bare "0.04 TAO" renders when Platform
 * publishes no previous fee: for the first published fee (revision 1, whose
 * parent is only the build's built-in default) or an unpublishable one. */
export function feeChangeText(previousRao: number | null, rao: number): string {
  return previousRao === null
    ? `${raoToTao(rao)} TAO`
    : `${raoToTao(previousRao)} → ${raoToTao(rao)} TAO`;
}

/** "up 25%" / "down 7%" relative to the previous fee; "" when unchanged.
 * The <1% boundary is decided exactly in integers before any rounding, so a
 * 0.99% change never reads as "1%"; a change of at least 1% rounds to the
 * nearest whole percent (never below 1%). */
export function feeDirection(previousRao: number | null, rao: number): string {
  if (previousRao === null || previousRao <= 0 || previousRao === rao) return "";
  if (!Number.isSafeInteger(previousRao) || !Number.isSafeInteger(rao)) return "";
  const previous = BigInt(previousRao);
  const delta = BigInt(rao) - previous;
  const magnitude = delta < 0n ? -delta : delta;
  const word = delta > 0n ? "up" : "down";
  if (magnitude * 100n < previous) return `${word} <1%`;
  // Round half up on exact integers: (magnitude * 200 + previous) / (2 * previous).
  const percent = (magnitude * 200n + previous) / (2n * previous);
  // A fee is never zero, so a decrease never reads as a full 100%.
  if (delta < 0n && percent >= 100n) return "down >99%";
  return `${word} ${percent}%`;
}

export function feeDate(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not recorded";
  return date.toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** "24 hours", "1 hour", "90 minutes", "59 seconds": the largest whole unit
 * that states the lifetime exactly, never rounded up past what is allowed. */
export function quoteLifetimeText(seconds: number): string {
  if (!Number.isSafeInteger(seconds) || seconds <= 0) return "the quote lifetime";
  const unit = (value: number, name: string): string => `${value} ${name}${value === 1 ? "" : "s"}`;
  if (seconds % 3600 === 0) return unit(seconds / 3600, "hour");
  if (seconds % 60 === 0) return unit(seconds / 60, "minute");
  return unit(seconds, "second");
}

/** Meta line for the current fee's revision. ``fee_revision`` 0 means the fee
 * was never changed by an operator; cooldown-only revisions can still exist
 * (``policy_revision`` > 0). */
export function feeRevisionText(revision: number | null, policyRevision: number): string {
  if (revision === 0) {
    return policyRevision === 0
      ? "Built-in default (no operator revision yet)"
      : "Built-in default fee (never changed by an operator)";
  }
  if (revision === null) return "Fixed TAO · revision not in the scanned history";
  return `Fixed TAO · revision ${revision}`;
}

/** Direction of one change. */
export function changeLabel(previousRao: number | null, rao: number, isGenesis: boolean): string {
  // Platform publishes no previous fee for revision 1 (its parent is only the
  // built-in default, never a charged fee) or for an unpublishable parent.
  // Only the oldest row of a complete history is the first published fee.
  if (previousRao === null) return isGenesis ? "first published fee" : "previous fee not shown";
  return feeDirection(previousRao, rao) || "same amount";
}
