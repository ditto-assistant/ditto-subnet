import { cleanup, fireEvent, render } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { THEME_STORAGE_KEY, ThemeSwitcher, fromHour, themeBootstrap } from "./ThemeSwitcher";

beforeEach(() => {
  localStorage.clear();
  delete window.__dittoDashboardTheme;
  const root = document.documentElement;
  delete root.dataset.theme;
  delete root.dataset.systemTheme;
  delete root.dataset.timePhase;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function choice(mode: string): HTMLButtonElement {
  const el = document.querySelector<HTMLButtonElement>(`[data-theme-choice="${mode}"]`);
  if (!el) throw new Error("missing theme choice " + mode);
  return el;
}

describe("ThemeSwitcher (row 27)", () => {
  it("offers all four modes with the group semantics", () => {
    render(() => <ThemeSwitcher />);
    ["system", "light", "dark", "time"].forEach((mode) => expect(choice(mode)).toBeTruthy());
    const group = document.querySelector('.theme-switch[role="group"]');
    expect(group).toHaveAttribute("aria-label", "Color theme");
  });

  it("defaults to system and reflects it via aria-pressed", () => {
    render(() => <ThemeSwitcher />);
    expect(choice("system")).toHaveAttribute("aria-pressed", "true");
    expect(choice("dark")).toHaveAttribute("aria-pressed", "false");
    expect(document.documentElement.dataset.theme).toBe("system");
  });

  it("persists a chosen mode to localStorage and stamps the root attributes", () => {
    render(() => <ThemeSwitcher />);
    fireEvent.click(choice("dark"));
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(choice("dark")).toHaveAttribute("aria-pressed", "true");
    expect(choice("system")).toHaveAttribute("aria-pressed", "false");
    // data-system-theme tracks prefers-color-scheme; without matchMedia
    // support (jsdom) it resolves light.
    expect(document.documentElement.dataset.systemTheme).toBe("light");
  });

  it("falls back to system for junk in storage (and for storage throws)", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "sparkle");
    expect(themeBootstrap().readMode()).toBe("system");
    delete window.__dittoDashboardTheme;
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(themeBootstrap().readMode()).toBe("system");
  });

  it("maps hours onto the five landing-page phases", () => {
    expect(fromHour(5)).toBe("dawn");
    expect(fromHour(7)).toBe("dawn");
    expect(fromHour(8)).toBe("morning");
    expect(fromHour(11)).toBe("morning");
    expect(fromHour(12)).toBe("afternoon");
    expect(fromHour(16)).toBe("afternoon");
    expect(fromHour(17)).toBe("dusk");
    expect(fromHour(19)).toBe("dusk");
    expect(fromHour(20)).toBe("night");
    expect(fromHour(4)).toBe("night");
  });

  it("derives the time phase from the clock and labels the time button with it", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 6, 31, 6, 30, 0));
    render(() => <ThemeSwitcher />);
    fireEvent.click(choice("time"));
    expect(document.documentElement.dataset.theme).toBe("time");
    expect(document.documentElement.dataset.timePhase).toBe("dawn");
    expect(document.getElementById("theme-time-label")).toHaveTextContent("Time · Dawn");
    // Away from time mode the button reads plain "Time" again.
    fireEvent.click(choice("light"));
    expect(document.getElementById("theme-time-label")).toHaveTextContent("Time");
  });

  it("re-derives the phase on the minute tick while in time mode", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 6, 31, 7, 59, 30));
    render(() => <ThemeSwitcher />);
    fireEvent.click(choice("time"));
    expect(document.documentElement.dataset.timePhase).toBe("dawn");
    vi.setSystemTime(new Date(2026, 6, 31, 8, 1, 0));
    vi.advanceTimersByTime(60_000);
    expect(document.documentElement.dataset.timePhase).toBe("morning");
    expect(document.getElementById("theme-time-label")).toHaveTextContent("Time · Morning");
  });

  it("exposes the index.html bootstrap contract on window", () => {
    render(() => <ThemeSwitcher />);
    const theme = window.__dittoDashboardTheme;
    expect(theme).toBeTruthy();
    expect(theme?.storageKey).toBe("ditto:dashboard-theme");
    expect(theme?.apply("dark")).toBe("dark");
    expect(theme?.apply("nonsense")).toBe("system");
  });
});
