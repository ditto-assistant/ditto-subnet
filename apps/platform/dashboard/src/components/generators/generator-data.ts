// Generator competition: public payload types and the pure math the page
// shows. Every number comes from Platform's public generator endpoints, the
// shared /public/weights epoch, or /public/treasury-allocation. The payout
// projection mirrors the validator fold (ditto/validator/weights.py
// compute_generator_weights) so the page never invents a share.

import type { ChainEpoch } from "../../types/leaderboard";

export type GeneratorMode = "off" | "shadow" | "enforce";

export interface GeneratorBoardEntry {
  miner_hotkey: string;
  submission_id: string;
  round_index: number | null;
  generator_contract_version: number;
  weight_eligible: boolean;
  score: number;
  discriminating_cases: number;
  first_seen: string;
  scored_at: string;
}

export interface GeneratorBoard {
  entries: GeneratorBoardEntry[];
  count: number;
  window_days: number;
  mode: GeneratorMode;
  generated_at: string;
}

export type GeneratorRoundStatus =
  | "committed"
  | "generating"
  | "running"
  | "scoring"
  | "scored"
  | "void";

export interface GeneratorRoundSummary {
  round_index: number;
  status: GeneratorRoundStatus;
  epochs_per_round: number;
  commitment: string;
  commitment_publication: string;
  commitment_confirmations: number;
  committed_at: string | null;
  open_epoch_index: number | null;
  open_block: number | null;
  open_block_hash: string | null;
  opened_at: string | null;
  column_count: number;
  panel_size: number;
  panel_dropped: number;
  results: number;
  answered_results: number;
  closed_at: string | null;
  scored_at: string | null;
  void_reason: string | null;
  revealed_at: string | null;
  secret_hex: string | null;
  seed: string | null;
  dataset_sha256: string | null;
}

export interface GeneratorRoundSchedule {
  mode: GeneratorMode;
  epochs_per_round: number;
  subnet_epoch_index?: number | null;
  current_round?: number | null;
  next_round_epoch?: number | null;
  tempo?: number | null;
  block?: number | null;
  observed_at?: string | null;
}

export interface GeneratorRoundList {
  schedule: GeneratorRoundSchedule;
  rounds: GeneratorRoundSummary[];
}

export interface GeneratorRoundColumn {
  column_id: string;
  submission_id: string;
  miner_hotkey: string | null;
  counted: boolean;
  public_default: boolean;
  generated: boolean;
  score: number;
  discriminating: number;
  trivial: number;
  unsolved: number;
  duplicate: number;
}

export interface GeneratorRoundStanding {
  agent_id: string;
  rank: number;
  pairwise_score: number;
  wins: number;
  ties: number;
  losses: number;
  opponents: number;
  common_mean: number;
  common_cases: number;
  reference: boolean;
}

export interface GeneratorRoundDetail {
  summary: GeneratorRoundSummary;
  columns: GeneratorRoundColumn[];
  standings: GeneratorRoundStanding[];
}

export interface GeneratorUploadTerms {
  accepting: boolean;
  payment_required: boolean;
  payment_amount_rao: number | null;
  payment_send_address: string | null;
  upload_cooldown_seconds: number;
  retry_at: string | null;
  blockers: string[];
}

/** One recipient's share of total miner emission (SN-350). */
export interface EmissionShare {
  kind: "burn" | "track" | "gamma";
  id: string | null;
  share: number;
  bps: number;
}

/** The slice of /public/treasury-allocation this page reads. The split
 * fields arrive with SN-350; older Platform builds omit them. */
export interface EmissionAllocation {
  forecast_burn_share: number;
  forecast_miner_share: number;
  emission_allocation_revision?: number;
  emission_split?: EmissionShare[];
  emission_split_epoch?: number | null;
  emission_split_source?: "epoch_pin" | "settings" | "unavailable";
}

/** The validator's frozen generator rank shares (ditto/validator/config.py). */
export const GENERATOR_RANK_SHARES: readonly number[] = [0.4, 0.25, 0.15, 0.12, 0.08];

export interface ProjectedPayout {
  entry: GeneratorBoardEntry;
  /** 1-based paid position, or null when the entry earns nothing. */
  position: number | null;
  /** Fraction of the generator track this entry folds to (0 when unpaid). */
  trackShare: number;
  /** Why an entry is unpaid, for the row label. */
  unpaidReason: "ineligible" | "zero_score" | "duplicate_hotkey" | "beyond_paid_ranks" | null;
}

function payable(entry: GeneratorBoardEntry): boolean {
  return entry.weight_eligible && Number.isFinite(entry.score) && entry.score > 0;
}

function compareEntries(a: GeneratorBoardEntry, b: GeneratorBoardEntry): number {
  if (a.score !== b.score) return b.score - a.score;
  if (a.first_seen !== b.first_seen) return a.first_seen < b.first_seen ? -1 : 1;
  return a.submission_id < b.submission_id ? -1 : a.submission_id > b.submission_id ? 1 : 0;
}

