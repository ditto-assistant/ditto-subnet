// Accessible global search (monolith 6547–6753): a combobox over one corpus
// built from the leaderboard (miners) plus the deduped operations activity
// feed (submissions) — which is why boot fetches everything. Prefix matches
// on the title sort first, results cap at 8, and choosing a result jumps to
// the owning page and opens the entity overlay. Keyboard: ArrowUp/ArrowDown
// wrap through options, Enter opens, Escape closes, and "/" or Cmd/Ctrl+K
// focuses the field from anywhere.
import { For, Show, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { agentLabel, agentVersionLabel, fx, relTime, shortKey } from "../../lib/format";
import { dashboardHref } from "../../lib/router";
import { pushEntityRoute, syncFromLocation } from "../../stores/routeStore";
import { activityStage } from "../pipeline/status";
import type { LeaderboardEntry } from "../../types/leaderboard";
import type { PipelineEntry } from "../../types/pipeline";

/** Ranked board entry (rank assigned client-side, null when unranked). */
type RankedEntry = LeaderboardEntry & { rank?: number | null };

function stageLabel(status: string | undefined): string {
  return activityStage(status)[0];
}

function searchText(value: unknown): string {
  return String(value == null ? "" : value).toLowerCase();
}

function onSubmissionsPage(): boolean {
  return (location.hash || "").startsWith("#/submissions");
}

interface SearchItem {
  type: "miner" | "submission";
  hotkey?: string;
  submission?: PipelineEntry;
  title: string;
  detail: string;
  text: string;
}

export interface GlobalSearchProps {
  /** Current ranked leaderboard entries (the last successful payload). */
  miners: () => RankedEntry[];
  /** Operations activity feed entries (the last successful payload). */
  submissions: () => PipelineEntry[];
  /** Optional hook for the submissions page's server-side search: called
   * (debounced 250ms) with the query while #/submissions is active. */
  onServerSearch?: (query: string) => void;
}

export function GlobalSearch(props: GlobalSearchProps): JSX.Element {
  const [value, setValue] = createSignal("");
  const [open, setOpen] = createSignal(false);
  const [matches, setMatches] = createSignal<SearchItem[]>([]);
  const [activeIndex, setActiveIndex] = createSignal(-1);

  let root: HTMLDivElement | undefined;
  let input: HTMLInputElement | undefined;
  let serverSearchTimer: ReturnType<typeof setTimeout> | undefined;
  let pointerDownInside = false;

  function corpus(): SearchItem[] {
    const items: SearchItem[] = props.miners().map((entry) => {
      const model = entry.models && entry.models.harness ? entry.models.harness : "";
      return {
        type: "miner" as const,
        hotkey: entry.miner_hotkey,
        title: shortKey(entry.miner_hotkey),
        detail:
          (entry.rank ? "Rank #" + entry.rank : "Unranked") + " · composite " + fx(entry.composite),
        text: [entry.miner_hotkey, model, entry.bench_version].map(searchText).join(" "),
      };
    });
    const seen: Record<string, boolean> = {};
    props.submissions().forEach((entry) => {
      const key = String(entry.agent_id || entry.name || "");
      if (!key || seen[key]) return;
      seen[key] = true;
      const stage = stageLabel(entry.status);
      const preserved = (entry as { preserved_composite?: number | null }).preserved_composite;
      items.push({
        type: "submission",
        submission: entry,
        title: agentLabel(entry.name, entry.version),
        detail:
          String(entry.agent_id || "").slice(0, 8) +
          " · " +
          stage +
          (preserved != null
            ? " · composite " + fx(Number(preserved))
            : entry.provisional_composite != null
              ? " · provisional " + fx(Number(entry.provisional_composite))
              : "") +
          " · " +
          relTime(entry.submitted_at),
        text: [
          entry.name,
          agentVersionLabel(entry.version),
          entry.agent_id,
          entry.miner_hotkey,
          entry.status,
          stage,
          entry.review_reason,
          entry.screening_reason,
          entry.duplicate_of,
          entry.duplicate_name,
          agentVersionLabel(entry.duplicate_version),
        ]
          .map(searchText)
          .join(" "),
      });
    });
    return items;
  }

  function closeSearch(): void {
    setOpen(false);
    setActiveIndex(-1);
  }

  function updateSearch(): void {
    const query = value().trim().toLowerCase();
    setActiveIndex(-1);
    if (!query) {
      setMatches([]);
      return;
    }
    const found = corpus()
      .filter((item) => item.text.includes(query) || searchText(item.title).includes(query))
      .sort((a, b) => {
        const aStarts = searchText(a.title).indexOf(query) === 0;
        const bStarts = searchText(b.title).indexOf(query) === 0;
        return aStarts === bStarts ? 0 : aStarts ? -1 : 1;
      })
      .slice(0, 8);
    setMatches(found);
  }

  function setSearchActive(index: number): void {
    const total = matches().length;
    if (!total) return;
    const next = ((index % total) + total) % total;
    setActiveIndex(next);
    const option = document.getElementById("search-result-" + next);
    if (option && typeof option.scrollIntoView === "function") {
      option.scrollIntoView({ block: "nearest" });
    }
  }

  function chooseSearchResult(index: number): void {
    const result = matches()[index];
    if (!result) return;
    closeSearch();
    if (result.type === "miner" && result.hotkey) {
      // Re-resolve by hotkey against the CURRENT board (a background refresh
      // may have re-sorted since the corpus was built); a miner that dropped
      // off the board still gets its canonical entity route.
      history.pushState({}, "", dashboardHref("overview"));
      syncFromLocation();
      pushEntityRoute("miner", result.hotkey);
    } else if (result.submission && result.submission.agent_id) {
      history.pushState({}, "", dashboardHref("submissions"));
      syncFromLocation();
      pushEntityRoute("agent", String(result.submission.agent_id));
    }
  }

  function onInput(): void {
    setValue(input ? input.value : "");
    updateSearch();
    setOpen(true);
    // On #/submissions the same field doubles as the server-side submission
    // search (250ms debounce), owned by the submissions page via this hook.
    if (!props.onServerSearch) return;
    if (!onSubmissionsPage()) return;
    clearTimeout(serverSearchTimer);
    serverSearchTimer = setTimeout(() => {
      if (!onSubmissionsPage()) return;
      props.onServerSearch?.(value().trim().slice(0, 200));
    }, 250);
  }

  function onKeyDown(event: KeyboardEvent): void {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSearchActive(activeIndex() + 1);
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSearchActive(activeIndex() - 1);
    }
    if (event.key === "Enter" && matches().length) {
      event.preventDefault();
      chooseSearchResult(activeIndex() >= 0 ? activeIndex() : 0);
    }
    if (event.key === "Escape") {
      closeSearch();
      input?.blur();
    }
  }

  function clear(): void {
    setValue("");
    if (input) input.value = "";
    updateSearch();
    setOpen(true);
    input?.focus();
    if (props.onServerSearch && onSubmissionsPage()) {
      clearTimeout(serverSearchTimer);
      props.onServerSearch("");
    }
  }

  onMount(() => {
    const onDocPointerDown = (event: PointerEvent): void => {
      if (root && event.target instanceof Node && !root.contains(event.target)) closeSearch();
    };
    // Close when focus leaves the combobox + popover entirely (Tab away).
    // Safari/iOS blur the input on a result tap WITHOUT focusing the button
    // (relatedTarget null), so a pointerdown inside the combobox suppresses
    // the close until that tap's click has landed.
    const onRootPointerDown = (): void => {
      pointerDownInside = true;
      setTimeout(() => {
        pointerDownInside = false;
      }, 0);
    };
    const onFocusOut = (event: FocusEvent): void => {
      if (pointerDownInside) return;
      const next = event.relatedTarget;
      if (!next || !(next instanceof Node) || !root || !root.contains(next)) closeSearch();
    };
    const onDocKeyDown = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      const tag = target ? target.tagName : "";
      const typing = tag === "INPUT" || tag === "TEXTAREA" || Boolean(target?.isContentEditable);
      if (
        (!typing && event.key === "/") ||
        ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")
      ) {
        event.preventDefault();
        input?.focus();
        input?.select();
      }
    };
    document.addEventListener("pointerdown", onDocPointerDown);
    document.addEventListener("keydown", onDocKeyDown);
    root?.addEventListener("pointerdown", onRootPointerDown);
    root?.addEventListener("focusout", onFocusOut);
    onCleanup(() => {
      document.removeEventListener("pointerdown", onDocPointerDown);
      document.removeEventListener("keydown", onDocKeyDown);
      root?.removeEventListener("pointerdown", onRootPointerDown);
      root?.removeEventListener("focusout", onFocusOut);
      clearTimeout(serverSearchTimer);
    });
  });

  const hasQuery = () => Boolean(value().trim());
  const metaText = () => {
    if (!hasQuery()) return "Search by hotkey, agent name, or ID";
    const count = matches().length;
    return count
      ? count + (count === 1 ? " result" : " results") + " · Enter to open"
      : "No results";
  };

  return (
    <div
      class="global-search"
      id="global-search"
      classList={{ "has-query": hasQuery() }}
      ref={(el) => {
        root = el;
      }}
    >
      <label class="visually-hidden" for="search-input">
        Search miners and submissions
      </label>
      <div class="search-field">
        <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-4-4" />
        </svg>
        <input
          class="search-input"
          id="search-input"
          type="search"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={open() ? "true" : "false"}
          aria-controls="search-results"
          aria-activedescendant={activeIndex() >= 0 ? "search-result-" + activeIndex() : undefined}
          autocomplete="off"
          placeholder="Search miners or submissions"
          ref={(el) => {
            input = el;
          }}
          onFocus={() => {
            updateSearch();
            setOpen(true);
          }}
          onInput={onInput}
          onKeyDown={onKeyDown}
        />
        <kbd class="search-shortcut" aria-hidden="true">
          /
        </kbd>
        <button
          class="search-clear"
          id="search-clear"
          type="button"
          aria-label="Clear search"
          title="Clear search"
          onClick={clear}
        >
          ×
        </button>
      </div>
      <div class="search-popover" id="search-popover" hidden={!open()}>
        <div class="search-meta" id="search-meta" role="status" aria-live="polite">
          {metaText()}
        </div>
        <div class="search-results" id="search-results" role="listbox" aria-label="Search results">
          <Show
            when={hasQuery()}
            fallback={
              <div class="search-empty">
                Start typing to find a miner or submission. Press <b>↓</b> to move through results.
              </div>
            }
          >
            <Show
              when={matches().length}
              fallback={
                <div class="search-empty">
                  No miner or submission matches “{value().trim()}”.
                  <br />
                  Try a full or partial hotkey, agent name, or ID.
                </div>
              }
            >
              <For each={matches()}>
                {(item, index) => (
                  <button
                    class="search-result"
                    id={"search-result-" + index()}
                    type="button"
                    role="option"
                    aria-selected={activeIndex() === index() ? "true" : "false"}
                    data-search-i={index()}
                    onMouseMove={() => setSearchActive(index())}
                    onClick={() => chooseSearchResult(index())}
                  >
                    <span class="search-result-kind">
                      {item.type === "miner" ? "Miner" : "Submission"}
                    </span>
                    <span class="search-result-copy">
                      <span class="search-result-title">{item.title}</span>
                      <span class="search-result-detail">{item.detail}</span>
                    </span>
                    <span class="search-result-arrow" aria-hidden="true">
                      ↗
                    </span>
                  </button>
                )}
              </For>
            </Show>
          </Show>
        </div>
      </div>
    </div>
  );
}
