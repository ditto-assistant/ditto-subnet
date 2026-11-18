// Context models (/context-models): the model_context (CPM) competition,
// contract docs/research/cpm/competition-v1.md. Miners fine-tune a small
// open-weight model that builds Ditto's context packet; each season the CPM
// scorer runs every active model on three public seeds and Platform ranks
// them. The page reads only public endpoints: the board, the seasons and the
// upload terms. Rank, score and eligibility are Platform's, shown as served.
//
// Each section loads, fails and retries on its own, and a failed refresh keeps
// the last good data on screen. With the lane off the page says so on purpose
// instead of rendering empty tables.
import { For, Show, createMemo } from "solid-js";
import type { Accessor, JSX } from "solid-js";

import type { ResourceState } from "../data/useEndpoint";
import { useEndpoint } from "../data/useEndpoint";
import { REFRESH_MS } from "../lib/config";
import { fx, monoDisplay, relTime, relTimeUntil, shortKey } from "../lib/format";
import { CopyButton } from "../components/shell/CopyButton";
import { EntityButton } from "../components/ui/EntityButton";
import { StatusChip } from "../components/ui/StatusChip";
import { SectionError, SkeletonRows } from "../components/generators/GeneratorParts";
import { cooldownText } from "../components/generators/generator-data";
import type {
  ContextModelBoard,
  ContextModelBoardEntry,
  ContextModelMode,
  ContextModelSeason,
  ContextModelSeasonList,
  ContextModelUploadTerms,
} from "../components/context-models/context-model-data";
import {
  SCORE_COMPONENTS,
  eligibilityBar,
  exactSeeds,
  hfRevisionUrl,
  paramsText,
  sizeText,
} from "../components/context-models/context-model-data";

const CONTRACT_URL =
  "https://github.com/ditto-assistant/ditto-subnet/blob/main/docs/research/cpm/competition-v1.md";

const MODE_CHIP: Record<ContextModelMode, readonly [string, string]> = {
  enforce: ["Enforce", "good"],
  shadow: ["Shadow · unpaid", "paused"],
  off: ["Off", "unknown"],
};

const blockText = (block: number) => "#" + block.toLocaleString("en-US");

/** The newest successful value, kept through a failed refresh. */
function lastGood<T>(resource: ResourceState<T>): Accessor<T | undefined> {
  let last: T | undefined;
  return createMemo(() => {
    if (!resource.error()) {
      const value = resource.data();
      if (value !== undefined) last = value;
    }
    return last;
  });
}

function ModeChip(props: { mode: ContextModelMode }): JSX.Element {
  const chip = () => MODE_CHIP[props.mode] ?? [props.mode, ""];
  return <StatusChip label={chip()[0]} tone={chip()[1]} />;
}

/** The lane is off: say what the page will show and why it is empty now. */
function OffNotice(): JSX.Element {
  return (
    <section class="gen-section cm-off" aria-labelledby="cm-off-title">
      <h3 id="cm-off-title">Not open yet</h3>
      <p>
        The competition is off. Platform accepts no submissions, opens no season, and serves an
        empty board. When operators move it to shadow, the first season opens on the next finalized
        block, and its seeds, base floors and board appear here.
      </p>
      <p class="gen-note">
        The allowlisted bases and upload limits below are already fixed, so a model can be trained
        against them now.
      </p>
    </section>
  );
}

