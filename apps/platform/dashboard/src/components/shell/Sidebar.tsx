// The sidebar shell (monolith 2536–2605): wordmark + bench badge, the
// path-routed nav with its inline SVG icons, the theme switcher, and the
// side-foot controls (wandb telemetry link, platform source link, manual
// refresh). SiteFooter is the open-source repository footer (2995–3007) that
// renders on the benchmark page; it lives here because the shell owns the
// public-source-repositories contract (platform link appears exactly twice:
// #github-link in the sidebar and "Platform source" in the footer).
import { For } from "solid-js";
import type { JSX } from "solid-js";

import { WANDB_URL } from "../../lib/config";
import type { PageName } from "../../lib/router";
import type { ChainEpoch } from "../../types/leaderboard";
import { dashboardHref } from "../../lib/router";
import { minerSession } from "../../stores/sessionStore";
import { currentPage, navigateToPage } from "../../stores/routeStore";
import { BenchBadge } from "./BenchBadge";
import { EpochClock } from "./EpochClock";
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
  /** /public/weights `epoch`, for the rail's payout clock. An accessor so the
   * clock re-reads it on every poll without re-rendering the whole rail. */
  epoch: () => ChainEpoch | null | undefined;
  onRefresh: () => void;
}

// Plain left clicks route through the store; modified clicks keep native
// anchor behavior (new tab etc.) on the crawlable pathname href.
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

/**
 * The approved hybrid wordmark from the Ditto typography kit: an outlined
 * vector (not typed text) kept at its 3077:732 aspect ratio, never paired
 * with a neighbouring symbol. It is the one brand mark on the page; the fill
 * follows `currentColor`.
 */
export function Wordmark(): JSX.Element {
  return (
    <svg class="wordmark" viewBox="0 0 3077 732" role="img" aria-label="Ditto">
      <g fill="currentColor" fill-rule="evenodd" transform="translate(-87 718) scale(1 -1)">
        <path d="M119 0Q91 0 91 28V672Q91 700 119 700H378Q455 700 517.5 674.5Q580 649 625.5 602.5Q671 556 695 492Q719 428 719 350Q719 273 695 208.5Q671 144 626 97.5Q581 51 518 25.5Q455 0 378 0ZM241 135H373Q418 135 454 150.5Q490 166 515.5 193.5Q541 221 554 261Q567 301 567 350Q567 399 554 438.5Q541 478 515.5 506Q490 534 454 549.5Q418 565 373 565H241Z" />
        <path
          transform="translate(716 0)"
          d="M99 0H397Q423 0 423 26V106Q423 132 397 132H339Q323 132 323 148V552Q323 568 339 568H397Q423 568 423 594V674Q423 700 397 700H99Q73 700 73 674V594Q73 568 99 568H157Q173 568 173 552V148Q173 132 157 132H99Q73 132 73 106V26Q73 0 99 0Z"
        />
        <path
          transform="translate(1176 0)"
          d="M272 0H362Q394 0 394 32V545Q394 565 414 565H565Q597 565 597 597V668Q597 700 565 700H65Q33 700 33 668V597Q33 565 65 565H219Q239 565 239 545V32Q239 0 272 0Z"
        />
        <path
          transform="translate(1798 0)"
          d="M272 0H362Q394 0 394 32V545Q394 565 414 565H565Q597 565 597 597V668Q597 700 565 700H65Q33 700 33 668V597Q33 565 65 565H219Q239 565 239 545V32Q239 0 272 0Z"
        />
        <path
          transform="translate(2403 0)"
          d="M402 -10Q324 -10 259.0 17.0Q194 44 145.5 92.5Q97 141 71.0 207.5Q45 274 45 352Q45 431 71.0 497.0Q97 563 145.5 611.5Q194 660 259.0 687.0Q324 714 401 714Q479 714 543.5 687.0Q608 660 656.5 611.0Q705 562 731.0 496.0Q757 430 757 352Q757 274 731.0 208.0Q705 142 656.5 93.0Q608 44 543.5 17.0Q479 -10 402 -10ZM401 132Q446 132 483.0 148.5Q520 165 547.0 194.5Q574 224 589.5 264.0Q605 304 605 352Q605 400 589.5 440.0Q574 480 547.0 509.5Q520 539 483.0 555.5Q446 572 401 572Q357 572 320.0 555.5Q283 539 255.0 509.5Q227 480 212.5 440.0Q198 400 198 352Q198 304 212.5 264.0Q227 224 255.0 194.5Q283 165 320.0 148.5Q357 132 401 132Z"
        />
      </g>
    </svg>
  );
}

export function Sidebar(props: SidebarProps): JSX.Element {
  return (
    <aside class="sidebar" aria-label="Site sections">
      <div class="brand">
        <Wordmark />
        <div class="sub">
          Subnet&nbsp;118 · Public agent-memory scoring leaderboard
          <BenchBadge {...props.bench} />
        </div>
      </div>
      <EpochClock epoch={props.epoch} />
      <nav class="nav" id="site-nav" aria-label="Sections">
        <For each={NAV_ITEMS}>
          {(item) => (
            <a
              class="nav-item"
              classList={{ active: currentPage() === item.page }}
              href={dashboardHref(item.page)}
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
