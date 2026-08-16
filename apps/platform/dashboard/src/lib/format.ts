// Pure formatting helpers. No DOM, no state — everything here is a direct
// port of the original formatters, exact output formats included.

import type { TimelineRelease } from "../types";

/** Fraction as a percent with one decimal: 0.123 → "12.3%". */
export function pct(x: number): string {
  return (x * 100).toFixed(1) + "%";
}

/** Fixed three decimals: 0.4917 → "0.492". */
export function fx(x: number): string {
  return x.toFixed(3);
}

/** Six decimals, for a composite that is being compared against another one.
 *
 * Three decimals is the right precision for a subscore — `tool_mean` moves in
 * steps of 0.01 and `memory_mean` in steps of 1/251 ≈ 0.004, so both are fully
 * resolved there. The composite is not: a single Bench-v9 run moves it in steps
 * of 0.000996 (half a LongMem case out of 251, at weight 0.5), and a continual
 * mean over n runs resolves 0.000996/n. Rendering that at three decimals prints
 * the whole top of a saturated board as one repeated `0.997`, which is exactly
 * where a reader most needs to tell two agents apart. Six decimals covers the
 * finest increment the evidence can produce and matches the precision miners
 * already quote scores in.
 */
export function fxScore(x: number): string {
  return x.toFixed(6);
}

// Trailing zeros on a consensus parameter read as false precision.
/** ≤4 decimals with trailing zeros stripped: 0.5000 → "0.5". */
export function num(x: number): string {
  return String(Number(x.toFixed(4)));
}

/** Human copy for the dethrone margin; a non-finite margin reads "incumbent". */
export function marginText(margin: number | null | undefined): string {
  if (!Number.isFinite(margin)) return "incumbent";
  return num(margin as number) + " composite points";
}

/** Clamp to [0, 1]. */
export function clamp01(v: number): number {
  return Math.max(0, Math.min(1, v));
}

/** "N ms" below 1s, seconds with 1 decimal below 10s, 0 decimals at/above 10s. */
export function fmtMs(ms: number): string {
  return ms >= 1000 ? (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) + " s" : ms + " ms";
}

/** Median of a numeric array; 0 for empty input, never mutates the input. */
export function median(nums: number[]): number {
  if (!nums.length) return 0;
  const s = nums.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? (s[m] as number) : ((s[m - 1] as number) + (s[m] as number)) / 2;
}

/** HTML-escape the five specials (&<>"'). Solid escapes text on its own;
 * this exists for the few places raw strings become markup/attributes. */
export function esc(s: unknown): string {
  return String(s).replace(/[&<>"']/g, (c) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string;
  });
}

/** Relative time from an ISO string: "5m ago". Invalid dates render "–" (en
 * dash); future timestamps clamp to "0s ago". */
