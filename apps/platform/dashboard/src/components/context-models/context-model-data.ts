// Wire shapes and pure helpers for the Context models page (/context-models):
// the model_context (CPM) competition, contract
// docs/research/cpm/competition-v1.md. These mirror the public responses in
// apps/platform/ditto/api_models/model_context.py; rank, score and
// eligibility are Platform's, never recomputed here.

export type ContextModelMode = "off" | "shadow" | "enforce";

export interface ContextModelBase {
  base_id: string;
  hf_repo: string;
  revision: string;
  parameters: number;
  architectures: string[];
  lora_target_modules: string[];
}

export interface ContextModelBoardEntry {
  rank: number;
  hotkey: string;
  submission_id: string;
  score: number;
  eligible: boolean;
  commitment_block: number;
  season: number;
  base_id: string;
  kind: "lora" | "full";
  hf_repo: string;
  revision: string;
  model_url: string;
  components: Record<string, number | null>;
  floor: number | null;
}

export interface ContextModelBoard {
  entries: ContextModelBoardEntry[];
  count: number;
  season: number | null;
  mode: ContextModelMode;
  floor_margin: number;
  base_floors: Record<string, number>;
  generated_at: string;
}

export interface ContextModelSeason {
  season: number;
  open_block: number;
  open_block_hash: string;
  /** uint64 seeds; a JSON number past 2**53 arrives rounded, so display
   * `seed_strings`. */
  seeds: number[];
  seed_strings?: string[];
  bench_version: number;
  cpm_score_version: number;
  base_floors: Record<string, number>;
  gpu_sku: string | null;
  opened_at: string;
  closes_at: string;
  closed_at: string | null;
}

export interface ContextModelSeasonList {
  current: number | null;
  seasons: ContextModelSeason[];
}

export interface ContextModelUploadTerms {
  accepting: boolean;
  competition: "model_context";
  mode: ContextModelMode;
  payment_required: boolean;
  upload_cooldown_seconds: number;
  retry_at: string | null;
  blockers: string[];
  signature_payload: string;
  commitment_format: string;
  manifest_schema: string;
  manifest_filename: string;
  licenses: string[];
  bases: ContextModelBase[];
  max_lora_rank: number;
  max_total_bytes: number;
  cpm_score_version: number;
}

/** The miner's Hugging Face repo at the exact revision Platform pinned. Both
 * parts are Platform-validated; each path segment is still encoded. */
export function hfRevisionUrl(repo: string, revision: string): string {
  const path = repo
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return "https://huggingface.co/" + path + "/tree/" + encodeURIComponent(revision);
}

/** A season's seeds as exact decimal strings. An older Platform without
 * `seed_strings` yields only the seeds a JavaScript number holds exactly;
 * `null` means some seed cannot be shown without rounding. */
export function exactSeeds(season: ContextModelSeason): string[] | null {
  if (season.seed_strings && season.seed_strings.length === season.seeds.length) {
    return season.seed_strings;
  }
  return season.seeds.every((seed) => Number.isSafeInteger(seed))
    ? season.seeds.map((seed) => String(seed))
    : null;
}

/** The eligibility bar for a base this season: its floor plus the margin. */
export function eligibilityBar(floor: number | null | undefined, margin: number): number | null {
  return floor == null ? null : floor + margin;
}

/** Bytes as GiB/MiB for the upload caps. */
export function sizeText(bytes: number): string {
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) return (Number.isInteger(gib) ? gib : gib.toFixed(1)) + " GiB";
  return Math.round(bytes / 1024 ** 2) + " MiB";
}

/** A parameter count as 0.87B / 2.27B. */
export function paramsText(parameters: number): string {
  return (parameters / 1e9).toFixed(2) + "B";
}

/** The three score components the contract defines, in display order. */
export const SCORE_COMPONENTS = [
  ["memory_recall", "Memory recall"],
  ["distractor_rate", "Distractor rate"],
  ["tool_recall", "Tool recall"],
] as const;