/** Rank the board the way validators fold it: eligible positive scores by
 * (-score, first_seen, submission_id), one entry per hotkey, rank shares by
 * position, then normalised to the track (blend_track_weights). Unpaid rows
 * follow in score order with the reason they earn nothing. */
export function projectGeneratorPayouts(
  entries: readonly GeneratorBoardEntry[],
  rankShares: readonly number[] = GENERATOR_RANK_SHARES,
): ProjectedPayout[] {
  const ranked = [...entries].sort(compareEntries);
  const seen = new Set<string>();
  const paid: ProjectedPayout[] = [];
  const unpaid: ProjectedPayout[] = [];
  for (const entry of ranked) {
    if (!entry.weight_eligible) {
      unpaid.push({ entry, position: null, trackShare: 0, unpaidReason: "ineligible" });
    } else if (!payable(entry)) {
      unpaid.push({ entry, position: null, trackShare: 0, unpaidReason: "zero_score" });
    } else if (seen.has(entry.miner_hotkey)) {
      unpaid.push({ entry, position: null, trackShare: 0, unpaidReason: "duplicate_hotkey" });
    } else if (paid.length >= rankShares.length) {
      seen.add(entry.miner_hotkey);
      unpaid.push({ entry, position: null, trackShare: 0, unpaidReason: "beyond_paid_ranks" });
    } else {
      seen.add(entry.miner_hotkey);
      paid.push({ entry, position: paid.length + 1, trackShare: 0, unpaidReason: null });
    }
  }
  const total = paid.reduce((sum, row) => sum + (rankShares[(row.position ?? 1) - 1] ?? 0), 0);
  for (const row of paid) {
    row.trackShare = total > 0 ? (rankShares[(row.position ?? 1) - 1] ?? 0) / total : 0;
  }
  return [...paid, ...unpaid];
}

/** Estimated start of the next round: the next epoch boundary, plus one
 * epoch per whole epoch still between it and the round's opening epoch.
 * Null when the schedule or the chain clock is missing, or when the schedule
 * is stale (its opening epoch has already been reached). */
export function nextRoundEta(
  schedule: GeneratorRoundSchedule | null | undefined,
  epoch: ChainEpoch | null | undefined,
): Date | null {
  const nextRound = schedule?.next_round_epoch;
  const current = schedule?.subnet_epoch_index;
  const nextAt = epoch?.next_epoch_at ? new Date(epoch.next_epoch_at).getTime() : NaN;
  const seconds = epoch?.epoch_seconds;
  if (nextRound == null || current == null || !Number.isFinite(nextAt)) return null;
  if (nextRound <= current) return null;
  const extra = nextRound - current - 1;
  if (extra > 0 && !(typeof seconds === "number" && seconds > 0)) return null;
  return new Date(nextAt + extra * (seconds ?? 0) * 1000);
}

/** Cases one column produced: every case lands in exactly one bucket. */
export function columnCases(column: GeneratorRoundColumn): number {
  return column.discriminating + column.trivial + column.unsolved + column.duplicate;
}

/** The newest round with scored results (the one whose detail to show). */
export function latestScoredRound(
  list: GeneratorRoundList | null | undefined,
): GeneratorRoundSummary | null {
  let newest: GeneratorRoundSummary | null = null;
  for (const round of list?.rounds ?? []) {
    if (round.status === "scored" && (!newest || round.round_index > newest.round_index)) {
      newest = round;
    }
  }
  return newest;
}

/** The round index that opens next. ``next_round_epoch`` is the round's first
 * subnet epoch (round_index * epochs_per_round), not its index. */
export function nextRoundIndex(schedule: GeneratorRoundSchedule | null | undefined): number | null {
  if (!schedule) return null;
  if (schedule.current_round != null) return schedule.current_round + 1;
  const epoch = schedule.next_round_epoch;
  const per = schedule.epochs_per_round;
  if (epoch == null || !(per > 0)) return null;
  return Math.floor(epoch / per);
}

/** The share of total miner emission the generator track carries, when
 * Platform publishes the split; null before SN-350 lands. */
export function generatorEmissionShare(
  allocation: EmissionAllocation | null | undefined,
): number | null {
  const split = allocation?.emission_split;
  if (!split || split.length === 0) return null;
  const generator = split.find((row) => row.kind === "track" && row.id === "generator");
  return generator ? generator.share : 0;
}

/** Exact TAO for an integer rao amount, trailing zeros stripped. */
export function taoAmount(rao: number | null | undefined): string {
  if (rao == null || !Number.isSafeInteger(rao) || rao < 0) return "–";
  const whole = Math.floor(rao / 1e9);
  const frac = String(rao % 1e9)
    .padStart(9, "0")
    .replace(/0+$/, "");
  return (frac ? whole + "." + frac : String(whole)) + " TAO";
}

/** "1h", "45m", "90s" for a whole-second cooldown. */
export function cooldownText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "none";
  if (seconds % 3600 === 0) return seconds / 3600 + "h";
  if (seconds % 60 === 0) return seconds / 60 + "m";
  return seconds + "s";
}
