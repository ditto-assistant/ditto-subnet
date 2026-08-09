// The built-output gate (assert-inventory rows 3, 7, 8, 20, 33, 34, 35, 36,
// 40 — the negative-grep halves the DOM tests defer here). Consensus
// parameters (incumbent margin, champion share, tail size, authority
// threshold, bench version) are API-served and must never be literals: a
// literal is a claim that silently stops being true, and miners read it as
// the rule they are judged by. Copy-level bans run against the BUILT dist
// assets (what browsers actually receive); code-pattern bans run against
// the source tree, where minification cannot launder identifiers.
import { beforeAll, describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");

/** Recursively collect files under a directory. */
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

let distText = "";
let srcText = "";

beforeAll(() => {
  // Build to a scratch outDir so the gate always judges the current source,
  // independent of any stale dist/ lying around (npm test runs before
  // npm run build in CI).
  const outDir = mkdtempSync(join(tmpdir(), "ditto-dashboard-gate-"));
  execFileSync(
    process.execPath,
    [
      join(ROOT, "node_modules", "vite", "bin", "vite.js"),
      "build",
      "--logLevel",
      "error",
      "--outDir",
      outDir,
      "--emptyOutDir",
    ],
    { cwd: ROOT, stdio: ["ignore", "ignore", "inherit"] },
  );
  distText = walk(outDir)
    .filter((path) => /\.(?:js|css|html|svg|json)$/.test(path))
    .map((path) => readFileSync(path, "utf-8"))
    .join("\n");
  expect(distText.length).toBeGreaterThan(0);

  // Product source only: the test tree quotes these strings on purpose.
  srcText = walk(join(ROOT, "src"))
    .filter((path) => /\.(?:ts|tsx|css)$/.test(path) && !/\.test\.tsx?$/.test(path))
    .map((path) => readFileSync(path, "utf-8"))
    .join("\n");
}, 180_000);

// ── Rows 35 + 36: no hardcoded fold constants, floor published as a floor ──
// Class docstring: consensus parameters are API-served and must never be
// markup literals. The "score to beat" is computed from the API-served
// margin (champComposite + effectiveMargin), published as a floor, not a
// guarantee — never the inlined champComposite * (1 + margin) formula.
describe("scoring transparency (rows 35/36)", () => {
  it("ships no hardcoded consensus constants in the built assets", () => {
    for (const banned of [
      "2% protection margin",
      "2% incumbent margin",
      "receives 90% of the miner pool",
      "up to four participation-tail recipients",
      "up to 25 miners",
    ]) {
      expect(distText, banned).not.toContain(banned);
    }
  });

  it("publishes the dethrone floor as a floor, not a guarantee", () => {
    expect(distText).toContain("this is a floor, not a guarantee");
    // The banned multiplicative formula must not reach a browser. (It is
    // quoted in a lib/scoring doc comment precisely to ban it, so the source
    // half of this grep would only catch its own documentation.)
    expect(distText).not.toContain("champComposite * (1 + margin)");
  });
});

// ── Row 39 half + task list: no literal bench version in explainer copy ─────
// The explainer titles are composed from the API-adopted version ("What
// DittoBench v" + v + " measures"); a digit baked into the copy is a claim
// that silently goes stale.
describe("bench versions never literal in explainer copy", () => {
  it("keeps version digits out of the built explainer strings", () => {
    expect(distText).not.toMatch(/What DittoBench v\d/);
    // Inspect the explainer string itself. A scan to the next HTML tag is not
    // meaningful in a minified JS bundle (there may be no ``<`` for hundreds
    // of symbols), and falsely treats unrelated v9 wire-field names as copy.
    expect(distText).not.toMatch(/measures and the frozen scoring setup.{0,32}v\d/);
  });

  // Row 7: the memory timeline window is count-based ("No version literal
  // decides what is drawn") — no bench-version equality tests anywhere in
  // product code.
  it("compares bench versions to data, never to a numeral (row 7)", () => {
    for (let version = 3; version <= 11; version += 1) {
      expect(srcText, "bench_version === " + version).not.toContain("bench_version === " + version);
      expect(srcText, "version === " + version).not.toContain("version === " + version);
    }
  });
});

// ── Row 8 half: API failures never render sample data ───────────────────────
// Failure states are explicit absence; no sample/demo payload may exist to
// fall back on.
describe("no sample data (row 8)", () => {
  it("bundles no SAMPLE fixtures", () => {
    for (const banned of ["var SAMPLE", "SAMPLE_HEALTH", "render(SAMPLE"]) {
      expect(distText, banned).not.toContain(banned);
      expect(srcText, banned).not.toContain(banned);
    }
  });
});

// ── Row 3 half: standings are never hidden behind a disclosure ──────────────
// ditto-platform#383 collapsed the leaderboard table behind a <details>;
// that stays banned — compactness comes from the second surface (the
// dedicated Leaderboard page), not from disclosure.
describe("no board disclosure (row 3)", () => {
  it("ships neither the board rail nor the details fold", () => {
    for (const banned of ['class="board-rail"', 'class="board-full"']) {
      expect(distText, banned).not.toContain(banned);
    }
    expect(srcText).not.toContain("board-rail");
    expect(srcText).not.toContain("board-full");
  });
});

// Agent evidence is data, not an optional action. The summary and history may
// settle at different times, but neither request is gated on a click.
describe("no manual agent-detail gate", () => {
  it("ships no load-details affordance", () => {
    expect(distText).not.toContain("Load details");
    expect(srcText).not.toContain("pipeline-history-disclosure");
  });
});

// ── Row 20 half: the rollout target never overwrites the current bench ──────
describe("bench authority (row 20)", () => {
  it("never assigns the desired version over the current bench", () => {
    expect(srcText).not.toContain("currentBench = data.desired_bench_version");
    expect(srcText).not.toContain("desired_bench_version || currentBench");
  });
});

// ── Row 33 half: the badge names the transition, never a bare "latest" ──────
describe("bench badge copy (row 33)", () => {
  it('never claims "· latest"', () => {
    expect(distText).not.toContain(" · latest");
    expect(srcText).not.toContain('currentBench + " · latest"');
  });
});

// ── Row 34: the removed tie chip never returns ───────────────────────────────
describe("tie labels stay removed (row 34)", () => {
  it("ships neither the tie glyph nor its renderer", () => {
    expect(distText).not.toContain("≈ tie");
    expect(srcText).not.toContain("tieChip");
  });
});

// ── Row 40: the reference baseline stays unpublished ────────────────────────
// Docstring: the stock-harness reference baseline is deliberately
// unpublished — v7 calibration is sharply bimodal (15 of 20 seeds score
// conversational_sanity exactly 0.000, composite 0.185–0.221; 5 clear the
// gate at 0.344–0.450; no mass at the mean 0.248, sd 0.087), so any single
// number describes a run that does not exist.
describe("no reference baseline stat (row 40)", () => {
  it("keeps the baseline card and its framing out of the product", () => {
    for (const banned of ["REFERENCE_BASELINES", 'id="c-baseline"', 'id="c-baseline-tip"']) {
      expect(distText, banned).not.toContain(banned);
      expect(srcText, banned).not.toContain(banned);
    }
    expect(distText).not.toContain("Reference Baseline");
    expect(distText.toLowerCase()).not.toContain("reference baseline");
    expect(distText).not.toContain("must beat");
  });
});
