// The historical miner + third-party memory timeline (monolith 4238–4731),
// split into a pure chart builder (memoryTimelineHtml — the whole SVG is
// computed from data + options, so tests can render it headlessly) and the
// per-version field cache (loadMemoryField). Every contract rescores from
// scratch, so a wall-clock axis is the wrong shape for this data: each
// contract gets an equal band and the points are ordered inside it. The
// reigning champion's crown plate is the point of the chart; gaps (a
// reference harness with no run, an open rollout) are named in place, never
// papered over.
import { createSignal } from "solid-js";
import type { Accessor } from "solid-js";

import { getJSON, poolMap } from "../../lib/api";
import { entityAnchorAttrs } from "../../lib/entity-links";
import { agentName, esc, fx, releaseTime, timelineDate } from "../../lib/format";
import type { LeaderboardPayload, TimelinePayload, TimelineRelease } from "../../types";

// ── Off-network comparison records (monolith 3945–3985) ──────
// Every point is one retained practice run measured now against an immutable
// historical contract, then positioned at that contract's release date. These
// records never enter the leaderboard, KOTH, validator weights, or payouts.

export interface HarnessPoint {
  benchVersion: number;
  memoryMean: number;
  memoryCorrect: number;
  memoryCases: number;
  runId: string;
  measuredAt?: string;
  model?: string;
  route?: string;
  seed?: string;
  datasetSha256?: string;
}

export interface HarnessEvidence {
  id: string;
  label: string;
  subject: string;
  profile: string;
  seed: string;
  model: string;
  route: string;
  measuredAt: string;
  evidenceUrl: string;
  points: HarnessPoint[];
}

export const THIRD_PARTY_HARNESSES: HarnessEvidence[] = [
  {
    id: "hermes",
    label: "Hermes Agent",
    subject: "Hermes Agent 0.19.0",
    profile: "Native SessionDB session_search",
    seed: "3058240546919425205",
    model: "qwen/qwen3-32b",
    route: "OpenRouter · Nebius pinned",
    measuredAt: "2026-07-23",
    evidenceUrl:
      "https://github.com/ditto-assistant/ditto-subnet/tree/main/services/dittobench-api/docs/third-party-benchmark-timeline",
    points: [
      {
        benchVersion: 2,
        memoryMean: 0.1111111111111111,
        memoryCorrect: 6,
        memoryCases: 54,
        runId: "7c20350e-7b64-4a19-8913-dc4b9cdeb164",
      },
      {
        benchVersion: 3,
        memoryMean: 0.12796610169491526,
        memoryCorrect: 6,
        memoryCases: 59,
        runId: "b5ca5d5d-56cc-465e-ae38-56d72ffe397b",
      },
      {
        benchVersion: 4,
        memoryMean: 0.17796610169491525,
        memoryCorrect: 10,
        memoryCases: 59,
        runId: "97570165-5e96-4d98-b053-7d5b36a28c90",
      },
      {
        benchVersion: 5,
        memoryMean: 0.16907051041666668,
        memoryCorrect: 16,
        memoryCases: 96,
        runId: "31c7b01b-be22-4b28-b27d-9a82f299d0a3",
      },
      {
        benchVersion: 6,
        memoryMean: 0.19791666666666666,
        memoryCorrect: 19,
        memoryCases: 96,
        runId: "c9230dd4-b5bd-4543-94a7-6cc5f2521161",
      },
      {
        benchVersion: 7,
        memoryMean: 0.13636363636363635,
        memoryCorrect: 27,
        memoryCases: 198,
        runId: "34178537-0529-48d8-8421-8b7c566db2d4",
        measuredAt: "2026-07-26",
        model: "openai/gpt-oss-20b",
        route: "OpenRouter · aggregate throughput",
      },
      {
        benchVersion: 8,
        memoryMean: 0.029880478087649404,
        memoryCorrect: 5,
        memoryCases: 251,
        runId: "0bce82c0-e1da-42b8-8b25-d3f47b13f117",
        measuredAt: "2026-08-04",
        model: "openai/gpt-oss-20b",
        route: "OpenRouter · aggregate throughput",
        seed: "123456789",
        datasetSha256: "6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98",
      },
    ],
  },
  {
    id: "openclaw",
    label: "OpenClaw",
    subject: "OpenClaw 2026.7.1",
    profile: "Native memory-core FTS · 20-result recall",
    seed: "3058240546919425205",
    model: "qwen/qwen3-32b",
    route: "OpenRouter · Nebius pinned",
    measuredAt: "2026-07-23",
    evidenceUrl:
      "https://github.com/ditto-assistant/ditto-subnet/tree/main/services/dittobench-api/docs/third-party-benchmark-timeline",
    points: [
      {
        benchVersion: 2,
        memoryMean: 0.3333333333333333,
        memoryCorrect: 18,
        memoryCases: 54,
        runId: "aff3b762-45cf-48e9-9293-9759352b4ab4",
      },
      {
        benchVersion: 3,
        memoryMean: 0.3771186440677966,
        memoryCorrect: 22,
        memoryCases: 59,
        runId: "025956de-c312-4cd4-8a04-ca636ffc143a",
      },
      {
        benchVersion: 4,
        memoryMean: 0.3559322033898305,
        memoryCorrect: 21,
        memoryCases: 59,
        runId: "37662b1a-fdd2-4135-a790-749750e33b46",
      },
      {
        benchVersion: 5,
        memoryMean: 0.421875,
        memoryCorrect: 40,
        memoryCases: 96,
        runId: "bc4cf453-ddce-4e66-ab67-659280143d62",
      },
      {
        benchVersion: 6,
        memoryMean: 0.3333333333333333,
        memoryCorrect: 32,
        memoryCases: 96,
        runId: "5fd141aa-25e6-476c-9cc4-1b4279ee6559",
      },
      {
        benchVersion: 7,
        memoryMean: 0.22601010101010102,
        memoryCorrect: 44,
        memoryCases: 198,
        runId: "dd651606-bcfd-4ed8-83ae-926a0a19ee6b",
        measuredAt: "2026-07-25",
        model: "openai/gpt-oss-20b",
        route: "OpenRouter · aggregate throughput",
      },
      {
        benchVersion: 8,
        memoryMean: 0.40039840637450197,
        memoryCorrect: 98,
        memoryCases: 251,
        runId: "d3ddbb28-1240-46a5-b851-560582657f08",
        measuredAt: "2026-08-03",
        model: "openai/gpt-oss-20b",
        route: "OpenRouter · aggregate throughput",
        seed: "123456789",
        datasetSha256: "6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98",
      },
    ],
  },
];