function SeasonSection(props: {
  season: ContextModelSeason | null | undefined;
  floorMargin: number | undefined;
  loading: boolean;
  failed: boolean;
  onRetry: () => void;
}): JSX.Element {
  const seeds = () => (props.season ? exactSeeds(props.season) : null);
  const floors = () =>
    Object.entries(props.season?.base_floors ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <section class="gen-section" aria-labelledby="cm-season-title">
      <div class="gen-section-title">
        <h3 id="cm-season-title">
          {props.season ? "Season " + props.season.season : "Current season"}
        </h3>
        <Show when={props.season}>
          {(season) => (
            <span>
              Opened {relTime(season().opened_at)} · closes {relTimeUntil(season().closes_at)}
            </span>
          )}
        </Show>
      </div>
      <Show when={props.failed}>
        <SectionError what="Season" stale={!!props.season} onRetry={props.onRetry} />
      </Show>
      <Show
        when={props.season}
        fallback={
          <Show
            when={props.loading}
            fallback={
              <Show when={!props.failed}>
                <p class="gen-note">
                  No season is open yet. The season loop opens one on the next finalized block, and
                  every active model is queued on its seeds.
                </p>
              </Show>
            }
          >
            <dl class="gen-stats" role="status" aria-label="Loading the season">
              <For each={["Open block", "Bench version", "GPU", "Floor margin"]}>
                {(label) => (
                  <div>
                    <dt>{label}</dt>
                    <dd>
                      <span class="gen-skel gen-skel-stat" />
                    </dd>
                  </div>
                )}
              </For>
            </dl>
          </Show>
        }
      >
        {(season) => (
          <>
            <dl class="gen-stats">
              <div>
                <dt>Open block</dt>
                <dd>{blockText(season().open_block)}</dd>
              </div>
              <div>
                <dt>Block hash</dt>
                <dd class="mono cm-mono" title={season().open_block_hash}>
                  {monoDisplay(season().open_block_hash)}
                  <CopyButton value={season().open_block_hash} label="season block hash" />
                </dd>
              </div>
              <div>
                <dt>Bench version</dt>
                <dd>v{season().bench_version}</dd>
              </div>
              <div>
                <dt>Score version</dt>
                <dd>v{season().cpm_score_version}</dd>
              </div>
              <div>
                <dt>GPU</dt>
                <dd>{season().gpu_sku ?? <small>Set by the first result</small>}</dd>
              </div>
              <Show when={props.floorMargin !== undefined}>
                <div>
                  <dt>Floor margin</dt>
                  <dd>+{props.floorMargin}</dd>
                </div>
              </Show>
            </dl>
            <h4 class="cm-subhead">Seeds</h4>
            <Show
              when={seeds()}
              fallback={
                <p class="gen-note">
                  This Platform serves the seeds only as numbers a browser rounds, so they are not
                  shown. Each one is derived from the block hash above.
                </p>
              }
            >
              {(values) => (
                <ol class="cm-seeds">
                  <For each={values()}>
                    {(seed, index) => (
                      <li>
                        <span class="cm-seed-k">k={index()}</span>
                        <code class="mono">{seed}</code>
                        <CopyButton value={seed} label={"season seed " + index()} />
                      </li>
                    )}
                  </For>
                </ol>
              )}
            </Show>
            <p class="gen-note">
              Seed k is the first 8 bytes of sha256(&quot;dcpm-season:{season().season}:k:
              {"{block hash}"}&quot;), read big-endian, so nobody chooses them, Platform included.
            </p>
            <div class="gen-table-scroll">
              <table class="gen-table cm-floors">
                <caption>Base floors this season</caption>
                <thead>
                  <tr>
                    <th scope="col">Base</th>
                    <th scope="col" class="num">
                      Unmodified base score
                    </th>
                    <th scope="col" class="num">
                      Eligible at
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <Show
                    when={floors().length}
                    fallback={
                      <tr>
                        <td colspan={3} class="empty">
                          <div class="empty-msg">
                            The reference runs that set each base&apos;s floor have not reported
                            yet. They run before any model.
                          </div>
                        </td>
                      </tr>
                    }
                  >
                    <For each={floors()}>
                      {([base, floor]) => (
                        <tr>
                          <td class="mono">{base}</td>
                          <td class="num">{fx(floor)}</td>
                          <td class="num">
                            {(() => {
                              const bar = eligibilityBar(floor, props.floorMargin ?? 0);
                              return bar === null ? "–" : fx(bar);
                            })()}
                          </td>
                        </tr>
                      )}
                    </For>
                  </Show>
                </tbody>
              </table>
            </div>
          </>
        )}
      </Show>
    </section>
  );
}

