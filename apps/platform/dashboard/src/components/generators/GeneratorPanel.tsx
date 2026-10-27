// One generator's panel over the generators page (?generator=<id>): who
// made it, how it is doing on the payout ladder, its round-by-round results,
// its lifecycle, and the miner's other versions. Every field is public;
// Platform never serves generator source, round seeds, or case content here.
import { For, Show, createMemo, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { athDate, fx, relTime } from "../../lib/format";
import { entityRoute } from "../../stores/routeStore";
import { CopyButton } from "../shell/CopyButton";
import { EntityButton } from "../ui/EntityButton";
import { HandleBadge } from "../ui/HandleBadge";
import { MinerAvatar } from "../ui/MinerAvatar";
import type {
  GeneratorSubmissionDetail,
  GeneratorSubmissionSummary,
  ProjectedPayout,
} from "./generator-data";
import { generatorLabel } from "./generator-data";
import { GeneratorLink } from "./GeneratorIdentity";
import {
  CaseBar,
  GradingBadge,
  SectionError,
  SubmissionStatusChip,
  hasGradingVersions,
} from "./GeneratorParts";
import { GeneratorRoundChart } from "./GeneratorRoundChart";

const share = (value: number) =>
  new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 }).format(value);

const UNPAID: Record<NonNullable<ProjectedPayout["unpaidReason"]>, string> = {
  ineligible: "Not weight-eligible",
  zero_score: "Score is zero",
  duplicate_hotkey: "Hotkey already paid",
  beyond_paid_ranks: "Below the paid ranks",
};

function kib(bytes: number): string {
  return bytes < 1024 ? bytes + " B" : (bytes / 1024).toFixed(1) + " KiB";
}

function runLabel(summary: GeneratorSubmissionSummary): string {
  const run = summary.latest_run;
  if (!run) return "Not run yet";
  return (run.outcome ?? run.status).replaceAll("_", " ") + " · attempt " + run.attempt;
}