export function relTime(iso: string | null | undefined): string {
  const t = iso == null ? NaN : new Date(iso).getTime();
  if (isNaN(t)) return "–";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

/** Future-facing twin of relTime for deadlines (embargo unlock times):
 * "in 5m" (units rounded UP); an elapsed deadline reads "any moment now";
 * invalid dates render "–". */
export function relTimeUntil(iso: string | null | undefined): string {
  const t = iso == null ? NaN : new Date(iso).getTime();
  if (isNaN(t)) return "–";
  const s = (t - Date.now()) / 1000;
  if (s <= 0) return "any moment now";
  if (s < 60) return "in " + Math.ceil(s) + "s";
  if (s < 3600) return "in " + Math.ceil(s / 60) + "m";
  if (s < 86400) return "in " + Math.ceil(s / 3600) + "h";
  return "in " + Math.ceil(s / 86400) + "d";
}

/** Elapsed-seconds twin of relTime, for API-reported ages (staleness
 * markers) rather than timestamps. Takes seconds because that is what the
 * API sends; invalid/negative render "–". */
export function relDuration(seconds: unknown): string {
  const s = Number(seconds);
  if (!isFinite(s) || s < 0) return "–";
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

/** "Name, Submission vN" — the agent's full accessible label. */
export function agentLabel(
  name: string | null | undefined,
  version: number | null | undefined,
): string {
  const label = name || "Unnamed agent";
  return label + ", " + agentVersionLabel(version);
}

/** Display name with the "Unnamed agent" fallback. */
export function agentName(name: string | null | undefined): string {
  return name || "Unnamed agent";
}

/** Public stand-in after an upheld handle claim strikes a colliding name. */
export const STRICKEN_PUBLIC_NAME = "Unnamed submission";

/** Public identity for a stored name plus its optional handle annotation. */
export function publicDisplayName(
  stored: string | null | undefined,
  handle?: { status?: string | null } | null,
): string {
  if (handle?.status === "disputed") return STRICKEN_PUBLIC_NAME;
  return agentName(stored);
}

/** "Submission vN"; a missing version reads "Legacy submission". */
export function agentVersionLabel(version: number | null | undefined): string {
  return version == null ? "Legacy submission" : "Submission v" + version;
}

/** Label of the submission this entry duplicates: the duplicate's own agent
 * label when named, else "Submission " + first 8 chars of its id; "" when
 * the entry is not a duplicate. */
export function duplicateLabel(
  entry:
    | {
        duplicate_of?: string | null;
        duplicate_name?: string | null;
        duplicate_version?: number | null;
      }
    | null
    | undefined,
): string {
  if (!entry || !entry.duplicate_of) return "";
  if (entry.duplicate_name) return agentLabel(entry.duplicate_name, entry.duplicate_version);
  return "Submission " + String(entry.duplicate_of).slice(0, 8);
}

/** Abbreviate an ss58 hotkey for a compact row (full value stays in the
 * title): first 8 chars + "…" + last 6 when longer than 16. Null-safe. */
export function shortKey(k: string | null | undefined): string {
  return k && k.length > 16 ? k.slice(0, 8) + "…" + k.slice(-6) : k || "";
}

/** The elided form of a long opaque value — a digest, a git revision — for
 * the mono/copy treatment (monoValue 8908–8914): head 12 + "…" + tail 10 once
 * past 26 characters, so a sha256 stays recognizable at both ends while the
 * full value lives in the title and on the copy control. */
export function monoDisplay(value: string): string {
  return value.length > 26 ? value.slice(0, 12) + "…" + value.slice(-10) : value;
}

/** Strip the provider prefix from a model name (last "/" segment). */
export function shortModel(name: string): string {
  return name.indexOf("/") >= 0 ? (name.split("/").pop() ?? name) : name;
}

/** Coerce to a non-negative integer count; invalid/negative → 0. */
export function telemetryCount(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.floor(number) : 0;
}

/** Format an ms duration: invalid/negative → "—"; <1s → "N ms"; <60s →
 * seconds (1 decimal below 10s); else "<m>m <s>s". */
export function telemetryDuration(value: unknown): string {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "—";
  if (milliseconds < 1000) return Math.round(milliseconds) + " ms";
  if (milliseconds < 60000)
    return (milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0) + " s";
  return Math.floor(milliseconds / 60000) + "m " + Math.round((milliseconds % 60000) / 1000) + "s";
}

/** Elapsed time since an ISO timestamp as "<h>h <m>m <s>s" (leading units
 * omitted when zero); "" for an invalid/missing start. */
export function elapsedDuration(startedAt: string | null | undefined): string {
  const started = Date.parse(startedAt || "");
  if (!Number.isFinite(started)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return (hours ? hours + "h " : "") + (hours || minutes ? minutes + "m " : "") + remainder + "s";
}

// Shared UTC date formatter for the memory timeline, so a release marker and
// a point placed on it never disagree across viewer timezones.
const TIMELINE_DATE = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

/** "Mon D" in UTC via the shared timeline formatter. */
export function timelineDate(value: string | number): string {
  return TIMELINE_DATE.format(new Date(value));
}

/** Epoch ms of a timeline release's released_at (NaN when unparsable). */
export function releaseTime(release: TimelineRelease): number {
  return Date.parse(release.released_at ?? "");
}

/** Locale date-time (medium/short); "Not recorded" for missing/unparsable. */
export function athDate(value: string | number | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not recorded";
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
