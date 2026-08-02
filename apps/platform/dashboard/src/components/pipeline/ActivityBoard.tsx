// The recent-submissions activity board (monolith markup 2864–2908 +
// renderActivity 8114–8183 + renderActivityFilterControls 3381–3394 +
// setActivityPagerVisible 8110–8112 + the row/filter/pager wiring
// 6490–6538): server-backed quick filters, two pagers, and the five-column
// table whose rows open the agent evidence drawer.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import {
  agentLabel,
  agentName,
  agentVersionLabel,
  duplicateLabel,
  relTime,
  shortKey,
} from "../../lib/format";
import { pushEntityRoute } from "../../stores/routeStore";
import { CopyButton } from "../shell/CopyButton";
import { EntityButton } from "../ui/EntityButton";
import { Pager } from "../ui/Pager";
import { StatusChip } from "../ui/StatusChip";
import { artifactReleaseCopy, artifactReleaseNote } from "./artifact-release";
import type { ArtifactRelease } from "./artifact-release";
import type { ActivityStore } from "./activity-store";
import {
  ACTIVITY_FILTER_LABELS,
  ACTIVITY_FILTER_NAMES,
  activityStage,
  duplicateComparisonLabel,
  reviewEvidenceNotes,
  validationProgress,
} from "./status";
import type { ActivityStatusEntry } from "./status";

type ActivityRow = ActivityStatusEntry & { artifact_release?: ArtifactRelease | null };

function rowAriaLabel(e: ActivityRow): string {
  const stage = activityStage(e.status);
  const sourceCopy = artifactReleaseCopy(e.artifact_release);
  return (
    "View details for " +
    agentLabel(e.name, e.version) +
    ", " +
    stage[0] +
    ", " +
    validationProgress(e) +
    (sourceCopy ? ", " + sourceCopy.label : "")
  );
}

function openRow(e: ActivityRow): void {
  if (e.agent_id) pushEntityRoute("agent", String(e.agent_id));
}

function interactiveTarget(ev: Event): boolean {
  const target = ev.target as HTMLElement | null;
  return Boolean(target && target.closest(".copy, a"));
}

function StageCell(props: { entry: ActivityRow }): JSX.Element {
  const e = () => props.entry;
  const stage = () => activityStage(e().status);
  const note = () => artifactReleaseNote(e().artifact_release);
  return (
    <td class="stage-cell">
      <StatusChip label={stage()[0]} tone={stage()[1]} />
      {/* #622: the CURRENT review reason leads under its event label; the
          initial hold reason stays visible as history when it differs. */}
      <For each={reviewEvidenceNotes(e())}>
        {(evidenceNote) => (
          <span class="stage-note">
            <b>{evidenceNote.label}:</b> {evidenceNote.text}
          </span>
        )}
      </For>
      <Show when={!e().review_reason && e().screening_reason}>
        <span class="stage-note">
          <b>Screening:</b> {e().screening_reason}
        </span>
      </Show>
      {/* #636: past the opening event, the mechanical duplicate claim reads
          as the initial comparison, not the live review reason. */}
      <Show when={e().duplicate_of}>
        {(duplicateOf) => (
          <span class="stage-note">
            <b>{duplicateComparisonLabel(e())}:</b>{" "}
            <EntityButton kind="agent" id={duplicateOf()} label={duplicateLabel(e())} />
          </span>
        )}
      </Show>
      <Show when={note()}>
        {(n) => <span class={"source-release-note " + n().state}>{n().text}</span>}
      </Show>
    </td>
  );
}

