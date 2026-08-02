// The benchmark page (monolith markup 2940–3008 + renderBenchDocs 9535–9606,
// loadBenchConfig 9608–9636, applyBenchVersion 9740–9765). Everything a miner
// could read as a consensus rule — champion share, tail size, hysteresis
// margin, dethrone z, quorum threshold, the benchmark version itself — is
// filled from API payloads; the static markup carries neutral placeholders
// only, so a stale literal is never shown. The site footer renders inside
// this page's section, exactly where the monolith mounts it.
import { createMemo } from "solid-js";
import type { JSX } from "solid-js";

import {
  GlossaryDisplay,
  VersionHistory,
  VersionParagraph,
} from "../components/benchmark/Reference";
import { changelogItems } from "../components/benchmark/docs";
import { SiteFooter } from "../components/shell/Sidebar";
import { useEndpoint } from "../data/useEndpoint";
import { REFRESH_MS } from "../lib/config";
import type { ResourceState } from "../data/useEndpoint";
import { benchmarkDisplayVersion } from "../lib/bench-state";
import { API_BASE } from "../lib/config";
import { marginText, num, pct } from "../lib/format";
import { emissionsSplit, rolloutQuorum } from "../lib/scoring";
import type { BenchConfigPayload, GlossaryPayload } from "../types/bench";
import type { LeaderboardPayload, RolloutState } from "../types/leaderboard";

