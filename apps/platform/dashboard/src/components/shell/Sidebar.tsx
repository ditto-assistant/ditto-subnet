// The sidebar shell (monolith 2536–2605): brand + bench badge, the
// hash-routed nav with its inline SVG icons, the theme switcher, and the
// side-foot controls (wandb telemetry link, platform source link, manual
// refresh). SiteFooter is the open-source repository footer (2995–3007) that
// renders on the benchmark page; it lives here because the shell owns the
// public-source-repositories contract (platform link appears exactly twice:
// #github-link in the sidebar and "Platform source" in the footer).
import { For, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { WANDB_URL } from "../../lib/config";
import type { PageName } from "../../lib/router";
import { minerSession } from "../../stores/sessionStore";
import { currentPage, navigateToPage } from "../../stores/routeStore";
import { BenchBadge } from "./BenchBadge";
import type { BenchBadgeProps } from "./BenchBadge";
import { ThemeSwitcher } from "./ThemeSwitcher";

interface NavItem {
  page: PageName;
  label: string | (() => string);
  desc: (benchVersion: number | null) => string;
  icon: () => JSX.Element;
}

const NAV_ITEMS: NavItem[] = [
  {
    page: "overview",
    label: "Overview",
    desc: () => "Snapshot & leaderboard",
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <rect x="3" y="3" width="7" height="9" rx="1" />
        <rect x="14" y="3" width="7" height="5" rx="1" />
        <rect x="14" y="12" width="7" height="9" rx="1" />
        <rect x="3" y="16" width="7" height="5" rx="1" />
      </svg>
    ),
  },
  {
    page: "leaderboard",
    label: "Leaderboard",
    desc: () => "Full ranked table",
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <path d="M8 21V9" />
        <path d="M16 21v-5" />
        <path d="M4 21v-3" />
        <path d="M20 21v-8" />
        <path d="M4 3h16" />
      </svg>
    ),
  },
  {
    page: "pipeline",
    label: "Pipeline",
    desc: () => "Submission flow & screening",
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <rect width="8" height="8" x="3" y="3" rx="2" />
        <path d="M7 11v4a2 2 0 0 0 2 2h4" />
        <rect width="8" height="8" x="13" y="13" rx="2" />
      </svg>
    ),
  },
  {
    page: "operations",
    label: "Fleet",
    desc: () => "Validators, screeners & builds",
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <rect width="20" height="8" x="2" y="2" rx="2" />
        <rect width="20" height="8" x="2" y="14" rx="2" />
        <path d="M6 6h.01" />
        <path d="M6 18h.01" />
      </svg>
    ),
  },
  {
    page: "submissions",
    label: "Submissions",
    desc: () => "Recent uploads",
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <path d="M22 12h-6l-2 3h-4l-2-3H2" />
        <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
      </svg>
    ),
  },
  {
    page: "reviews",
    label: () => (minerSession() ? "Account" : "Sign in"),
    desc: () => (minerSession() ? "Your miner console" : "Miner profile & MCP"),
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <path d="M12 3 3.6 7.2v5.6c0 4.7 3.6 7.2 8.4 8.2 4.8-1 8.4-3.5 8.4-8.2V7.2Z" />
        <path d="M9 12h6M12 9v6" />
      </svg>
    ),
  },
  {
    page: "ath",
    label: "ATH reviews",
    desc: () => "Active public holds",
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <path d="M12 3 3.6 7.2v5.6c0 4.7 3.6 7.2 8.4 8.2 4.8-1 8.4-3.5 8.4-8.2V7.2Z" />
        <path d="M9 12h6M12 9v6" />
      </svg>
    ),
  },
  {
    page: "benchmark",
    label: "Benchmark",
    // The nav description names the live version once known (monolith
    // applyBenchVersion 9740–9765); the static fallback names no version.
    desc: (v) => (v ? "What v" + v + " measures" : "Scoring benchmark"),
    icon: () => (
      <svg class="ic" viewBox="0 0 24 24">
        <path d="M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.41 2.41 0 0 1 0-3.4l2.6-2.6a2.41 2.41 0 0 1 3.4 0Z" />
        <path d="m14.5 12.5 2-2" />
        <path d="m11.5 9.5 2-2" />
        <path d="m8.5 6.5 2-2" />
        <path d="m17.5 15.5 2-2" />
      </svg>
    ),
  },
];

export interface SidebarProps {
  bench: BenchBadgeProps;
  /** benchmarkDisplayVersion(): fills the benchmark nav description. */
  displayVersion: number | null;
  onRefresh: () => void;
}

// Plain left clicks route through the store; modified clicks keep native
// anchor behavior (new tab etc.) on the static "#/{page}" href.
function onNavClick(ev: MouseEvent, page: PageName): void {
  if (
    ev.defaultPrevented ||
    ev.button !== 0 ||
    ev.metaKey ||
    ev.ctrlKey ||
    ev.shiftKey ||
    ev.altKey
  ) {
    return;
  }
  ev.preventDefault();
  navigateToPage(page);
}

