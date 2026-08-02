// Four-mode theme switcher (system | light | dark | time), the port of the
// sidebar switcher IIFE (monolith 3044–3084). The pre-paint bootstrap in
// index.html owns first paint and exposes its contract as
// window.__dittoDashboardTheme (storage key "ditto:dashboard-theme", the
// fromHour time phases, apply() stamping data-theme / data-system-theme /
// data-time-phase on <html>); this component drives that same contract and
// installs an identical fallback when the bootstrap is absent (tests).
import { createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

export type ThemeMode = "system" | "light" | "dark" | "time";
export type TimePhase = "dawn" | "morning" | "afternoon" | "dusk" | "night";

export const THEME_STORAGE_KEY = "ditto:dashboard-theme";

export interface ThemeBootstrap {
  storageKey: string;
  fromHour: (hour: number) => string;
  readMode: () => string;
  apply: (mode: string) => string;
}

declare global {
  interface Window {
    __dittoDashboardTheme?: ThemeBootstrap;
  }
}

const MODES: Record<string, true> = { system: true, light: true, dark: true, time: true };

export function fromHour(hour: number): TimePhase {
  if (hour >= 5 && hour < 8) return "dawn";
  if (hour >= 8 && hour < 12) return "morning";
  if (hour >= 12 && hour < 17) return "afternoon";
  if (hour >= 17 && hour < 20) return "dusk";
  return "night";
}

function readMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    return saved !== null && MODES[saved] ? (saved as ThemeMode) : "system";
  } catch {
    return "system";
  }
}

function applyTheme(mode: string): string {
  const next = MODES[mode] ? mode : "system";
  const root = document.documentElement;
  root.dataset.theme = next;
  root.dataset.systemTheme =
    window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  root.dataset.timePhase = fromHour(new Date().getHours());
  return next;
}

/** The bootstrap contract; index.html's pre-paint copy wins when present. */
export function themeBootstrap(): ThemeBootstrap {
  if (!window.__dittoDashboardTheme) {
    window.__dittoDashboardTheme = {
      storageKey: THEME_STORAGE_KEY,
      fromHour,
      readMode,
      apply: applyTheme,
    };
    window.__dittoDashboardTheme.apply(readMode());
  }
  return window.__dittoDashboardTheme;
}

export function ThemeSwitcher(): JSX.Element {
  const theme = themeBootstrap();
  const [mode, setMode] = createSignal(document.documentElement.dataset.theme || "system");
  const [phase, setPhase] = createSignal(document.documentElement.dataset.timePhase || "afternoon");

  function sync(): void {
    setMode(document.documentElement.dataset.theme || "system");
    setPhase(document.documentElement.dataset.timePhase || "afternoon");
  }

  function choose(choice: ThemeMode): void {
    const next = theme.apply(choice);
    try {
      localStorage.setItem(theme.storageKey, next);
    } catch {
      // Storage is optional; the mode still applies for this page view.
    }
    sync();
  }

  onMount(() => {
    sync();
    // Track the OS appearance while in "system" mode.
    if (window.matchMedia) {
      const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = (): void => {
        if (document.documentElement.dataset.theme === "system") {
          theme.apply("system");
          sync();
        }
      };
      if (typeof systemTheme.addEventListener === "function") {
        systemTheme.addEventListener("change", onChange);
        onCleanup(() => systemTheme.removeEventListener("change", onChange));
      }
    }
    // Re-derive the phase every minute while in "time" mode.
    const timer = setInterval(() => {
      if (document.documentElement.dataset.theme === "time") {
        theme.apply("time");
        sync();
      }
    }, 60_000);
    onCleanup(() => clearInterval(timer));
  });

  const pressed = (choice: ThemeMode) => (mode() === choice ? "true" : "false");
  const timeLabel = () =>
    mode() === "time" ? "Time · " + phase().charAt(0).toUpperCase() + phase().slice(1) : "Time";

  return (
    <div class="side-theme">
      <div class="theme-switch" role="group" aria-label="Color theme">
        <button
          class="theme-option"
          type="button"
          data-theme-choice="system"
          aria-pressed={pressed("system")}
          title="Follow your system light or dark appearance"
          onClick={() => choose("system")}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            aria-hidden="true"
          >
            <rect x="3" y="4" width="18" height="13" rx="2" />
            <path d="M8 21h8M12 17v4" />
          </svg>
          <span>System</span>
        </button>
        <button
          class="theme-option"
          type="button"
          data-theme-choice="light"
          aria-pressed={pressed("light")}
          title="Always use the light paper theme"
          onClick={() => choose("light")}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="3.5" />
            <path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42" />
          </svg>
          <span>Light</span>
        </button>
        <button
          class="theme-option"
          type="button"
          data-theme-choice="dark"
          aria-pressed={pressed("dark")}
          title="Always use the dark ink-blue theme"
          onClick={() => choose("dark")}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            aria-hidden="true"
          >
            <path d="M20.5 15.4A8.5 8.5 0 0 1 8.6 3.5 8.5 8.5 0 1 0 20.5 15.4Z" />
          </svg>
          <span>Dark</span>
        </button>
        <button
          class="theme-option"
          type="button"
          data-theme-choice="time"
          aria-pressed={pressed("time")}
          title="Follow the landing page theme for the current time of day"
          onClick={() => choose("time")}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7v5l3 2" />
          </svg>
          <span id="theme-time-label">{timeLabel()}</span>
        </button>
      </div>
    </div>
  );
}
