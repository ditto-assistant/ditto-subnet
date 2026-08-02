// Benchmark reference displays fed by the glossary payload (renderBenchDocs
// 9535–9606): the version-specific "what this version is" copy, the version
// changelog, and the standalone category + metric glossary. Each keeps its
// static loading placeholder until the fetch lands — the markup is the
// offline fallback, exactly like the monolith.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import type { GlossaryCategory, GlossaryMetric, GlossaryVersion } from "../../types/bench";
import type { ChangelogItem } from "./docs";
import { glossaryGroups, neutralVersionCopy } from "./docs";

/** Version changelog: every contract, newest first, with what it changed.
 * The active contract is highlighted; an OPEN rollout target is labeled
 * separately and an in-flight target is never shown as active. */
export function VersionHistory(props: { items: ChangelogItem[] }): JSX.Element {
  return (
    <div id="bench-changelog">
      <Show
        when={props.items.length}
        fallback={<p class="muted">Loading the benchmark changelog…</p>}
      >
        <For each={props.items}>
          {(item) => (
            <div
              class={
                "ver-item" + (item.active ? " ver-current" : item.rollout ? " ver-rollout" : "")
              }
            >
              <div class="ver-head">
                <span class="ver-tag">v{item.version}</span>
                <b>{item.title}</b>
                <Show when={item.active}>
                  <span class="ver-now">active</span>
                </Show>
                <Show when={!item.active && item.rollout}>
                  <span class="ver-next">rollout</span>
                </Show>
                <span class="ver-epoch">{item.epoch}</span>
              </div>
              <div class="ver-sum">{item.summary}</div>
              <Show when={item.highlights.length}>
                <ul class="ver-hl">
                  <For each={item.highlights}>{(highlight) => <li>{highlight}</li>}</For>
                </ul>
              </Show>
            </div>
          )}
        </For>
      </Show>
    </div>
  );
}

/**
 * The "What v{N} is" paragraph (#bs-v4-copy). Three tiers, exactly the
 * monolith's overlay order: the glossary's own entry for the live version
 * wins; a known non-v4 version without a glossary entry gets the neutral
 * versioning-rule copy; offline keeps the static v4 paragraph, which the
 * static markup shipped as the fallback.
 */
export function VersionParagraph(props: {
  entry: GlossaryVersion | null;
  displayVersion: number | null;
}): JSX.Element {
  return (
    <span id="bs-v4-copy">
      <Show
        when={props.entry}
        fallback={
          <Show when={props.displayVersion && props.displayVersion !== 4} fallback={<StaticV4 />}>
            {neutralVersionCopy(props.displayVersion as number)}
          </Show>
        }
      >
        {(entry) => (
          <>
            <b>{entry().title}.</b> {entry().summary}
            <Show when={(entry().highlights || []).length}>
              <ul style="margin:6px 0 0;padding-left:18px">
                <For each={entry().highlights || []}>{(highlight) => <li>{highlight}</li>}</For>
              </ul>
            </Show>
          </>
        )}
      </Show>
    </span>
  );
}

/** The static v4 description (markup 2961) — the offline fallback only. */
function StaticV4(): JSX.Element {
  return (
    <>
      Version 4 is not a new benchmark. It is version 3 with a set of scoring false positives
      corrected: cases where the machinery penalised an agent for doing the right thing. The suite
      it administers is the same suite; what changed is that several ways of being <i>correct</i> no
      longer lose points. A leaking canary is charged once instead of twice, delete instructions are
      graded as acknowledgements rather than on whether they echoed a noun phrase, decimal durations
      parse ("1.5 years" was read as 15), and "used to" is treated as a temporal marker rather than
      a cessation phrase. Scores rise where an agent was being penalised for correct behaviour; the
      tests themselves are unchanged.
    </>
  );
}

/** Standalone glossary: every scored category (grouped by kind) and every
 * metric / gate factor, so the definitions are browsable off a run too. */
export function GlossaryDisplay(props: {
  categories: GlossaryCategory[];
  metrics: GlossaryMetric[];
}): JSX.Element {
  return (
    <div id="bench-glossary-display">
      <Show
        when={props.categories.length || props.metrics.length}
        fallback={<p class="muted">Loading the glossary…</p>}
      >
        <details class="gloss" open>
          <summary>Score metrics &amp; gate factors ({props.metrics.length})</summary>
          <For each={props.metrics}>
            {(metric) => (
              <div class="gitem">
                <div class="gterm">{metric.label}</div>
                <div class="gdesc">{metric.description}</div>
              </div>
            )}
          </For>
        </details>
        <details class="gloss">
          <summary>Every scored test category ({props.categories.length})</summary>
          <div class="gdesc" style="padding:4px 0 6px">
            What each case probes, never the answer key.
          </div>
          <For each={glossaryGroups(props.categories)}>
            {(group) => (
              <>
                <div class="gcat-head">{group.label}</div>
                <For each={group.rows}>
                  {(category) => (
                    <div class="gitem">
                      <div class="gterm">{category.label}</div>
                      <div class="gdesc">{category.purpose}</div>
                      {/* A short, public-safe example of what the case looks
                          like (illustrative only, never the actual seeded
                          prompt or its answer). */}
                      <Show when={category.example}>
                        <div class="gex">
                          <span class="gex-tag">e.g.</span>
                          {category.example}
                        </div>
                      </Show>
                    </div>
                  )}
                </For>
              </>
            )}
          </For>
        </details>
      </Show>
    </div>
  );
}