export function Sidebar(props: SidebarProps): JSX.Element {
  onMount(() => {
    // Reuse the inlined brand logo as the favicon (monolith 3087–3091).
    const logo = document.getElementById("brand-logo") as HTMLImageElement | null;
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (logo && icon) icon.href = logo.src;
  });

  return (
    <aside class="sidebar" aria-label="Site sections">
      <div class="brand">
        <div class="mark">
          <img id="brand-logo" alt="Ditto" src="/assets/paperditto-512.png" />
        </div>
        <div>
          <div class="brand-name">Ditto&nbsp;·&nbsp;Subnet&nbsp;118</div>
          <div class="sub">
            Public agent-memory scoring leaderboard
            <BenchBadge {...props.bench} />
          </div>
        </div>
      </div>
      <nav class="nav" id="site-nav" aria-label="Sections">
        <For each={NAV_ITEMS}>
          {(item) => (
            <a
              class="nav-item"
              classList={{ active: currentPage() === item.page }}
              href={"#/" + item.page}
              data-page={item.page}
              aria-current={currentPage() === item.page ? "page" : undefined}
              onClick={(ev) => onNavClick(ev, item.page)}
            >
              <span class="ni-icon" aria-hidden="true">
                {item.icon()}
              </span>
              <span class="ni-text">
                <span class="ni-label">
                  {typeof item.label === "function" ? item.label() : item.label}
                </span>
                <span class="ni-desc">{item.desc(props.displayVersion)}</span>
              </span>
            </a>
          )}
        </For>
      </nav>
      <ThemeSwitcher />
      <div class="side-foot">
        <a
          id="wandb-link"
          class="btn ghost"
          href={WANDB_URL}
          target="_blank"
          rel="noopener"
          aria-label="Full telemetry (wandb)"
          title="Full telemetry (wandb)"
        >
          <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
            <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" />
            <polyline points="16 7 22 7 22 13" />
          </svg>
          <span class="btn-label">
            {" Full telemetry "}
            <svg class="ic ext" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M15 3h6v6" />
              <path d="M10 14 21 3" />
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6" />
            </svg>
          </span>
        </a>
        <a
          id="github-link"
          class="btn ghost"
          href="https://github.com/ditto-assistant/ditto-subnet/tree/main/apps/platform"
          target="_blank"
          rel="noopener"
          aria-label="Platform source on GitHub"
          title="Platform source on GitHub"
        >
          <svg class="ic github-mark" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 .7a11.5 11.5 0 0 0-3.64 22.41c.58.11.79-.25.79-.56v-2.23c-3.22.7-3.9-1.37-3.9-1.37-.52-1.34-1.28-1.69-1.28-1.69-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.57-.29-5.27-1.28-5.27-5.68 0-1.26.45-2.28 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.16 1.18a10.96 10.96 0 0 1 5.76 0c2.19-1.49 3.16-1.18 3.16-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.83 1.19 3.09 0 4.41-2.71 5.38-5.29 5.67.42.36.79 1.07.79 2.16v3.2c0 .31.21.68.8.56A11.5 11.5 0 0 0 12 .7Z" />
          </svg>
          <span class="btn-label"> GitHub</span>
        </a>
        <button id="refresh" class="btn" title="Refresh now" onClick={() => props.onRefresh()}>
          <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
            <path d="M21 3v5h-5" />
            <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
            <path d="M8 16H3v5" />
          </svg>{" "}
          Refresh
        </button>
      </div>
    </aside>
  );
}

/** The benchmark-page footer (monolith 2995–3007): every open-source repo in
 * the stack, labelled for assistive tech. */
export function SiteFooter(): JSX.Element {
  return (
    <footer>
      <div class="foot-links" aria-label="Open-source Ditto repositories">
        <span class="foot-label">Open-source stack</span>
        <a id="foot-wandb" href={WANDB_URL} target="_blank" rel="noopener">
          Full per-epoch telemetry (wandb) ↗
        </a>
        <a
          href="https://github.com/ditto-assistant/ditto-subnet/tree/main/apps/platform"
          target="_blank"
          rel="noopener"
        >
          Platform source ↗
        </a>
        <a href="https://github.com/ditto-assistant/ditto-subnet" target="_blank" rel="noopener">
          Subnet &amp; validator ↗
        </a>
        <a
          href="https://github.com/ditto-assistant/ditto-subnet/tree/main/workers/screener"
          target="_blank"
          rel="noopener"
        >
          Screening worker ↗
        </a>
        <a
          href="https://github.com/ditto-assistant/ditto-subnet/tree/main/services/dittobench-api"
          target="_blank"
          rel="noopener"
        >
          Scoring engine ↗
        </a>
        <a
          href="https://github.com/ditto-assistant/ditto-subnet/tree/main/research/dittobench-datagen"
          target="_blank"
          rel="noopener"
        >
          Dataset &amp; grader ↗
        </a>
        <a
          href="https://github.com/ditto-assistant/ditto-subnet/tree/main/miners/dittobench-starter-kit"
          target="_blank"
          rel="noopener"
        >
          Miner starter kit ↗
        </a>
        <a href="https://github.com/ditto-assistant/ditto-harness" target="_blank" rel="noopener">
          Memory harness ↗
        </a>
      </div>
    </footer>
  );
}