// A reference harness is measured per contract, and a contract that shipped
// after its measurement date simply has no run. That gap is load-bearing:
// these are third-party systems, and a line that stops without saying why
// reads as a baseline that collapsed. v7 is the live case — it re-froze the
// inference contract onto a different model, so a v6 number cannot be
// carried across even in principle. Nothing here is ever interpolated.
export function harnessMeasuredVersions(evidence: HarnessEvidence): number[] {
  return (evidence.points || [])
    .map((point) => Number(point.benchVersion))
    .filter((version) => isFinite(version))
    .sort((a, b) => a - b);
}

export function harnessUnmeasuredVersions(
  evidence: HarnessEvidence,
  shownVersions: Record<number, boolean>,
): number[] {
  const measured: Record<number, boolean> = {};
  harnessMeasuredVersions(evidence).forEach((version) => {
    measured[version] = true;
  });
  return Object.keys(shownVersions)
    .map(Number)
    .filter((version) => !measured[version])
    .sort((a, b) => a - b);
}

export function versionList(versions: number[]): string {
  return versions.map((version) => "v" + version).join(", ");
}

// One sentence covering both series, so the reader learns the coverage and
// the gap together rather than inferring the gap from a missing line.
export function harnessCoverageText(shownVersions: Record<number, boolean>): string {
  const perSeries = THIRD_PARTY_HARNESSES.map((evidence) =>
    versionList(harnessMeasuredVersions(evidence)),
  );
  const shared = perSeries.every((list) => list === perSeries[0]);
  const covered = shared
    ? perSeries[0] + " for both"
    : THIRD_PARTY_HARNESSES.map(
        (evidence, index) => evidence.label + " on " + perSeries[index],
      ).join(" and ");
  const gaps: Record<number, boolean> = {};
  THIRD_PARTY_HARNESSES.forEach((evidence) => {
    harnessUnmeasuredVersions(evidence, shownVersions).forEach((version) => {
      gaps[version] = true;
    });
  });
  const missing = Object.keys(gaps)
    .map(Number)
    .sort((a, b) => a - b);
  if (!missing.length) return covered + ".";
  return (
    covered +
    ". No reference run exists on " +
    versionList(missing) +
    " yet, so those bands carry no reference line; the absence is a missing measurement, not a measured zero."
  );
}

/** The method paragraph under the chart (monolith 4322–4326), rebuilt from
 * the records + the shown window so the coverage sentence can never drift
 * from the plotted lines. */
export function harnessMethodText(shownVersions: Record<number, boolean>): string {
  return (
    "The miner line is the running high of finalized three-validator memory medians within each benchmark version; points are ordered by their actual scoring completion time. The horizontal axis gives every contract an equal band rather than equal clock time, because contracts ran for very different durations and the short ones were unreadable on a wall-clock scale. Hermes and OpenClaw are single-seed off-network practice runs. Versions 2–6 used the same Qwen3-32B model and pinned OpenRouter/Nebius route; v7–v8 use the frozen GPT-OSS-20B aggregate OpenRouter route. Versions 2–7 use seed " +
    (THIRD_PARTY_HARNESSES[0] as HarnessEvidence).seed +
    "; v8 uses its released reference seed 123456789, covering " +
    harnessCoverageText(shownVersions) +
    " Their points are positioned in each immutable contract's band for comparison, not presented as historical measurements. Version changes are not all monotonic difficulty increases: v4 corrects v3 false positives. Third-party harnesses never enter score rank, KOTH, validator weights, or payouts."
  );
}

// ── Per-version field cache (monolith 4661–4698) ─────────────
// The record line alone hides the shape of the competition: the field is
// what shows a whole subnet climbing. Every contract's full finalized board
// is already public per bench_version, so the scatter needs no new endpoint.
// Settled contracts are immutable, so each is fetched once and kept; only
// the newest board is allowed to refetch.

export interface MemoryFieldEntry {
  version: number;
  score: number;
  composite: number;
  name: string;
  agentId: string | undefined;
  hotkey: string;
  firstSeen: number;
}

let memoryFieldByVersion: Record<number, MemoryFieldEntry[]> = {};
// Submissions that have been scored but have not reached quorum. They are
// deliberately *not* plotted — the chart's contract is finalized runs only,
// and drawing a provisional score would let a rank move retroactively when
// the third validator lands. The count is kept so an unsettled contract can
// say how much is still outstanding instead of looking complete.
let memoryPendingByVersion: Record<number, number> = {};

const [fieldRevision, setFieldRevision] = createSignal(0);
/** Bumps whenever loadMemoryField lands new boards (reactive re-render cue). */
export const memoryFieldRevision: Accessor<number> = fieldRevision;

