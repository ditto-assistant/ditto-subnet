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

/** "0.1 → 0.04 TAO" for a change, "0.04 TAO" for the first recorded fee. */
export function feeChangeText(previousRao: number | null, rao: number): string {
  return previousRao === null
    ? `${raoToTao(rao)} TAO`
    : `${raoToTao(previousRao)} → ${raoToTao(rao)} TAO`;
}

/** "up 25%" / "down 7%" relative to the previous fee; "" for the first. */
export function feeDirection(previousRao: number | null, rao: number): string {
  if (previousRao === null || previousRao <= 0 || previousRao === rao) return "";
  const change = Math.round(((rao - previousRao) / previousRao) * 100);
  if (change === 0) return rao > previousRao ? "up <1%" : "down <1%";
  return change > 0 ? `up ${change}%` : `down ${Math.abs(change)}%`;
}

export function feeDate(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not recorded";
  return date.toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** "24 hours", "1 hour", "90 minutes"; whole units only, never a rounded "0 hours". */
export function quoteLifetimeText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "the quote lifetime";
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}

/** Meta line for the current fee's revision. */
export function feeRevisionText(revision: number | null): string {
  if (revision === 0) return "Built-in default (no operator revision yet)";
  if (revision === null) return "Fixed TAO · revision not in the scanned history";
  return `Fixed TAO · revision ${revision}`;
}

/** Direction of one change; "initial" only for the true first fee. */
export function changeLabel(previousRao: number | null, rao: number, isGenesis: boolean): string {
  if (previousRao === null) return isGenesis ? "initial" : "previous fee not shown";
  return feeDirection(previousRao, rao) || "same amount";
}
