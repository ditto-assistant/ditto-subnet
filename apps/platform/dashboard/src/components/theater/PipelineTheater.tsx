// The pipeline theater: the /pipeline page told as one story in three acts.
// Build is a factory that assembles each submission into its Paper Ditto,
// Screening is airport security, and Scoring is the arena where Dittos face
// the data generators. Every act is drawn from the same single
// /public/operations snapshot the pipeline board below reads (plus the
// screener feed for live stages and the public generator round for the
// arena), and every act carries its authoritative numbers, its cast as
// linked text, and a plain-language summary.
//
// Motion: CSS and a small replay clock, paused while the theater is off
// screen, while the tab is hidden, when the viewer pauses it, and entirely
// under prefers-reduced-motion (each act then shows a still tableau).
import { For, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { REFRESH_MS } from "../../lib/config";
import { relTime } from "../../lib/format";
import type { FleetReport } from "../../types/fleet";
import { leaderboardStore } from "../board/leaderboard-data";
import type { GeneratorRoundDetail, GeneratorRoundList } from "../generators/generator-data";
import { latestScoredRound } from "../generators/generator-data";
import type { PipelineEntryExt } from "../operations/pipeline";
import { EntityButton } from "../ui/EntityButton";
import { ArenaScene } from "./ArenaScene";
import { FactoryScene } from "./FactoryScene";
import { MiniDitto, PaperDefs } from "./PaperFigures";
import type { AgentLabel } from "./round-script";
import { buildRoundScript } from "./round-script";
import { SecurityScene } from "./SecurityScene";
import type { CastMember, Theater } from "./theater-model";
import { buildTheater, theaterSummary } from "./theater-model";

/** Reactive media query; false where matchMedia is unavailable. */
function mediaQuery(query: string): () => boolean {
  const list = typeof matchMedia === "function" ? matchMedia(query) : null;
  const [matches, setMatches] = createSignal(Boolean(list?.matches));
  if (list) {
    const update = (): void => {
      setMatches(list.matches);
    };
    list.addEventListener?.("change", update);
    onCleanup(() => list.removeEventListener?.("change", update));
  }
  return matches;
}

function Stat(props: {
  label: string;
  value: number | null;
  id?: string;
  tone?: "good" | "warn" | "danger";
  note?: string | null;
}): JSX.Element {
  return (
    <div class="act-stat" data-tone={props.tone}>
      <dt>{props.label}</dt>
      <dd id={props.id}>
        {props.value == null ? "–" : props.value.toLocaleString("en-US")}
        <Show when={props.note}>
          <small>
            <span class="sr-only">, </span>
            {props.note}
          </small>
        </Show>
      </dd>
    </div>
  );
}

function CastList(props: {
  members: CastMember[];
  empty: string;
  detail: (member: CastMember) => string | null;
}): JSX.Element {
  return (
    <Show when={props.members.length} fallback={<p class="act-empty">{props.empty}</p>}>
      <ul class="act-cast">
        <For each={props.members}>
          {(member) => (
            <li>
              <MiniDitto persona={member.persona} />
              <EntityButton
                kind="agent"
                id={member.agentId}
                label={member.name}
                title="Open the agent card"
              />
              <Show when={props.detail(member)}>
                {(text) => <span class="act-cast-detail">{text()}</span>}
              </Show>
            </li>
          )}
        </For>
      </ul>
    </Show>
  );
}

function ReplayNote(props: {
  member: CastMember | null;
  live: boolean;
  verb: string;
}): JSX.Element {
  return (
    <Show when={!props.live && props.member}>
      {(member) => (
        <p class="act-replay">
          Replay · the newest submission, {member().name}, {props.verb}{" "}
          {relTime(member().submittedAt)}
        </p>
      )}
    </Show>
  );
}

export function PipelineTheater(props: {
  entries: PipelineEntryExt[];
  statusCounts: Record<string, number>;
  screeners: FleetReport | null;
  activeVersion: number | null;
  unavailable: boolean;
}): JSX.Element {
  let root: HTMLElement | undefined = undefined;
  const reduced = mediaQuery("(prefers-reduced-motion: reduce)");
  const [onScreen, setOnScreen] = createSignal(true);
  const [tabVisible, setTabVisible] = createSignal(
    typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  const [userPaused, setUserPaused] = createSignal(false);
  const [compact, setCompact] = createSignal(false);
  const motion = (): boolean => !reduced();
  const playing = (): boolean => motion() && onScreen() && tabVisible() && !userPaused();

  onMount(() => {
    const onVisibility = (): void => {
      setTabVisible(document.visibilityState !== "hidden");
    };
    document.addEventListener("visibilitychange", onVisibility);
    onCleanup(() => document.removeEventListener("visibilitychange", onVisibility));
    if (root && typeof IntersectionObserver === "function") {
      const observer = new IntersectionObserver((records) => {
        setOnScreen(records.some((record) => record.isIntersecting));
      });
      observer.observe(root);
      onCleanup(() => observer.disconnect());
    }
    if (root && typeof ResizeObserver === "function") {
      const sizer = new ResizeObserver((records) => {
        const width = records[0]?.contentRect.width ?? 0;
        setCompact(width > 0 && width < 640);
      });
      sizer.observe(root);
      onCleanup(() => sizer.disconnect());
    }
  });

  const theater = createMemo<Theater>(() =>
    buildTheater({
      entries: props.entries,
      statusCounts: props.statusCounts,
      screeners: props.screeners,
      activeVersion: props.activeVersion,
    }),
  );
  const summary = createMemo(() => theaterSummary(theater()));
  const number = (value: number): number | null =>
    props.unavailable && props.entries.length === 0 ? null : value;

  // The arena reads the newest scored generator round: list, then detail.
  const rounds = useEndpoint<GeneratorRoundList>("/generator/rounds", { pollMs: REFRESH_MS });
  const latest = createMemo(() => latestScoredRound(rounds.error() ? null : rounds.data()));
  const detail = useEndpoint<GeneratorRoundDetail>(() => {
    const index = latest()?.round_index;
    return index == null ? "" : "/generator/rounds/" + index;
  });
  const board = leaderboardStore();
  const labels = createMemo(() => {
    const map = new Map<string, AgentLabel>();
    for (const entry of board.entries()) {
      if (entry.agent_id) {
        map.set(String(entry.agent_id), { name: entry.agent_name, avatarUrl: entry.avatar_url });
      }
    }
    for (const entry of props.entries) {
      if (entry.agent_id && !map.has(entry.agent_id)) {
        map.set(entry.agent_id, { name: entry.name, avatarUrl: entry.avatar_url });
      }
    }
    return map;
  });
  const script = createMemo(() => {
    const round = detail.error() ? null : detail.data();
    return round ? buildRoundScript(round, (id) => labels().get(id)) : null;
  });

  const f = (): Theater["factory"] => theater().factory;
  const s = (): Theater["security"] => theater().security;
  const a = (): Theater["arena"] => theater().arena;

  return (
    <section
      ref={root}
      class="theater"
      aria-labelledby="theater-title"
      data-playing={playing() ? "true" : "false"}
      data-motion={motion() ? "on" : "off"}
    >
      <PaperDefs />
      <header class="theater-head">
        <div>
          <h2 id="theater-title">From upload to arena</h2>
          <p>
            Every submission is built into its own Paper Ditto, screened at security, then scored
            against the data generators. Each figure is a real submission; its look is picked from
            its id.
          </p>
        </div>
        <Show when={motion()}>
          <button
            type="button"
            class="btn theater-pause"
            aria-pressed={userPaused() ? "true" : "false"}
            onClick={() => setUserPaused((paused) => !paused)}
          >
            {userPaused() ? "Play animation" : "Pause animation"}
          </button>
        </Show>
      </header>
      <div class="theater-acts">
        <article class="act act-build" aria-labelledby="act-build-title">
          <header class="act-head">
            <span class="act-step">Build</span>
            <h3 id="act-build-title">The factory</h3>
          </header>
          <FactoryScene act={f()} featured={theater().featured} motion={motion()} />
          <dl class="act-stats">
            <Stat label="Queued for admission" value={number(f().queued)} id="theater-queued" />
            <Stat
              label="In screening"
              value={number(f().screening)}
              id="theater-screening"
              note={f().building ? `${f().building} on the line` : null}
            />
          </dl>
          <CastList
            members={[...f().stations, ...f().intake]}
            empty="Nobody on the line right now."
            detail={(member) =>
              member.spot === "intake" ? "Queued" : (member.stageLabel ?? "Building")
            }
          />
          <ReplayNote member={theater().featured} live={f().stations.length > 0} verb="submitted" />
          <p class="sr-only act-summary">{summary().factory}</p>
        </article>
        <article class="act act-screen" aria-labelledby="act-screen-title">
          <header class="act-head">
            <span class="act-step">Screening</span>
            <h3 id="act-screen-title">Security</h3>
          </header>
          <SecurityScene act={s()} featured={theater().featured} motion={motion()} />
          <dl class="act-stats">
            <Stat label="In source review" value={number(s().reviewing)} id="theater-reviewing" />
            <Stat label="Held for review" value={number(s().held)} id="theater-held" tone="warn" />
            <Stat label="Manual retry" value={number(s().retry)} id="theater-retry" tone="warn" />
            <Stat
              label="Rejected, all time"
              value={number(s().rejected)}
              id="theater-rejected"
              tone="danger"
            />
          </dl>
          <CastList
            members={[...s().scanning, ...s().secondary]}
            empty="Nobody at security right now."
            detail={(member) =>
              member.spot === "xray"
                ? (member.stageLabel ?? "Source review")
                : member.status === "screening_failed"
                  ? "Manual retry required"
                  : "Held for review"
            }
          />
          <ReplayNote member={theater().featured} live={s().scanning.length > 0} verb="submitted" />
          <p class="sr-only act-summary">{summary().security}</p>
        </article>
        <article class="act act-arena" aria-labelledby="act-arena-title">
          <header class="act-head">
            <span class="act-step">Scoring</span>
            <h3 id="act-arena-title">The arena</h3>
          </header>
          <dl class="act-stats act-stats-row">
            <Stat
              label="Waiting for validators"
              value={number(a().waiting)}
              id="theater-waiting"
              note={a().stuck ? `${a().stuck} on hold` : null}
            />
            <Stat
              label="Validator runs live"
              value={number(a().scoring)}
              id="theater-scoring"
              tone="good"
            />
            <Stat label="Scored & live" value={number(a().scored)} id="theater-scored" />
          </dl>
          <ArenaScene
            script={script()}
            scoredAt={latest()?.scored_at ?? null}
            motion={motion()}
            playing={playing()}
            compact={compact()}
          />
          <div class="act-lanes">
            <div>
              <h4>On the floor</h4>
              <CastList
                members={a().floor}
                empty="No validator runs right now."
                detail={(member) =>
                  member.percent != null
                    ? Math.round(member.percent) +
                      "%" +
                      (member.stageLabel ? " · " + member.stageLabel : "")
                    : member.stageLabel
                }
              />
            </div>
            <div>
              <h4>At the gate</h4>
              <CastList
                members={a().gate}
                empty="Nobody waiting for a validator."
                detail={(member) => member.holdLabel ?? "In line"}
              />
            </div>
          </div>
          <p class="sr-only act-summary">{summary().arena}</p>
        </article>
      </div>
    </section>
  );
}