export function memoryFieldSnapshot(): {
  fieldByVersion: Record<number, MemoryFieldEntry[]>;
  pendingByVersion: Record<number, number>;
} {
  return { fieldByVersion: memoryFieldByVersion, pendingByVersion: memoryPendingByVersion };
}

export function resetMemoryFieldCache(): void {
  memoryFieldByVersion = {};
  memoryPendingByVersion = {};
}

export function loadMemoryField(
  versions: number[],
  activeVersion: number,
): Promise<Record<number, MemoryFieldEntry[]>> {
  const wanted = versions.filter(
    (version) => !memoryFieldByVersion[version] || version === activeVersion,
  );
  if (!wanted.length) return Promise.resolve(memoryFieldByVersion);
  return poolMap(wanted, 3, (version) =>
    getJSON<LeaderboardPayload>("/public/leaderboard?bench_version=" + version)
      .then((board) => {
        const entries = (board && board.entries) || [];
        memoryPendingByVersion[version] = entries.filter(
          (entry) => entry.finalized === false,
        ).length;
        memoryFieldByVersion[version] = entries
          .filter(
            (entry) => entry.finalized !== false && Number.isFinite(Number(entry.memory_mean)),
          )
          .map((entry) => ({
            version,
            score: Number(entry.memory_mean),
            composite: Number(entry.composite),
            name: agentName(entry.agent_name),
            agentId: entry.agent_id,
            hotkey: entry.miner_hotkey,
            firstSeen: Date.parse(entry.first_seen ?? "") || 0,
          }));
      })
      .catch(() => {
        /* a missing board just means no cloud for that band */
      }),
  ).then(() => {
    setFieldRevision((revision) => revision + 1);
    return memoryFieldByVersion;
  });
}

// ── The chart itself (renderMemoryTimeline 4293–4654) ────────

// The generation ramp is validated for up to six steps; past that the
// lightness gap between adjacent contracts drops below the legible floor,
// so window to the most recent six rather than silently muddying it.
export const TIMELINE_MAX_ERAS = 6;

/** The rollout fields the chart reads (monolith reads them off lastRollout). */
export interface TimelineRolloutContext {
  active_version?: number | null;
  desired_version?: number | null;
  status?: string | null;
}

export interface MemoryTimelineOptions {
  /** Measured target width (the viewBox is measured, not fixed). */
  width: number;
  /** Below 1181px CSS the chart may take the tall portrait branch. */
  phoneViewport: boolean;
  rollout: TimelineRolloutContext | null;
  /** The live emissions fold's champion hotkey (crown identity). */
  championHotkey: string | null;
  fieldByVersion: Record<number, MemoryFieldEntry[]>;
  pendingByVersion: Record<number, number>;
}

export type MemoryTimelineResult =
  | { kind: "chart"; html: string }
  | { kind: "state"; text: string };

interface ChartPoint {
  at: number;
  score: number;
  version: number;
  tooltip: string;
  source: string;
  measured: string;
  agentId?: string | undefined;
  runId?: string;
  x: number;
  y: number;
}

interface Era {
  version: number;
  release: TimelineRelease;
  at: number;
  index: number;
  points: ChartPoint[];
  pending: number;
  open: boolean;
  color: string;
  field: Array<{
    x: number;
    y: number;
    entry: MemoryFieldEntry;
    tooltip: string;
    champion?: boolean;
  }>;
}

function timelinePath(points: Array<{ x: number; y: number }>): string {
  return points
    .map((point, index) => (index ? "L" : "M") + point.x.toFixed(2) + " " + point.y.toFixed(2))
    .join(" ");
}

function timelinePoint(point: ChartPoint, series: string, color: string, radius: number): string {
  const label = point.tooltip;
  return (
    '<circle class="timeline-point ' +
    series +
    '" cx="' +
    point.x.toFixed(2) +
    '" cy="' +
    point.y.toFixed(2) +
    '" r="' +
    (radius || 5) +
    '"' +
    (color ? ' style="fill:' + color + '"' : "") +
    ' tabindex="0" role="img" aria-label="' +
    esc(label) +
    '" data-timeline-tooltip="' +
    esc(label) +
    '"><title>' +
    esc(label) +
    "</title></circle>"
  );
}

/** An entity anchor as raw markup for the exact-data table (the chart is one
 * innerHTML write, exactly like the monolith's). */
function entityAnchorHtml(agentId: string, label: string): string {
  const attrs = entityAnchorAttrs("agent", agentId, label);
  if (!attrs) return esc(label);
  return (
    '<a class="' +
    esc(attrs.class) +
    '" href="' +
    esc(attrs.href) +
    '" data-entity-link="' +
    esc(attrs["data-entity-link"]) +
    '">' +
    esc(attrs.label) +
    "</a>"
  );
}