export function ActivityBoard(props: { store: ActivityStore }): JSX.Element {
  const store = props.store;
  const pagerHidden = () => !store.unavailable() && store.totalPages() <= 1;
  const pageInfo = () => "Page " + store.page() + " of " + store.totalPages();
  const summary = (): string => {
    if (store.busy()) return "Updating submissions…";
    if (store.unavailable()) return "Could not load submissions. Try again.";
    const total = store.total();
    if (!store.loaded() || total == null) return "Loading submissions…";
    return (
      total +
      (total === 1 ? " submission" : " submissions") +
      (store.filtered() ? " match" : " total")
    );
  };
  const emptyMessage = (): string => {
    if (store.unavailable()) return "Submission activity is temporarily unavailable.";
    if (!store.loaded()) return "Loading submissions…";
    return store.filtered()
      ? "No submissions match these filters. Clear filters or try a different search."
      : "No submissions yet. New uploads will appear here before screening begins.";
  };
  const onPrev = (anchor: HTMLElement | null): void => {
    if (store.page() > 1) store.navigatePage(store.page() - 1, anchor, true, true);
  };
  const onNext = (anchor: HTMLElement | null): void => {
    if (store.page() < store.totalPages()) store.navigatePage(store.page() + 1, anchor, true, true);
  };

  return (
    <div
      class="board activity"
      aria-busy={store.busy() ? "true" : "false"}
      tabindex="0"
      role="region"
      aria-label="Recent submissions, horizontally scrollable on small screens"
    >
      <div class="activity-filters" aria-labelledby="activity-filter-label">
        <span class="visually-hidden" id="activity-filter-label">
          Filter recent submissions by public pipeline state
        </span>
        <div class="activity-filter-list" role="group" aria-label="Quick submission filters">
          <For each={ACTIVITY_FILTER_NAMES.slice()}>
            {(name) => (
              <button
                class="activity-filter"
                type="button"
                data-activity-filter={name}
                aria-pressed={store.filterSelected(name) ? "true" : "false"}
                onClick={() => store.applyFilter(name)}
              >
                {ACTIVITY_FILTER_LABELS[name]}{" "}
                <span class="activity-filter-count" data-activity-count={name}>
                  {store.loaded() && !store.unavailable() ? String(store.filterCount(name)) : "–"}
                </span>
              </button>
            )}
          </For>
        </div>
        <div class="activity-filter-meta">
          <span
            class="activity-filter-summary"
            id="activity-filter-summary"
            role="status"
            aria-live="polite"
          >
            {summary()}
          </span>
          <button
            class="activity-clear"
            id="activity-clear"
            type="button"
            hidden={!store.filtered()}
            onClick={(ev) => {
              // The shared search input doubles as the submissions server
              // search; clearing the filters clears it too (monolith 6532).
              const input = document.getElementById("search-input") as HTMLInputElement | null;
              if (input) input.value = "";
              store.clearFilters(ev.currentTarget as HTMLElement);
            }}
          >
            Clear filters
          </button>
        </div>
      </div>
      <Pager
        class="pager top"
        id="activity-pager"
        label="Submission pages"
        info={pageInfo()}
        activityData
        hidden={pagerHidden()}
        prevDisabled={store.unavailable() || store.page() <= 1}
        nextDisabled={store.unavailable() || store.page() >= store.totalPages()}
        onPrev={() => onPrev(document.getElementById("activity-pager"))}
        onNext={() => onNext(document.getElementById("activity-pager"))}
      />
      <div class="activity-table-frame" ref={(el) => store.setFrame(el)}>
        <table aria-label="Recent agent submission pipeline">
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col" class="hide-sm">
                Miner
              </th>
              <th scope="col" style={{ width: "390px" }}>
                Stage &amp; evidence
              </th>
              <th scope="col" style={{ width: "120px" }}>
                Validation
              </th>
              <th scope="col" class="num hide-sm" style={{ width: "110px" }}>
                Submitted
              </th>
            </tr>
          </thead>
          <tbody id="activity-rows">
            <Show
              when={store.entries().length}
              fallback={
                <tr>
                  <td colspan="5" class="empty">
                    <div class="empty-msg">{emptyMessage()}</div>
                  </td>
                </tr>
              }
            >
              <For each={store.entries()}>
                {(e, i) => {
                  const progress = () => validationProgress(e);
                  const id = String(e.agent_id || "");
                  return (
                    <tr
                      tabindex="0"
                      data-activity-i={i()}
                      aria-label={rowAriaLabel(e)}
                      onClick={(ev) => {
                        if (interactiveTarget(ev)) return;
                        openRow(e);
                      }}
                      onKeyDown={(ev) => {
                        if (interactiveTarget(ev)) return;
                        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
                          ev.preventDefault();
                          openRow(e);
                        }
                      }}
                    >
                      <td>
                        <span class="agent-name">{agentName(e.name)}</span>
                        <span class="submission-version">{agentVersionLabel(e.version)}</span>
                        <span class="agent-id copyable" title={id}>
                          <span>{id.slice(0, 8)}</span>
                          <CopyButton value={id} label="agent ID" />
                        </span>
                      </td>
                      <td class="hide-sm">
                        <span class="hotkey copyable" title={e.miner_hotkey}>
                          <span>{shortKey(e.miner_hotkey)}</span>
                          <CopyButton value={e.miner_hotkey} label="miner hotkey" />
                        </span>
                      </td>
                      <StageCell entry={e} />
                      <td>
                        <span
                          class={
                            "validation-progress" + (progress() === "Not started" ? " pending" : "")
                          }
                        >
                          {progress()}
                        </span>
                      </td>
                      <td class="num hide-sm" title={e.submitted_at}>
                        {relTime(e.submitted_at)}
                      </td>
                    </tr>
                  );
                }}
              </For>
            </Show>
          </tbody>
        </table>
      </div>
      {/* The bottom pager is rendered inline rather than through the shared
          Pager: the monolith deliberately gives only the TOP status span
          aria-live (2920 vs 2941), so page changes announce once — the
          bottom twin is a visual convenience, not a second live region. */}
      <nav class="pager bottom" aria-label="Submission pages, bottom" hidden={pagerHidden()}>
        <button
          class="btn ghost"
          type="button"
          data-activity-page="prev"
          disabled={store.unavailable() || store.page() <= 1}
          onClick={() => onPrev(null)}
        >
          <span aria-hidden="true">←</span> Previous
        </button>
        <span class="page-status">{pageInfo()}</span>
        <button
          class="btn ghost"
          type="button"
          data-activity-page="next"
          disabled={store.unavailable() || store.page() >= store.totalPages()}
          onClick={() => onNext(null)}
        >
          Next <span aria-hidden="true">→</span>
        </button>
      </nav>
    </div>
  );
}
