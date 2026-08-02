// Benchmark documentation wire shapes (/public/bench/glossary,
// /public/bench/config, /public/bench/timeline).

// ── Benchmark glossary (/public/bench/glossary) ──────────────

export interface GlossaryCategory {
  key?: string;
  label?: string;
  purpose?: string;
  /** "memory" | "conversational" | "multi_step" | "tool" | "integrity". */
  kind?: string;
  example?: string | null;
}

export interface GlossaryMetric {
  key?: string;
  label?: string;
  description?: string;
}

export interface GlossaryVersion {
  version?: number;
  title?: string;
  summary?: string;
  epoch?: string;
  highlights?: string[];
}

export interface GlossaryPayload {
  categories?: GlossaryCategory[];
  metrics?: GlossaryMetric[];
  versions?: GlossaryVersion[];
}

// ── Bench config (/public/bench/config) ──────────────────────

export interface BenchHarnessConfig {
  canonical_id?: string;
  serving?: string;
  reasoning_effort?: string | null;
  thinking?: boolean;
}

export interface BenchConfigPayload {
  bench_version?: number;
  harness: BenchHarnessConfig;
  public_mirror_url_template?: string | null;
  ledger_path?: string;
  desired_bench_version?: number | null;
  dataset?: Record<string, unknown>;
  grading?: Record<string, unknown>;
  generated_at?: string;
  public_transcript_url_template?: string | null;
  public_transcript_telemetry_url_template?: string | null;
}

// ── Memory timeline (/public/bench/timeline) ─────────────────

export interface TimelineRelease {
  bench_version?: number | string;
  title?: string;
  released_at?: string;
  activated_at?: string | null;
}

export interface TimelineMinerPoint {
  memory_mean?: number | string;
  recorded_at?: string;
  bench_version?: number | string;
  agent_name?: string | null;
  agent_id?: string;
  hermes_memory_mean?: number | string | null;
  openclaw_memory_mean?: number | string | null;
}

export interface TimelinePayload {
  releases?: TimelineRelease[];
  points?: TimelineMinerPoint[];
  metric?: string;
  score_quorum?: number;
  generated_at?: string;
}