export function memoryTimelineHtml(
  data: TimelinePayload | null,
  options: MemoryTimelineOptions,
): MemoryTimelineResult {
  const allReleases = ((data && data.releases) || [])
    .filter((release) => Number(release.bench_version) >= 2)
    .sort((a, b) => Number(a.bench_version) - Number(b.bench_version));
  const releases = allReleases.slice(-TIMELINE_MAX_ERAS);
  const shownVersions: Record<number, boolean> = {};
  releases.forEach((release) => {
    shownVersions[Number(release.bench_version)] = true;
  });
  const minerPoints: ChartPoint[] = ((data && data.points) || [])
    .filter(
      (point) =>
        shownVersions[Number(point.bench_version)] &&
        Number.isFinite(Number(point.memory_mean)) &&
        Date.parse(point.recorded_at ?? ""),
    )
    .sort((a, b) => Date.parse(a.recorded_at ?? "") - Date.parse(b.recorded_at ?? ""))
    .map((point) => {
      const name = agentName(point.agent_name);
      return {
        at: Date.parse(point.recorded_at ?? ""),
        score: Number(point.memory_mean),
        version: Number(point.bench_version),
        tooltip:
          "Top miner · " +
          name +
          " · v" +
          point.bench_version +
          " · memory " +
          fx(Number(point.memory_mean)) +
          " · finalized " +
          timelineDate(point.recorded_at as string),
        source: name,
        measured: point.recorded_at as string,
        agentId: point.agent_id,
        x: 0,
        y: 0,
      };
    });

  if (!releases.length) {
    return {
      kind: "state",
      text: "Benchmark release history is temporarily unavailable. No substitute timeline is shown.",
    };
  }

  const eras: Era[] = releases.map((release, index) => ({
    version: Number(release.bench_version),
    release,
    at: releaseTime(release),
    index,
    points: [],
    pending: 0,
    open: false,
    color: "",
    field: [],
  }));
  const eraByVersion: Record<number, Era> = {};
  eras.forEach((era) => {
    eraByVersion[era.version] = era;
  });
  minerPoints.forEach((point) => {
    const era = eraByVersion[point.version];
    if (era) era.points.push(point);
  });

  // A contract can be on the chart before its rollout has settled, and the
  // band must not imply otherwise: it is drawn from the finalized runs so
  // far, and both the record and the field can still move. Rather than
  // invent a convention, reuse the rollout strip's — its own status word
  // and its own accent — and read the state from /public/bench/rollout,
  // which the board already polls. `pending` counts submissions that have
  // scored but not yet reached quorum, so they are legitimately absent from
  // a finalized-only chart while still being the reason it can change.
  let openVersion: number | null = null;
  if (options.rollout) {
    const rolloutStatus = String(options.rollout.status || "");
    const desiredVersion = Number(options.rollout.desired_version);
    if (
      rolloutStatus !== "activated" &&
      rolloutStatus !== "superseded" &&
      eraByVersion[desiredVersion]
    ) {
      openVersion = desiredVersion;
    }
  }
  eras.forEach((era) => {
    era.pending = options.pendingByVersion[era.version] || 0;
    era.open = era.version === openVersion;
  });

  const width = Math.max(300, Math.min(1200, options.width || 960));
  // A sub-560px measure can mean a phone viewport or the desktop split's
  // rail; only the phone deserves the tall portrait aspect. In the split,
  // stay on the landscape branch so the rail chart never squares off.
  const narrow = width < 560 && options.phoneViewport;
  const tight = width < 400 && options.phoneViewport;
  const height = narrow
    ? Math.max(300, Math.round(width * 0.86))
    : Math.max(272, Math.min(376, Math.round(width * 0.34)));
  const left = tight ? 32 : narrow ? 40 : 58;
  const right = narrow ? 14 : 26;
  // Reserve a slim annotation gutter above the plot. The reigning champion
  // plate lives here so a high score cannot cover the latest record line or
  // the finalized-run cloud it is meant to explain.
  const top = narrow ? 28 : 34;
  const bottom = narrow ? 52 : 62;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const bandWidth = plotWidth / eras.length;
  const inset = Math.min(bandWidth * 0.2, narrow ? 14 : 40);
  const ticks = narrow ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const dotR = narrow ? 2.1 : 2.6;
  const recordR = narrow ? 3.6 : 5;
  const bandStart = (index: number): number => left + index * bandWidth;
  const bandCenter = (index: number): number => left + (index + 0.5) * bandWidth;
  const y = (value: number): number => top + (1 - value) * plotHeight;
  // Points spread left-to-right inside their own band in scoring order.
  function placeInBand(era: Era, i: number, n: number): number {
    if (n <= 1) return bandCenter(era.index);
    return bandStart(era.index) + inset + (i * (bandWidth - 2 * inset)) / (n - 1);
  }
  // One ordinal ramp, interpolated in OKLCH between two theme-aware ends, so
  // the reader sees the generation order in the colour itself and the ramp
  // keeps working when a new contract ships.
  function eraColor(index: number): string {
    if (eras.length <= 1) return "var(--era-to)";
    return (
      "color-mix(in oklch, var(--era-to) " +
      Math.round((index / (eras.length - 1)) * 100) +
      "%, var(--era-from))"
    );
  }
  eras.forEach((era) => {
    era.color = eraColor(era.index);
    era.points.forEach((point, i) => {
      point.x = placeInBand(era, i, era.points.length);
      point.y = y(point.score);
    });
  });

  const externalSeries = THIRD_PARTY_HARNESSES.map((evidence) => ({
    id: evidence.id,
    evidence,
    points: (evidence.points || [])
      .filter(
        (point) =>
          eraByVersion[Number(point.benchVersion)] && Number.isFinite(Number(point.memoryMean)),
      )
      .map((point): ChartPoint => {
        const era = eraByVersion[Number(point.benchVersion)] as Era;
        return {
          at: era.at,
          score: Number(point.memoryMean),
          version: era.version,
          x: bandCenter(era.index),
          y: y(Number(point.memoryMean)),
          tooltip:
            evidence.subject +
            " · v" +
            point.benchVersion +
            " · memory " +
            fx(Number(point.memoryMean)) +
            " (" +
            point.memoryCorrect +
            "/" +
            point.memoryCases +
            ") · " +
            (point.model || evidence.model) +
            " · " +
            (point.route || evidence.route) +
            " · seed " +
            (point.seed || evidence.seed) +
            " · measured " +
            (point.measuredAt || evidence.measuredAt),
          source: evidence.subject,
          measured: point.measuredAt || evidence.measuredAt,
          runId: point.runId,
        };
      })
      .sort((a, b) => a.version - b.version),
    unmeasured: harnessUnmeasuredVersions(evidence, shownVersions),
  }));
  // Contracts on the chart that no reference harness has run. The lines end
  // there, and an unexplained ending is the failure mode this guards: a
  // reader would take it for a baseline that fell off a cliff. So the gap
  // gets an end cap on each line and a label standing in the empty band,
  // and no segment is ever drawn across it.
  const unmeasuredEras = eras.filter(
    (era) =>
      externalSeries.length &&
      externalSeries.every((series) => series.unmeasured.indexOf(era.version) !== -1),
  );

  // The whole field for each contract, ordered by upload time so the cloud
  // reads left-to-right like the records above it. Champion is looked up by
  // hotkey from the live emissions payload.
  const championHotkey = options.championHotkey;
  let fieldCount = 0;
  let championDot: { point: Era["field"][number]; era: Era; entry: MemoryFieldEntry } | null = null;
  eras.forEach((era) => {
    const field = (options.fieldByVersion[era.version] || [])
      .slice()
      .sort((a, b) => a.firstSeen - b.firstSeen || a.score - b.score);
    fieldCount += field.length;
    era.field = field.map((entry, i) => {
      const point: Era["field"][number] = {
        x: placeInBand(era, i, field.length),
        y: y(entry.score),
        entry,
        tooltip:
          entry.name +
          " · v" +
          era.version +
          " · memory " +
          fx(entry.score) +
          " · composite " +
          fx(entry.composite),
      };
      if (championHotkey && entry.hotkey === championHotkey) point.champion = true;
      if (point.champion && era.index === eras.length - 1) championDot = { point, era, entry };
      return point;
    });
  });

  const svg: string[] = [];
  // Alternating band tints make the generation boundaries readable without
  // adding another line to the plot.
  eras.forEach((era) => {
    if (era.index % 2 || era.open) {
      svg.push(
        '<rect class="timeline-band' +
          (era.open ? " open" : "") +
          '" x="' +
          bandStart(era.index).toFixed(2) +
          '" y="' +
          top +
          '" width="' +
          bandWidth.toFixed(2) +
          '" height="' +
          plotHeight +
          '"></rect>',
      );
    }
  });
  ticks.forEach((tick) => {
    svg.push(
      '<line class="timeline-grid" x1="' +
        left +
        '" x2="' +
        (width - right) +
        '" y1="' +
        y(tick) +
        '" y2="' +
        y(tick) +
        '"></line>',
    );
    svg.push(
      '<text class="timeline-axis-label" x="' +
        (left - 10) +
        '" y="' +
        (y(tick) + 3) +
        '" text-anchor="end">' +
        tick.toFixed(2).replace(/^0/, "") +
        "</text>",
    );
  });
  eras.forEach((era) => {
    if (era.index) {
      svg.push(
        '<line class="timeline-release" x1="' +
          bandStart(era.index).toFixed(2) +
          '" x2="' +
          bandStart(era.index).toFixed(2) +
          '" y1="' +
          top +
          '" y2="' +
          (height - bottom) +
          '"></line>',
      );
    }
    const releaseTooltip =
      "DittoBench v" +
      era.version +
      " · " +
      era.release.title +
      " · released " +
      timelineDate(era.release.released_at as string) +
      (era.release.activated_at ? " · activated " + timelineDate(era.release.activated_at) : "") +
      " · " +
      era.points.length +
      (era.points.length === 1 ? " record" : " records") +
      (era.open ? " · rollout still collecting" : "") +
      (era.pending
        ? " · " +
          era.pending +
          " scored " +
          (era.pending === 1 ? "submission is" : "submissions are") +
          " still short of quorum and not plotted"
        : "");
    svg.push(
      '<text class="timeline-era-label" x="' +
        bandCenter(era.index).toFixed(2) +
        '" y="' +
        (height - bottom + (narrow ? 20 : 22)) +
        '" text-anchor="middle" style="fill:' +
        era.color +
        '" tabindex="0" role="img" aria-label="' +
        esc(releaseTooltip) +
        '" data-timeline-tooltip="' +
        esc(releaseTooltip) +
        '">v' +
        esc(era.version) +
        "<title>" +
        esc(releaseTooltip) +
        "</title></text>",
    );
    svg.push(
      '<text class="timeline-era-date" x="' +
        bandCenter(era.index).toFixed(2) +
        '" y="' +
        (height - bottom + (narrow ? 34 : 38)) +
        '" text-anchor="middle">' +
        esc(timelineDate(era.release.released_at as string)) +
        "</text>",
    );
  });

  // Field first so the record line and its points always sit on top of the
  // cloud rather than disappearing into it.
  eras.forEach((era) => {
    (era.field || []).forEach((point, i) => {
      svg.push(
        '<circle class="timeline-field' +
          (point.champion ? " champion" : "") +
          '" cx="' +
          point.x.toFixed(2) +
          '" cy="' +
          point.y.toFixed(2) +
          '" r="' +
          (point.champion ? recordR + 1 : dotR) +
          '" style="fill:' +
          era.color +
          "; --i:" +
          Math.min(i, 28) +
          '" role="img" aria-label="' +
          esc(point.tooltip) +
          '" data-timeline-tooltip="' +
          esc(point.tooltip) +
          '"><title>' +
          esc(point.tooltip) +
          "</title></circle>",
      );
    });
  });

  // Carry the eye across the contract boundary without pretending the score
  // continued: the connector is muted and dashed, the era segments are solid.
  const populated = eras.filter((era) => era.points.length);
  populated.forEach((era, i) => {
    const next = populated[i + 1];
    if (!next) return;
    const from = era.points[era.points.length - 1] as ChartPoint;
    const to = next.points[0] as ChartPoint;
    svg.push(
      '<path class="timeline-link" d="M' +
        from.x.toFixed(2) +
        " " +
        from.y.toFixed(2) +
        "L" +
        to.x.toFixed(2) +
        " " +
        to.y.toFixed(2) +
        '"></path>',
    );
  });
  populated.forEach((era) => {
    if (era.points.length > 1) {
      svg.push(
        '<path class="timeline-path miner" pathLength="1" d="' +
          timelinePath(era.points) +
          '" style="stroke:' +
          era.color +
          '"></path>',
      );
    }
  });
  externalSeries.forEach((series) => {
    if (series.points.length > 1)
      svg.push(
        '<path class="timeline-path ' +
          series.id +
          '" d="' +
          timelinePath(series.points) +
          '"></path>',
      );
    series.points.forEach((point) => {
      svg.push(timelinePoint(point, series.id, "", recordR));
    });
    // Stop bar on the last measured point when a later contract has no run:
    // the line is over, deliberately, rather than trailing off.
    const last = series.points[series.points.length - 1];
    if (last && series.unmeasured.some((version) => version > last.version)) {
      svg.push(
        '<line class="timeline-series-end ' +
          series.id +
          '" x1="' +
          last.x.toFixed(2) +
          '" x2="' +
          last.x.toFixed(2) +
          '" y1="' +
          (last.y - recordR - 4).toFixed(2) +
          '" y2="' +
          (last.y + recordR + 4).toFixed(2) +
          '"></line>',
      );
    }
  });
  // The label goes where the missing lines would have been — the reader's
  // eye is already there — so the empty space is answered in place, not only
  // in the caption underneath.
  if (unmeasuredEras.length) {
    const gapLines = tight
      ? ["no reference", "run yet"]
      : ["Hermes · OpenClaw", "not yet measured"];
    const lastMeasuredYs = externalSeries
      .map((series) => {
        const last = series.points[series.points.length - 1];
        return last ? last.y : null;
      })
      .filter((value): value is number => value != null);
    // Just under the lowest reference line, not level with it: at phone
    // widths the band is narrow enough that a label on the lines' own row
    // lands on their end caps, and a note explaining a gap must not be the
    // thing that obscures it.
    let anchorY = (lastMeasuredYs.length ? Math.max(...lastMeasuredYs) : y(0.25)) + 20;
    // Keep the block inside the plot whichever way the reference lines ran.
    anchorY = Math.min(
      Math.max(anchorY, top + 26),
      height - bottom - 15 - 13 * (gapLines.length - 1),
    );
    const gapNote =
      "Hermes Agent and OpenClaw have no run on " +
      versionList(unmeasuredEras.map((era) => era.version)) +
      ". The reference lines end at v" +
      Math.max(
        ...externalSeries.map((series) => {
          const last = series.points[series.points.length - 1];
          return last ? last.version : 0;
        }),
      ) +
      " because the measurement does not exist yet, not because the score fell.";
    // On a phone the band is narrower than the label, so centring it on the
    // band alone pushes half the words off the viewBox where they are
    // clipped rather than read. Clamp to the plot: nudging the label a
    // little into the neighbouring band costs nothing, losing it costs the
    // whole explanation. Monospace, so the width is countable.
    const gapHalf = Math.max(...gapLines.map((line) => line.length)) * 3 + 4;
    // One label per run of adjacent unmeasured contracts, centred on the run,
    // not one per band: two neighbouring bands each narrower than the label
    // (the desktop rail at six eras) put the same words on top of themselves
    // and nothing was readable. The tooltip target spans the run, so the
    // explanation is still reachable from anywhere the lines are missing.
    const gapRuns: Array<{ first: number; last: number }> = [];
    unmeasuredEras.forEach((era) => {
      const run = gapRuns[gapRuns.length - 1];
      if (run && era.index === run.last + 1) run.last = era.index;
      else gapRuns.push({ first: era.index, last: era.index });
    });
    gapRuns.forEach((run) => {
      const runCenter = (bandCenter(run.first) + bandCenter(run.last)) / 2;
      const gapX = Math.min(Math.max(runCenter, left + gapHalf), width - right - gapHalf);
      svg.push(
        '<g class="timeline-unmeasured" tabindex="0" role="img" aria-label="' +
          esc(gapNote) +
          '" data-timeline-tooltip="' +
          esc(gapNote) +
          '">' +
          gapLines
            .map(
              (line, i) =>
                '<text x="' +
                gapX.toFixed(2) +
                '" y="' +
                (anchorY + i * 13).toFixed(2) +
                '" text-anchor="middle">' +
                esc(line) +
                "</text>",
            )
            .join("") +
          "<title>" +
          esc(gapNote) +
          "</title></g>",
      );
    });
  }
  populated.forEach((era) => {
    era.points.forEach((point) => {
      svg.push(timelinePoint(point, "miner", era.color, recordR));
    });
  });
  // Crown the reigning champion. Who holds the crown is the single piece of
  // state this chart exists to show, so it gets the loudest treatment on the
  // plot: a halo, a drop line to its own contract, a slow pulse, and a named
  // plate carrying the score. Identity never rests on colour alone.
  const crowned = championDot as {
    point: Era["field"][number];
    era: Era;
    entry: MemoryFieldEntry;
  } | null;
  if (crowned) {
    const cx = crowned.point.x;
    const cy = crowned.point.y;
    const crownName = crowned.entry.name;
    const crownScore = fx(crowned.entry.score);
    // Monospace, so width is countable: name + the crown glyph and its
    // space, plus room for the right-aligned score and the gap between.
    const plateW = Math.min(
      Math.max((crownName.length + 2) * 6.7 + 74, 124),
      Math.max(plotWidth - 8, 110),
    );
    const plateH = 21;
    // Keep the plate in the annotation gutter, never over the data field.
    // Centre it on the champion where possible and clamp it at both edges.
    const plateX = Math.min(Math.max(cx - plateW / 2, left + 2), width - right - plateW - 2);
    const plateY = top - plateH - 5;
    svg.push(
      '<line class="timeline-champion-drop" x1="' +
        cx.toFixed(2) +
        '" x2="' +
        cx.toFixed(2) +
        '" y1="' +
        (cy + 8).toFixed(2) +
        '" y2="' +
        (height - bottom) +
        '"></line>',
    );
    svg.push(
      '<circle class="timeline-champion-halo" cx="' +
        cx.toFixed(2) +
        '" cy="' +
        cy.toFixed(2) +
        '" r="' +
        (recordR + 13) +
        '"></circle>',
    );
    svg.push(
      '<circle class="timeline-champion-pulse" cx="' +
        cx.toFixed(2) +
        '" cy="' +
        cy.toFixed(2) +
        '" r="' +
        (recordR + 5) +
        '"></circle>',
    );
    svg.push(
      '<circle class="timeline-champion-ring" cx="' +
        cx.toFixed(2) +
        '" cy="' +
        cy.toFixed(2) +
        '" r="' +
        (recordR + 5) +
        '"></circle>',
    );
    svg.push(
      '<g class="timeline-champion-plate" tabindex="0" role="img" aria-label="' +
        esc("Reigning champion " + crownName + ", memory " + crownScore) +
        '" data-timeline-tooltip="' +
        esc("Reigning champion · " + crownName + " · memory " + crownScore) +
        '">' +
        '<rect x="' +
        plateX.toFixed(2) +
        '" y="' +
        plateY.toFixed(2) +
        '" width="' +
        plateW.toFixed(2) +
        '" height="' +
        plateH +
        '" rx="3"></rect>' +
        '<text x="' +
        (plateX + 9).toFixed(2) +
        '" y="' +
        (plateY + 14.5).toFixed(2) +
        '">♛ ' +
        esc(crownName) +
        "</text>" +
        '<text class="crown-score" x="' +
        (plateX + plateW - 9).toFixed(2) +
        '" y="' +
        (plateY + 14.5).toFixed(2) +
        '" text-anchor="end">' +
        esc(crownScore) +
        "</text>" +
        "<title>" +
        esc("Reigning champion · " + crownName + " · memory " + crownScore) +
        "</title></g>",
    );
  }

  const latestMiner = minerPoints.length
    ? (minerPoints[minerPoints.length - 1] as ChartPoint)
    : null;
  const latestReferences = externalSeries
    .map((series) => series.points[series.points.length - 1])
    .filter((point): point is ChartPoint => Boolean(point));
  interface DataRow {
    source: string;
    version: number;
    placed: string;
    measured: string;
    score: number | null;
    agentId?: string | undefined;
  }
  const rows: DataRow[] = minerPoints.map((point) => ({
    source: point.source,
    version: point.version,
    placed: timelineDate(point.at),
    measured: timelineDate(point.measured),
    score: point.score,
    agentId: point.agentId,
  }));
  externalSeries.forEach((series) => {
    series.points.forEach((point) => {
      rows.push({
        source: point.source,
        version: point.version,
        placed: timelineDate(point.at),
        measured: point.measured,
        score: point.score,
      });
    });
  });
  // Absent measurements are rows in the exact-data table too. A reader who
  // opens the table to check a number should find the gap stated there
  // rather than having to notice which contract is missing.
  externalSeries.forEach((series) => {
    series.unmeasured.forEach((version) => {
      rows.push({
        source: series.evidence.subject,
        version,
        placed: timelineDate((eraByVersion[version] as Era).release.released_at as string),
        measured: "not yet measured",
        score: null,
      });
    });
  });
  rows.sort(
    (a, b) =>
      a.version - b.version ||
      (b.score == null ? -1 : a.score == null ? 1 : b.score - a.score) ||
      a.source.localeCompare(b.source),
  );
  const pendingTotal = eras.reduce((total, era) => total + (era.pending || 0), 0);
  const eraLegend = eras
    .map(
      (era) =>
        '<span class="era' +
        (era.open ? " open" : "") +
        '"><i style="background:' +
        era.color +
        '"></i>v' +
        esc(era.version) +
        (era.open ? " collecting" : "") +
        "</span>",
    )
    .join("");
  // Each reference chip carries its own gap, so the legend answers "why does
  // that line stop" without the reader leaving the chart.
  const referenceLegend = externalSeries
    .map(
      (series) =>
        '<span class="' +
        series.id +
        '"><i></i>' +
        esc(series.evidence.label) +
        (series.unmeasured.length
          ? " <em>· not on " + esc(versionList(series.unmeasured)) + "</em>"
          : "") +
        "</span>",
    )
    .join("");
  const referenceThrough = latestReferences.length
    ? Math.max(...latestReferences.map((point) => point.version))
    : null;
  const firstEra = eras[0] as Era;
  const lastEra = eras[eras.length - 1] as Era;
  const html =
    '<p class="harness-comparison-summary">' +
    // "finalized", not "scored": provisional runs are scored too, and they
    // are the ones this chart deliberately leaves out.
    (fieldCount ? "<span><strong>" + fieldCount + "</strong> finalized runs</span>" : "") +
    "<span><strong>" +
    minerPoints.length +
    "</strong> records</span>" +
    (latestMiner
      ? "<span>Latest record <strong>" +
        fx(latestMiner.score) +
        "</strong> on <strong>v" +
        latestMiner.version +
        "</strong></span>"
      : "<span>Miner history is not yet available</span>") +
    (pendingTotal ? "<span><strong>" + pendingTotal + "</strong> awaiting quorum</span>" : "") +
    "<span><strong>" +
    latestReferences.length +
    "</strong> native reference series" +
    (referenceThrough != null && unmeasuredEras.length ? " through v" + referenceThrough : "") +
    "</span></p>" +
    '<div class="memory-timeline-legend" aria-label="Timeline legend"><span class="legend-group">Best finalized miner' +
    eraLegend +
    "</span>" +
    referenceLegend +
    "</div>" +
    '<div class="memory-timeline-frame" tabindex="0" role="region" aria-label="Memory-score timeline, horizontally scrollable on small screens"><svg class="memory-timeline-svg" viewBox="0 0 ' +
    width +
    " " +
    height +
    '" role="img" aria-labelledby="memory-timeline-title memory-timeline-desc"><title id="memory-timeline-title">Best miner memory score with Hermes and OpenClaw across DittoBench v' +
    firstEra.version +
    " through v" +
    lastEra.version +
    '</title><desc id="memory-timeline-desc">Memory score from zero to one. Each benchmark contract occupies an equal band across the horizontal axis, coloured light to dark as the generations advance, with its records ordered left to right inside the band. A muted dashed connector crosses each contract boundary because every contract rescores from scratch. Third-party points are retrospective measurements placed in each contract band, and a contract they have not been run on is labelled in place rather than left blank.</desc>' +
    svg.join("") +
    '</svg><div class="memory-timeline-tooltip" role="tooltip" hidden></div></div>' +
    '<details class="timeline-data-details"><summary>How to read this chart</summary>' +
    '<p class="memory-timeline-note">Faded dots are every finalized run on that contract, ordered by upload; the solid line traces the record as it was beaten. Runs that have been scored but have not reached quorum are not plotted, so a rank here can never move retroactively. Each contract gets an equal band, not equal clock time, so a generation that ran for hours stays as readable as one that ran for days. The dashed step between bands marks a contract change, where every agent rescores from scratch. Third-party points are retrospective runs; hover or focus any point for its exact date, model, and route. ' +
    esc(
      unmeasuredEras.length
        ? "No reference harness has been run on " +
            versionList(unmeasuredEras.map((era) => era.version)) +
            ", so their lines stop where the measurements stop; that band's empty space is an unmeasured contract, not a score of zero."
        : "Every contract shown carries a reference measurement.",
    ) +
    (openVersion
      ? " The v" +
        esc(openVersion) +
        " rollout is still collecting, so that band is tinted and can still change."
      : "") +
    "</p></details>" +
    '<details class="timeline-data-details"><summary>Exact timeline data</summary><div class="timeline-data-table-wrap"><table class="timeline-data-table"><thead><tr><th>Series</th><th>Contract</th><th>Placed at</th><th>Measured</th><th>Memory</th></tr></thead><tbody>' +
    rows
      .map(
        (row) =>
          "<tr><td>" +
          (row.agentId ? entityAnchorHtml(row.agentId, row.source) : esc(row.source)) +
          "</td><td>v" +
          esc(row.version) +
          "</td><td>" +
          esc(row.placed) +
          "</td><td>" +
          esc(row.measured) +
          "</td><td>" +
          (row.score == null ? "—" : fx(row.score)) +
          "</td></tr>",
      )
      .join("") +
    "</tbody></table></div></details>";
  return { kind: "chart", html };
}