export function GeneratorPanel(props: {
  submissionId: string;
  payout: ProjectedPayout | null;
  generatorShare: number | null;
  onClose: () => void;
}): JSX.Element {
  const detail = useEndpoint<GeneratorSubmissionDetail>(
    () => "/generator/submissions/" + props.submissionId,
  );
  const data = () => (detail.error() ? undefined : detail.data());
  const submission = () => data()?.submission;
  const rounds = () => data()?.rounds ?? [];
  const best = createMemo(() =>
    rounds().reduce<number | null>((min, r) => (min === null || r.rank < min ? r.rank : min), null),
  );
  let closeButton: HTMLButtonElement | undefined;
  const opener = document.activeElement as HTMLElement | null;

  const onKey = (ev: KeyboardEvent) => {
    // The miner profile can open over this panel; its own Escape wins.
    if (ev.key === "Escape" && entityRoute() === null) props.onClose();
  };
  onMount(() => {
    document.addEventListener("keydown", onKey);
    document.body.classList.add("gen-panel-open");
    closeButton?.focus();
  });
  onCleanup(() => {
    document.removeEventListener("keydown", onKey);
    document.body.classList.remove("gen-panel-open");
    if (opener?.isConnected) opener.focus();
  });

  return (
    <>
      <div class="gen-panel-back" onClick={() => props.onClose()} />
      <aside
        class="gen-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gen-panel-title"
        tabindex="-1"
      >
        <button
          type="button"
          class="btn ghost gen-panel-close"
          aria-label="Close generator"
          ref={(el) => {
            closeButton = el;
          }}
          onClick={() => props.onClose()}
        >
          <span aria-hidden="true">✕</span>
        </button>

        <Show when={detail.error() && !data()}>
          <SectionError what="This generator" onRetry={() => detail.refresh()} />
        </Show>
        <Show when={!data() && !detail.error()}>
          <div class="gen-panel-loading" role="status" aria-label="Loading generator">
            <span class="gen-skel gen-skel-title" />
            <span class="gen-skel" />
            <span class="gen-skel gen-skel-block" />
          </div>
        </Show>

        <Show when={submission()}>
          {(sub) => (
            <>
              <header class="gen-panel-head">
                <MinerAvatar url={sub().avatar_url} size="lg" />
                <div class="gen-panel-heading">
                  <h3 id="gen-panel-title">{generatorLabel(sub())}</h3>
                  <div class="gen-panel-chips">
                    <Show when={sub().version != null}>
                      <span class="gen-version">v{sub().version}</span>
                    </Show>
                    <SubmissionStatusChip status={sub().status} qualified={!!sub().qualified_at} />
                    <HandleBadge handle={sub().name_handle} />
                  </div>
                </div>
              </header>
              <p class="gen-panel-miner">
                <span class="gen-panel-label">Miner</span>
                <EntityButton
                  kind="miner"
                  id={sub().miner_hotkey}
                  label={sub().miner_hotkey}
                  class="mono"
                  title="Open the miner profile"
                />
                <CopyButton value={sub().miner_hotkey} label="miner hotkey" />
              </p>
              <Show when={sub().status_reason}>
                <p class="gen-panel-reason">{sub().status_reason}</p>
              </Show>

              <dl class="gen-stats gen-panel-stats">
                <div>
                  <dt>Payout position</dt>
                  <dd>
                    {props.payout
                      ? props.payout.position !== null
                        ? "#" + props.payout.position
                        : UNPAID[props.payout.unpaidReason ?? "zero_score"]
                      : "Not on the board"}
                  </dd>
                </div>
                <div>
                  <dt>Latest round score</dt>
                  <dd>{sub().round_score != null ? fx(sub().round_score as number) : "–"}</dd>
                </div>
                <div>
                  <dt>Share of generator track</dt>
                  <dd>
                    {props.payout && props.payout.position !== null
                      ? share(props.payout.trackShare)
                      : "–"}
                  </dd>
                </div>
                <div>
                  <dt>Share of miner emission</dt>
                  <dd>
                    {props.payout && props.payout.position !== null
                      ? props.generatorShare === null
                        ? "Split not published"
                        : share(props.payout.trackShare * props.generatorShare)
                      : "–"}
                  </dd>
                </div>
                <div>
                  <dt>Rounds scored</dt>
                  <dd>{rounds().length}</dd>
                </div>
                <div>
                  <dt>Best rank</dt>
                  <dd>{best() !== null ? "#" + best() : "–"}</dd>
                </div>
              </dl>

              <section class="gen-panel-section" aria-labelledby="gen-panel-rounds">
                <h4 id="gen-panel-rounds">Score by round</h4>
                <Show
                  when={rounds().length}
                  fallback={
                    <p class="gen-note">
                      No round has scored this generator yet. Qualified generators fill round
                      columns best-first, so a new one waits for a free column.
                    </p>
                  }
                >
                  <GeneratorRoundChart rounds={rounds()} />
                  <div class="gen-table-scroll">
                    <table class="gen-table">
                      <caption class="sr-only">Results in each scored round</caption>
                      <thead>
                        <tr>
                          <th scope="col">Round</th>
                          <th scope="col" class="num">
                            Score
                          </th>
                          <th scope="col" class="num">
                            Rank
                          </th>
                          <th scope="col">Cases</th>
                          <th scope="col" class="num hide-sm">
                            Discriminating
                          </th>
                          <th scope="col" class="hide-sm">
                            Scored
                          </th>
                          <Show when={hasGradingVersions(rounds())}>
                            <th
                              scope="col"
                              class="hide-sm"
                              title="The grading policy this generator's pack declared"
                            >
                              Grading
                            </th>
                          </Show>
                        </tr>
                      </thead>
                      <tbody>
                        <For each={rounds()}>
                          {(round) => (
                            <tr>
                              <td>
                                {round.round_index}
                                <Show when={!round.counted}>
                                  <small title="A challenger column: scored, but not counted toward the round's cases">
                                    {" "}
                                    challenger
                                  </small>
                                </Show>
                              </td>
                              <td class="num">{fx(round.score)}</td>
                              <td class="num">
                                {round.rank}
                                <small> / {round.columns}</small>
                              </td>
                              <td>
                                <CaseBar counts={round} />
                              </td>
                              <td class="num hide-sm">{round.discriminating}</td>
                              <td class="hide-sm">{relTime(round.scored_at)}</td>
                              <Show when={hasGradingVersions(rounds())}>
                                <td class="hide-sm">
                                  <GradingBadge version={round.grading_bench_version} />
                                </td>
                              </Show>
                            </tr>
                          )}
                        </For>
                      </tbody>
                    </table>
                  </div>
                </Show>
              </section>

              <section class="gen-panel-section" aria-labelledby="gen-panel-life">
                <h4 id="gen-panel-life">Lifecycle</h4>
                <dl class="gen-facts">
                  <div>
                    <dt>Submitted</dt>
                    <dd>{athDate(sub().submitted_at)}</dd>
                  </div>
                  <div>
                    <dt>Qualified</dt>
                    <dd>{sub().qualified_at ? athDate(sub().qualified_at) : "Not yet"}</dd>
                  </div>
                  <div>
                    <dt>Latest qualification run</dt>
                    <dd>{runLabel(sub())}</dd>
                  </div>
                  <div>
                    <dt>Source</dt>
                    <dd>
                      {sub().file_count} files · {kib(sub().size_bytes)} · Go {sub().go_version}
                    </dd>
                  </div>
                  <div>
                    <dt>Module</dt>
                    <dd class="mono">{sub().module}</dd>
                  </div>
                  <div>
                    <dt>Archive SHA-256</dt>
                    <dd class="mono gen-hash">
                      <span title={sub().source_sha256}>{sub().source_sha256.slice(0, 16)}…</span>
                      <CopyButton value={sub().source_sha256} label="archive SHA-256" />
                    </dd>
                  </div>
                </dl>
                <p class="gen-note">
                  Generator source stays private to Platform and the trusted scorer.
                </p>
              </section>

              <Show when={(data()?.versions.length ?? 0) > 1}>
                <section class="gen-panel-section" aria-labelledby="gen-panel-versions">
                  <h4 id="gen-panel-versions">This miner&apos;s generators</h4>
                  <ol class="gen-versions">
                    <For each={data()?.versions ?? []}>
                      {(version) => (
                        <li classList={{ current: version.submission_id === sub().submission_id }}>
                          <span class="gen-version">v{version.version ?? "?"}</span>
                          <Show
                            when={version.submission_id !== sub().submission_id}
                            fallback={<strong>{generatorLabel(version)}</strong>}
                          >
                            <GeneratorLink submissionId={version.submission_id} class="gen-name">
                              {generatorLabel(version)}
                            </GeneratorLink>
                          </Show>
                          <SubmissionStatusChip
                            status={version.status}
                            qualified={!!version.qualified_at}
                          />
                          <span class="gen-versions-meta">
                            {version.round_score != null
                              ? "last round " + fx(version.round_score)
                              : "no round yet"}{" "}
                            · uploaded {relTime(version.submitted_at)}
                          </span>
                        </li>
                      )}
                    </For>
                  </ol>
                </section>
              </Show>
            </>
          )}
        </Show>
      </aside>
    </>
  );
}