function Components(props: { entry: ContextModelBoardEntry }): JSX.Element {
  const parts = () =>
    SCORE_COMPONENTS.filter(([key]) => typeof props.entry.components[key] === "number");
  return (
    <span class="cm-components">
      <For each={parts()}>
        {([key, label]) => (
          <span title={label}>
            <small>{label.split(" ")[0]}</small> {fx(props.entry.components[key] as number)}
          </span>
        )}
      </For>
    </span>
  );
}

function BoardSection(props: {
  board: ContextModelBoard | undefined;
  loading: boolean;
  failed: boolean;
  onRetry: () => void;
}): JSX.Element {
  return (
    <section class="gen-section" aria-labelledby="cm-board-title">
      <div class="gen-section-title">
        <h3 id="cm-board-title">Board</h3>
        <Show when={props.board}>
          {(board) => (
            <span>
              {board().season != null ? "Season " + board().season + " · " : ""}updated{" "}
              {relTime(board().generated_at)}
            </span>
          )}
        </Show>
      </div>
      <Show when={props.failed}>
        <SectionError what="Board" stale={!!props.board} onRetry={props.onRetry} />
      </Show>
      <Show when={props.board || props.loading}>
        <div class="gen-table-scroll">
          <table class="gen-table" aria-label="Context model board">
            <thead>
              <tr>
                <th scope="col">Rank</th>
                <th scope="col">Miner</th>
                <th scope="col">Model</th>
                <th scope="col" class="num">
                  Score
                </th>
                <th scope="col" class="hide-sm">
                  Components
                </th>
                <th scope="col">Eligibility</th>
                <th
                  scope="col"
                  class="num hide-md"
                  title="The block of the miner's on-chain commitment; earlier wins a tie"
                >
                  Commitment block
                </th>
              </tr>
            </thead>
            <tbody>
              <Show
                when={props.board}
                fallback={<SkeletonRows rows={3} cols={7} label="Loading the board" />}
              >
                <Show
                  when={props.board?.entries.length}
                  fallback={
                    <tr>
                      <td colspan={7} class="empty">
                        <div class="empty-msg">
                          {props.board?.season == null
                            ? "No season is open, so nothing is ranked yet."
                            : "No model has a scored run this season yet. Reference runs score first, then every queued model."}
                        </div>
                      </td>
                    </tr>
                  }
                >
                  <For each={props.board?.entries ?? []}>
                    {(entry) => (
                      <tr classList={{ unpaid: !entry.eligible }}>
                        <td class="gen-pos">#{entry.rank}</td>
                        <td>
                          <EntityButton
                            kind="miner"
                            id={entry.hotkey}
                            label={shortKey(entry.hotkey)}
                            class="gen-miner mono"
                            title="Open the miner profile"
                          />
                        </td>
                        <td>
                          <span class="cm-model">
                            <a
                              href={hfRevisionUrl(entry.hf_repo, entry.revision)}
                              target="_blank"
                              rel="noopener noreferrer"
                              title={
                                entry.hf_repo +
                                " at " +
                                entry.revision +
                                " (the revision Platform pinned)"
                              }
                            >
                              {entry.hf_repo}
                            </a>
                            <small>
                              <span class="mono">@{entry.revision.slice(0, 7)}</span> ·{" "}
                              {entry.kind === "lora" ? "LoRA" : "Full"} on {entry.base_id}
                              <Show
                                when={
                                  entry.model_url !== hfRevisionUrl(entry.hf_repo, entry.revision)
                                }
                              >
                                {" · "}
                                <a href={entry.model_url} target="_blank" rel="noopener noreferrer">
                                  mirror
                                </a>
                              </Show>
                            </small>
                          </span>
                        </td>
                        <td class="num">{fx(entry.score)}</td>
                        <td class="hide-sm">
                          <Components entry={entry} />
                        </td>
                        <td>
                          <StatusChip
                            label={entry.eligible ? "Eligible" : "Below floor"}
                            tone={entry.eligible ? "good" : "warn"}
                            title={
                              entry.floor == null
                                ? "No floor for " + entry.base_id + " yet this season"
                                : "Needs " +
                                  fx(entry.floor + (props.board?.floor_margin ?? 0)) +
                                  ": the " +
                                  entry.base_id +
                                  " floor " +
                                  fx(entry.floor) +
                                  " plus the margin"
                            }
                          />
                        </td>
                        <td class="num hide-md">
                          {entry.commitment_block.toLocaleString("en-US")}
                        </td>
                      </tr>
                    )}
                  </For>
                </Show>
              </Show>
            </tbody>
          </table>
        </div>
      </Show>
      <p class="gen-note">
        Ranked by score, then by commitment block, earlier first. A model is eligible when it beats
        its base&apos;s unmodified score this season by the floor margin.
        <Show when={props.board && props.board.mode !== "off"}>
          {" "}
          Validators pay this track nothing yet: it scores in {props.board?.mode} until a validator
          release pays it.
        </Show>
      </p>
    </section>
  );
}