/** The chart's own clamped tooltip (bindTimelineTooltips 4259–4290): shown
 * over the frame for whichever [data-timeline-tooltip] node is hovered or
 * focused, centred on its point and clamped inside the scrollable frame. */
export function bindTimelineTooltips(target: HTMLElement): void {
  const frame = target.querySelector<HTMLElement>(".memory-timeline-frame");
  const tooltip = target.querySelector<HTMLElement>(".memory-timeline-tooltip");
  if (!frame || !tooltip) return;
  function show(node: Element): void {
    const f = frame as HTMLElement;
    const t = tooltip as HTMLElement;
    const pointRect = node.getBoundingClientRect();
    const frameRect = f.getBoundingClientRect();
    t.textContent = node.getAttribute("data-timeline-tooltip") || "";
    t.hidden = false;
    // Measure after the text is in, then clamp inside the frame. The tooltip
    // is centred on its point and the frame is a scroll container, so a
    // point near either edge used to push the box out of view where it was
    // clipped instead of read.
    const half = t.offsetWidth / 2;
    const centre = pointRect.left - frameRect.left + f.scrollLeft + pointRect.width / 2;
    const lo = f.scrollLeft + half + 6;
    const hi = f.scrollLeft + f.clientWidth - half - 6;
    t.style.left = (hi < lo ? centre : Math.min(Math.max(centre, lo), hi)) + "px";
    // Flip below the point when there is no room above it.
    const above = pointRect.top - frameRect.top + f.scrollTop;
    const flip = above - t.offsetHeight - 10 < f.scrollTop;
    t.classList.toggle("below", flip);
    t.style.top = (flip ? above + pointRect.height : above) + "px";
  }
  function hide(): void {
    (tooltip as HTMLElement).hidden = true;
  }
  frame.querySelectorAll("[data-timeline-tooltip]").forEach((node) => {
    node.addEventListener("pointerenter", () => {
      show(node);
    });
    node.addEventListener("pointerleave", hide);
    node.addEventListener("focus", () => {
      show(node);
    });
    node.addEventListener("blur", hide);
  });
}
