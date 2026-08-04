// Parity tests for the sidebar shell (assert-inventory rows 28 + 29):
// row 28 (test_sidebar_shell_routes_every_section) — the dashboard is a
// sidebar shell with hash-routed pages; every section has a nav item with
// its href + data-page pair, and the theme switcher lives in the sidebar.
// row 29 (test_advertises_public_source_repositories) — the open-source repo
// links (platform exactly twice: the sidebar GitHub button and the footer's
// "Platform source"), with accessible labels.
// Row 31 (mobile sidebar z-index below the modal) is a stylesheet contract
// and lives with the CSS port, not this component.
import { cleanup, fireEvent, render } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PageName } from "../../lib/router";
import { currentPage, syncFromLocation } from "../../stores/routeStore";
import { SiteFooter, Sidebar } from "./Sidebar";

beforeEach(() => {
  history.replaceState(null, "", "/#/overview");
  syncFromLocation();
});

afterEach(cleanup);

const BENCH = { active: 7, desired: 7, status: "activated", hasOlderRuns: false };

function renderSidebar(onRefresh: () => void = () => undefined): void {
  render(() => <Sidebar bench={BENCH} displayVersion={7} onRefresh={onRefresh} />);
}

describe("Sidebar routes every section (row 28)", () => {
  it("is a sidebar shell with a nav item per hash-routed page", () => {
    renderSidebar();
    expect(document.querySelector('aside.sidebar[aria-label="Site sections"]')).toBeTruthy();
    const pages: PageName[] = [
      "overview",
      "leaderboard",
      "operations",
      "submissions",
      "reviews",
      "benchmark",
    ];
    pages.forEach((page) => {
      const item = document.querySelector(`.nav-item[data-page="${page}"]`);
      expect(item, page).toBeTruthy();
      expect(item).toHaveAttribute("href", "#/" + page);
    });
  });

  it("keeps the theme switcher wired inside the sidebar", () => {
    renderSidebar();
    expect(document.querySelector('.sidebar [data-theme-choice="system"]')).toBeTruthy();
  });

  it("marks the current page active with aria-current and navigates on click", () => {
    renderSidebar();
    const overview = document.querySelector('.nav-item[data-page="overview"]');
    expect(overview).toHaveAttribute("aria-current", "page");
    expect(overview?.classList.contains("active")).toBe(true);
    const leaderboard = document.querySelector<HTMLAnchorElement>(
      '.nav-item[data-page="leaderboard"]',
    );
    if (!leaderboard) throw new Error("missing leaderboard nav item");
    fireEvent.click(leaderboard);
    expect(location.hash).toBe("#/leaderboard");
    expect(currentPage()).toBe("leaderboard");
    expect(leaderboard).toHaveAttribute("aria-current", "page");
    expect(overview?.classList.contains("active")).toBe(false);
  });

  it("labels each section with the monolith's copy and fills the benchmark version from data", () => {
    renderSidebar();
    const labels = Array.from(document.querySelectorAll(".ni-label")).map((el) => el.textContent);
    expect(labels).toEqual([
      "Overview",
      "Leaderboard",
      "Network ops",
      "Submissions",
      "ATH reviews",
      "Benchmark",
    ]);
    const benchmarkDesc = document.querySelector('.nav-item[data-page="benchmark"] .ni-desc');
    expect(benchmarkDesc).toHaveTextContent("What v7 measures");
  });

  it("names no version in the benchmark description before live data arrives", () => {
    render(() => <Sidebar bench={BENCH} displayVersion={null} onRefresh={() => undefined} />);
    const benchmarkDesc = document.querySelector('.nav-item[data-page="benchmark"] .ni-desc');
    expect(benchmarkDesc).toHaveTextContent("Scoring benchmark");
  });

  it("carries the bench badge and the refresh control", () => {
    const onRefresh = vi.fn();
    renderSidebar(onRefresh);
    expect(document.getElementById("bench-badge")).toHaveTextContent("DittoBench v7");
    const refresh = document.getElementById("refresh");
    if (!refresh) throw new Error("missing refresh button");
    fireEvent.click(refresh);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("links the configured wandb telemetry target", () => {
    renderSidebar();
    const wandb = document.getElementById("wandb-link");
    expect(wandb).toHaveAttribute("href", "https://wandb.ai/");
    expect(wandb).toHaveAttribute("aria-label", "Full telemetry (wandb)");
    expect(wandb).toHaveAttribute("rel", "noopener");
  });
});

describe("Public source repositories (row 29)", () => {
  it("advertises the open-source stack with accessible labels", () => {
    render(() => (
      <>
        <Sidebar bench={BENCH} displayVersion={7} onRefresh={() => undefined} />
        <SiteFooter />
      </>
    ));
    expect(document.querySelector('[aria-label="Platform source on GitHub"]')).toBeTruthy();
    // The platform source appears exactly twice: the sidebar GitHub button and
    // the footer's "Platform source" link.
    const platformLinks = document.querySelectorAll(
      'a[href="https://github.com/ditto-assistant/ditto-subnet/tree/main/apps/platform"]',
    );
    expect(platformLinks.length).toBe(2);
    expect(
      document.querySelector('a[href="https://github.com/ditto-assistant/ditto-subnet"]'),
    ).toBeTruthy();
    expect(
      document.querySelector(
        'a[href="https://github.com/ditto-assistant/ditto-subnet/tree/main/workers/screener"]',
      ),
    ).toBeTruthy();
    expect(
      document.querySelector(
        'a[href="https://github.com/ditto-assistant/ditto-subnet/tree/main/research/dittobench-datagen"]',
      ),
    ).toBeTruthy();
    expect(
      document.querySelector(
        'a[href="https://github.com/ditto-assistant/ditto-subnet/tree/main/miners/dittobench-starter-kit"]',
      ),
    ).toBeTruthy();
    expect(document.querySelector('[aria-label="Open-source Ditto repositories"]')).toBeTruthy();
  });
});