function SeasonHistory(props: { list: ContextModelSeasonList }): JSX.Element {
  return (
    <section class="gen-section" aria-labelledby="cm-history-title">
      <div class="gen-section-title">
        <h3 id="cm-history-title">Recent seasons</h3>
        <span>Seeds and floors are fixed when a season opens</span>
      </div>
      <div class="gen-table-scroll">
        <table class="gen-table">
          <thead>
            <tr>
              <th scope="col">Season</th>
              <th scope="col">Status</th>
              <th scope="col">Opened</th>
              <th scope="col" class="num hide-sm">
                Open block
              </th>
              <th scope="col" class="hide-sm">
                Bench
              </th>
              <th scope="col" class="hide-md">
                Floors
              </th>
            </tr>
          </thead>
          <tbody>
            <For each={props.list.seasons}>
              {(season) => (
                <tr classList={{ shown: season.season === props.list.current }}>
                  <td>{season.season}</td>
                  <td>
                    <StatusChip
                      label={season.closed_at ? "Closed" : "Open"}
                      tone={season.closed_at ? "" : "progress"}
                    />
                  </td>
                  <td>{relTime(season.opened_at)}</td>
                  <td class="num hide-sm">{blockText(season.open_block)}</td>
                  <td class="hide-sm">v{season.bench_version}</td>
                  <td class="hide-md">
                    <small>
                      {Object.entries(season.base_floors)
                        .sort(([a], [b]) => a.localeCompare(b))
                        .map(([base, floor]) => base + " " + fx(floor))
                        .join(" · ") || "Pending"}
                    </small>
                  </td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TermsSection(props: {
  terms: ContextModelUploadTerms | undefined;
  failed: boolean;
  onRetry: () => void;
}): JSX.Element {
  return (
    <section class="gen-section" aria-labelledby="cm-terms-title">
      <div class="gen-section-title">
        <h3 id="cm-terms-title">Submitting a model</h3>
      </div>
      <Show when={props.failed && !props.terms}>
        <SectionError what="Upload terms" onRetry={props.onRetry} />
      </Show>
      <Show when={props.terms}>
        {(terms) => (
          <>
            <dl class="gen-stats">
              <div>
                <dt>Uploads</dt>
                <dd>{terms().accepting ? "Open" : "Closed"}</dd>
              </div>
              <div>
                <dt>Upload fee</dt>
                <dd>{terms().payment_required ? "Required" : "None"}</dd>
              </div>
              <div>
                <dt>Cooldown per hotkey</dt>
                <dd>{cooldownText(terms().upload_cooldown_seconds)}</dd>
              </div>
              <div>
                <dt>Max LoRA rank</dt>
                <dd>{terms().max_lora_rank}</dd>
              </div>
              <div>
                <dt>Max size</dt>
                <dd>{sizeText(terms().max_total_bytes)}</dd>
              </div>
              <div>
                <dt>Licenses</dt>
                <dd>{terms().licenses.join(", ")}</dd>
              </div>
            </dl>
            <Show when={terms().blockers.length}>
              <ul class="gen-note">
                <For each={terms().blockers}>{(blocker) => <li>{blocker}</li>}</For>
              </ul>
            </Show>
            <div class="gen-table-scroll">
              <table class="gen-table">
                <caption>Allowlisted bases</caption>
                <thead>
                  <tr>
                    <th scope="col">Base</th>
                    <th scope="col">Hugging Face revision</th>
                    <th scope="col" class="num">
                      Parameters
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <For each={terms().bases}>
                    {(base) => (
                      <tr>
                        <td class="mono">{base.base_id}</td>
                        <td>
                          <a
                            href={hfRevisionUrl(base.hf_repo, base.revision)}
                            target="_blank"
                            rel="noopener noreferrer"
                            title={base.hf_repo + " at " + base.revision}
                          >
                            {base.hf_repo}
                          </a>{" "}
                          <small class="mono">@{base.revision.slice(0, 7)}</small>
                        </td>
                        <td class="num">{paramsText(base.parameters)}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </div>
            <p class="gen-note">
              Push the model and <code>{terms().manifest_filename}</code> to a public Hugging Face
              repo, commit <code>{terms().commitment_format}</code> on chain (that block is your
              priority), then submit the signed payload <code>{terms().signature_payload}</code>{" "}
              naming the exact revision. Weights are public from submission.
            </p>
          </>
        )}
      </Show>
    </section>
  );
}

export function ContextModelsPage(): JSX.Element {
  const board = useEndpoint<ContextModelBoard>("/model-context/board", { pollMs: REFRESH_MS });
  const seasons = useEndpoint<ContextModelSeasonList>("/model-context/seasons", {
    pollMs: REFRESH_MS,
  });
  const terms = useEndpoint<ContextModelUploadTerms>("/model-context/upload-terms");

  const boardData = lastGood(board);
  const seasonList = lastGood(seasons);
  const termsData = lastGood(terms);

  const mode = (): ContextModelMode | undefined => boardData()?.mode ?? termsData()?.mode;
  const off = () => mode() === "off";
  const current = () => {
    const list = seasonList();
    if (!list || list.current == null) return null;
    return list.seasons.find((season) => season.season === list.current) ?? null;
  };

  return (
    <section class="page active context-models-page" data-page="context-models">
      <header class="gen-intro">
        <Show when={mode()}>
          {(value) => (
            <p class="gen-mode">
              Mode <ModeChip mode={value()} />
            </p>
          )}
        </Show>
        <p>
          Miners fine-tune a small open-weight model, a LoRA or full fine-tune of an allowlisted
          Qwen3.5 base, that builds Ditto&apos;s context packet: which memories and tools a reader
          model sees. Each season the scorer runs every active model on three public seeds and
          measures memory recall, distractor rate and tool recall. Weights are public from
          submission.{" "}
          <a href={CONTRACT_URL} target="_blank" rel="noopener">
            Competition contract
          </a>
        </p>
      </header>

      <Show when={!off() || current()} fallback={<OffNotice />}>
        <SeasonSection
          season={current()}
          floorMargin={boardData()?.floor_margin}
          loading={seasons.loading() && !seasonList()}
          failed={!!seasons.error()}
          onRetry={() => seasons.refresh()}
        />
        <BoardSection
          board={boardData()}
          loading={board.loading()}
          failed={!!board.error()}
          onRetry={() => board.refresh()}
        />
        <Show when={(seasonList()?.seasons.length ?? 0) > 1}>
          <SeasonHistory list={seasonList() as ContextModelSeasonList} />
        </Show>
      </Show>

      <TermsSection terms={termsData()} failed={!!terms.error()} onRetry={() => terms.refresh()} />
    </section>
  );
}