/** Errored resources read as absent — stated absence, never stale-as-fresh. */
function latest<T>(resource: ResourceState<T>): T | undefined {
  if (resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

export function BenchmarkPage(): JSX.Element {
  // The explainer numbers come from the same payloads the leaderboard ranks
  // by: the emissions fold from /public/leaderboard, the quorum threshold
  // from /public/bench/rollout, the frozen setup from /public/bench/config,
  // and the changelog/glossary from /public/bench/glossary.
  const leaderboard = useEndpoint<LeaderboardPayload>("/public/leaderboard", {
    pollMs: REFRESH_MS,
  });
  const rollout = useEndpoint<RolloutState>("/public/bench/rollout", { pollMs: REFRESH_MS });
  // bench/config is effectively static (max-age 300) and the glossary is
  // fetched once, ever — neither polls (monolith loadGlossary 9513–9529).
  const benchConfig = useEndpoint<BenchConfigPayload>("/public/bench/config");
  const glossary = useEndpoint<GlossaryPayload>("/public/bench/glossary");

  const lb = () => latest(leaderboard);
  const config = () => latest(benchConfig);

  // Version authority (loadOperations 9437–9440 + applyBenchVersion): active
  // wins, current is the fallback, and the rollout target never overwrites
  // either. The config version is only adopted when the leaderboard could
  // not resolve one (loadBenchConfig 9616–9622).
  const activeBench = createMemo(
    () => Number(lb()?.active_bench_version) || Number(latest(rollout)?.active_version) || null,
  );
  const currentBench = createMemo(
    () => Number(lb()?.current_bench_version) || Number(config()?.bench_version) || null,
  );
  const displayVersion = createMemo(() => benchmarkDisplayVersion(activeBench(), currentBench()));
  const desiredBench = createMemo(
    () =>
      Number(lb()?.desired_bench_version) ||
      Number(latest(rollout)?.desired_version) ||
      displayVersion(),
  );
  const rolloutStatus = () => latest(rollout)?.status ?? null;

  const split = createMemo(() => emissionsSplit(lb()?.emissions));
  const quorumNeeded = createMemo(() => {
    const needed = rolloutQuorum(latest(rollout)).needed;
    return Number.isFinite(needed as number) ? String(needed) : null;
  });

  const changelog = createMemo(() =>
    changelogItems(
      latest(glossary)?.versions ?? [],
      displayVersion(),
      desiredBench(),
      rolloutStatus(),
    ),
  );
  // Version-specific description: the changelog's own entry for the live
  // version (renderBenchDocs 9539–9548), or null for the tiered fallbacks.
  const versionEntry = createMemo(() => {
    const cur = displayVersion() || desiredBench() || currentBench();
    return (latest(glossary)?.versions ?? []).find((v) => v.version === Number(cur)) ?? null;
  });

  const thinking = () => {
    const c = config();
    if (!c) return "off";
    return c.harness.reasoning_effort || (c.harness.thinking ? "on" : "off");
  };
  const mirrorTemplate = () => config()?.public_mirror_url_template || null;

  return (
    <section class="page active" data-page="benchmark">
      <section class="about">
        <p class="lead">
          A seeded, procedurally generated benchmark for agentic-memory harnesses on Subnet 118.
          Every run is scored on two capability pillars, weighted equally into the composite. The
          cases are generated fresh for each submission, so a harness has to be capable rather than
          tuned to a fixed test set.
        </p>
        <div class="pillars">
          <div class="pillar mem">
            <h2>
              Agentic memory{" "}
              <span
                class="tag tip"
                tabindex="0"
                data-tooltip="Memory cases contribute half of the unadjusted composite. Quality gates and token efficiency can then reduce the final score."
              >
                50%
              </span>
            </h2>
            <p>
              Can the harness store what matters and get it back when it counts? Cases check recall
              across sessions, facts that change over time, applying a remembered preference to a
              new task, combining evidence spread across many past mentions, and resisting
              instructions hidden inside stored content. The harder categories are written to defeat
              keyword matching. Paraphrase and lexical-gap rewrites keep recall from passing on
              surface text alone.
            </p>
          </div>
          <div class="pillar tool">
            <h2>
              Tool-use judgment{" "}
              <span
                class="tag tip"
                tabindex="0"
                data-tooltip="Tool-use cases contribute the other half of the unadjusted composite and are scored from validator-observed actions."
              >
                50%
              </span>
            </h2>
            <p>
              Does the agent reach for the right action at the right time? Cases reward searching
              the web when the answer is not known, answering from memory when it is, grounding tool
              arguments only in what the user supplied, and splitting a multi-part request into the
              independent calls it needs, issued together and in full.
            </p>
          </div>
        </div>

        <div class="benchmark-reference" aria-label="Benchmark reference material">
          <details class="bench-disclosure" id="scoring-explainer">
            <summary>
              <span class="bench-disclosure-title">
                <strong>How a score becomes emissions</strong>
                <span>Scoring, eligibility, versioning, and KOTH rules</span>
              </span>
              <span class="bench-disclosure-icon" aria-hidden="true" />
            </summary>
            <div class="bench-disclosure-body">
              <ul>
                <li>
                  <b>What a score is.</b> The <b>composite</b> starts at{" "}
                  <code>0.5 × tool mean + 0.5 × memory mean</code>. DittoBench then applies
                  benchmark quality gates for wasteful tool use, consistency, unnecessary
                  memory-side actions, canary integrity, and conversational sanity. Bench v6 and
                  older retain their signed legacy token penalty, which can remove{" "}
                  <b>at most 10%</b>. The row detail shows that historical arithmetic without
                  applying it to newer benches.
                </li>
                <li>
                  <b>How the current token-efficiency bonus works.</b> On Bench v7 and newer,
                  audited relative efficiency is strictly upside. The platform first computes the
                  continual score from the original quorum and every retained retest sample, then
                  applies the frozen cohort bonus. The leaderboard shows the pre-bonus score, bonus
                  percentage, and final folded score separately.
                </li>
                <li>
                  <b>Which runs rank.</b> Only runs that administer the <b>full</b> benchmark
                  profile rank or earn emissions. Smaller practice profiles omit the hardest memory
                  categories and their few remaining cases are easy to ace, so they are published as{" "}
                  <b>provisional</b> and are never folded into weights. A run also needs the full
                  3-of-3 validator quorum before it is final; below that it is feedback, and its
                  composite can still move.
                </li>
                <li>
                  <b>Scores compare only within one benchmark version.</b> A{" "}
                  <code>bench_version</code> is an immutable generation contract: for a given seed
                  it emits the same bytes forever and grades them the same way forever, which is
                  what makes an old score auditable by anyone holding the seed. A correction to
                  grading therefore cannot be applied to an existing version. It ships as a new one.
                  Two composites from different versions are two different measurements, and the
                  board never ranks them against each other.
                </li>
                <li>
                  <b>
                    What v
                    <span class="bv-desired">
                      {displayVersion() ? String(displayVersion()) : "–"}
                    </span>{" "}
                    is.
                  </b>{" "}
                  <VersionParagraph entry={versionEntry()} displayVersion={displayVersion()} />
                </li>
                <li>
                  <b>How emissions work.</b> The KOTH fold pays a champion and a ranked tail: the
                  champion takes{" "}
                  <span id="ex-champion-share">
                    {split() ? pct(split()!.championShare) : "a fixed share"}
                  </span>{" "}
                  of the miner pool, and up to{" "}
                  <span id="ex-tail-size">
                    {split()?.tailSize != null ? String(split()!.tailSize) : "a fixed number of"}
                  </span>{" "}
                  runners-up by composite receive descending shares of{" "}
                  <span id="ex-tail-share">
                    {split() ? pct(split()!.tailShare) : "the remainder"}
                  </span>
                  . Only finalized, full-benchmark, currently-registered entries with a positive
                  composite are eligible. The live values are shown on the leaderboard's emissions
                  strip.
                </li>
                <li>
                  <b>How the crown changes hands.</b> The fold runs in <b>first-seen</b> order, so
                  the incumbent keeps the crown unless a challenger clearly beats it. Being
                  marginally ahead on one dataset is not enough. A challenger dethrones only if its
                  lead exceeds the <b>indifference band</b>, which starts as the larger of the fixed{" "}
                  <span id="ex-margin">
                    {split()?.margin != null
                      ? marginText(split()!.margin) + " hysteresis"
                      : "composite-point hysteresis"}
                  </span>{" "}
                  and the statistical band{" "}
                  <code>
                    <span id="ex-dethrone-z">
                      {split()?.dethroneZ != null
                        ? num(split()!.dethroneZ as number)
                        : "dethrone_z"}
                    </span>{" "}
                    × √(SE<sub>challenger</sub>² + SE<sub>champion</sub>²)
                  </code>{" "}
                  when both sides carry a standard error. From Bench v6 onward, the whole band
                  shrinks smoothly above a 0.60 incumbent score so the crown remains contestable
                  near the benchmark ceiling. A lead inside the band is not thrown away: both agents
                  are re-scored on <b>shared confirmation seeds</b> and the next fold decides on the
                  paired comparison, where per-dataset difficulty cancels. Dataset luck never
                  decides emissions.
                </li>
                <li>
                  <b>When weights move to a new version.</b> The whole ledger sits on one version at
                  a time. Weights stay on the <b>active</b> version until at least{" "}
                  <span class="quorum-needed">{quorumNeeded() ?? "the required number of"}</span>{" "}
                  agents hold a complete ranked quorum at the version being rolled out, and then the
                  entire ledger flips at once. Ranking a partly-migrated pool would mix two
                  incomparable scales; flipping early would leave the emission set (champion plus
                  tail) short of recipients. Live progress is on the leaderboard's rollout strip.
                </li>
              </ul>
            </div>
          </details>
          <details class="bench-disclosure" id="bench-setup">
            <summary>
              <span class="bench-disclosure-title">
                <strong>Frozen setup</strong>
                <span>Model pinning, deterministic grading, and audit trail</span>
              </span>
              <span class="tag" id="bs-version">
                {config() ? "v" + config()!.bench_version : "v–"}
              </span>
              <span class="bench-disclosure-icon" aria-hidden="true" />
            </summary>
            <div class="bench-disclosure-body">
              <p style="margin-bottom:8px">
                Every harness runs against <b>one frozen open-weight model</b> and every run is
                graded by <b>deterministic, judge-free rules</b>. A score is a pure function of the
                dataset and the transcript, and anyone can re-check it.
              </p>
              <ul>
                <li>
                  <b>Model</b>:{" "}
                  <code id="bs-model">{config()?.harness.canonical_id ?? "qwen/qwen3-32b"}</code>{" "}
                  served through{" "}
                  <code id="bs-serving">{config()?.harness.serving ?? "Qwen/Qwen3-32B-TEE"}</code>,
                  reasoning mode locked <b id="bs-thinking">{thinking()}</b>. The trusted gateway
                  forces the active benchmark model and only its eligible serving route; sandbox
                  egress is deny-all, so no other model is reachable.
                </li>
                <li>
                  <b>Grading</b>: no LLM judge. Deterministic per-kind checks from the public{" "}
                  <a
                    href="https://github.com/ditto-assistant/dittobench-datagen"
                    target="_blank"
                    rel="noopener"
                  >
                    dittobench-datagen
                  </a>{" "}
                  module (also the generator: dataset + answer keys are byte-reproducible from the
                  seed).
                </li>
                <li>
                  <b>Seed</b>: derived from an on-chain block hash fixed <i>after</i> the miner
                  commits. The result is unpredictable: one fresh dataset per submission, pinned by{" "}
                  <code>dataset_sha256</code> in every score.
                </li>
                <li>
                  <b>Observed execution</b>: tool cases are scored on the trajectory the validator
                  watched through its mock tool endpoint. The harness's own self-report is never
                  used. Every scored run opens with a reachability probe; if the harness cannot
                  reach the endpoint, the run <b>fails and is retried</b>, never scored as zeros.
                </li>
                <li>
                  <b>Transcript</b>: each scored run publishes its graded inputs (every response and
                  observed tool call) content-addressed as <code>transcript_sha256</code>, and that
                  digest is bound <i>inside</i> the validator's score signature. Dataset +
                  transcript + public grader reproduce any score offline, with no trust in the
                  platform or validators required.
                </li>
                <li>
                  <b>Crown contests</b>: a challenger inside the dethrone band is re-scored,
                  together with the champion, on shared confirmation seeds; the crown then moves or
                  holds on the paired comparison, so dataset luck can never decide emissions.
                </li>
                <li id="bs-mirror-li" style={{ display: mirrorTemplate() ? undefined : "none" }}>
                  <b>Verify it yourself</b>: finalized run records (dataset pin + all signed scores)
                  are public. View the{" "}
                  <a
                    id="bs-mirror"
                    href={mirrorTemplate() ? mirrorTemplate()!.replace("{agent_id}", "") : "#"}
                    target="_blank"
                    rel="noopener"
                  >
                    run record mirror ↗
                  </a>{" "}
                  and signed{" "}
                  <a
                    id="bs-ledger"
                    href={
                      mirrorTemplate() && config()?.ledger_path
                        ? API_BASE + (config()!.ledger_path as string).replace("/api/v1", "")
                        : "#"
                    }
                    target="_blank"
                    rel="noopener"
                  >
                    score ledger ↗
                  </a>
                  .
                </li>
              </ul>
            </div>
          </details>
          <details class="bench-disclosure" id="bench-versions">
            <summary>
              <span class="bench-disclosure-title">
                <strong>Version history</strong>
                <span>What changed in each immutable benchmark contract</span>
              </span>
              <span class="bench-disclosure-icon" aria-hidden="true" />
            </summary>
            <div class="bench-disclosure-body">
              <p style="margin-bottom:10px">
                Each <code>bench_version</code> is an immutable contract; a scoring change ships as
                a new version rather than editing an old one. Newest first; the active version is
                highlighted, and an open rollout target is labeled separately.
              </p>
              <VersionHistory items={changelog()} />
            </div>
          </details>
          <details class="bench-disclosure" id="bench-glossary">
            <summary>
              <span class="bench-disclosure-title">
                <strong>Glossary</strong>
                <span>Metrics, gate factors, and scored test categories</span>
              </span>
              <span class="bench-disclosure-icon" aria-hidden="true" />
            </summary>
            <div class="bench-disclosure-body">
              <p style="margin-bottom:10px">
                What every score metric and gate factor means, and what each scored test category
                probes. Definitions come from the public{" "}
                <a
                  href="https://github.com/ditto-assistant/dittobench-datagen"
                  target="_blank"
                  rel="noopener"
                >
                  grader
                </a>
                , never an answer key.
              </p>
              <GlossaryDisplay
                categories={latest(glossary)?.categories ?? []}
                metrics={latest(glossary)?.metrics ?? []}
              />
            </div>
          </details>
        </div>
      </section>

      <SiteFooter />
    </section>
  );
}
