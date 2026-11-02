// Theme controls for the rail: the four-mode switcher (system | light | dark
// | time), the port of the sidebar switcher IIFE (monolith 3044–3084), plus
// the brand-kit palette picker (Carbon, Parchment, Signal, Vermilion, Tide).
// The pre-paint bootstrap in index.html owns first paint and exposes its
// contract as window.__dittoDashboardTheme (storage keys
// "ditto:dashboard-theme" and "ditto:dashboard-palette", the fromHour time
// phases, apply() stamping data-theme / data-system-theme / data-time-phase
// and the resolved kit attributes data-ditto-theme / data-ditto-mode on
// <html>); this component drives that same contract and installs an
// identical fallback when the bootstrap is absent (tests).
import { For, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

export type ThemeMode = "system" | "light" | "dark" | "time";
export type TimePhase = "dawn" | "morning" | "afternoon" | "dusk" | "night";
export type Palette = "carbon" | "parchment" | "signal" | "vermilion" | "tide";

export const THEME_STORAGE_KEY = "ditto:dashboard-theme";
export const PALETTE_STORAGE_KEY = "ditto:dashboard-palette";
export const DEFAULT_PALETTE: Palette = "carbon";

export interface ThemeBootstrap {
  storageKey: string;
  paletteStorageKey: string;
  fromHour: (hour: number) => string;
  readMode: () => string;
  readPalette: () => string;
  apply: (mode: string) => string;
  applyPalette: (palette: string) => string;
}

declare global {
  interface Window {
    __dittoDashboardTheme?: ThemeBootstrap;
  }
}

const MODES: Record<string, true> = { system: true, light: true, dark: true, time: true };
const PALETTES: Record<string, true> = {
  carbon: true,
  parchment: true,
  signal: true,
  vermilion: true,
  tide: true,
};

/** The five kit palettes, in the kit's own order, with their picker labels. */
export const PALETTE_OPTIONS: ReadonlyArray<{ id: Palette; label: string; title: string }> = [
  { id: "carbon", label: "Carbon", title: "Carbon: graphite and off-white" },
  { id: "parchment", label: "Parchment", title: "Parchment: warm paper and sepia" },
  { id: "signal", label: "Signal", title: "Signal: green on graphite" },
  { id: "vermilion", label: "Vermilion", title: "Vermilion: red-orange on slate" },
  { id: "tide", label: "Tide", title: "Tide: cyan and ink" },
];

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

function readPalette(): Palette {
  try {
    const saved = localStorage.getItem(PALETTE_STORAGE_KEY);
    return saved !== null && PALETTES[saved] ? (saved as Palette) : DEFAULT_PALETTE;
  } catch {
    return DEFAULT_PALETTE;
  }
}

function applyTheme(mode: string): string {
  const next = MODES[mode] ? mode : "system";
  const root = document.documentElement;
  const systemDark = Boolean(
    window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
  const phase = fromHour(new Date().getHours());
  root.dataset.theme = next;
  root.dataset.systemTheme = systemDark ? "dark" : "light";
  root.dataset.timePhase = phase;
  // The kit scopes its palettes on the resolved mode, never on "system".
  const dark =
    next === "dark" || (next === "system" && systemDark) || (next === "time" && phase === "night");
  root.dataset.dittoMode = dark ? "dark" : "light";
  if (!root.dataset.dittoTheme) root.dataset.dittoTheme = readPalette();
  return next;
}

function applyPalette(palette: string): string {
  const next = PALETTES[palette] ? palette : DEFAULT_PALETTE;
  document.documentElement.dataset.dittoTheme = next;
  return next;
}

/** The bootstrap contract; index.html's pre-paint copy wins when present. */
export function themeBootstrap(): ThemeBootstrap {
  if (!window.__dittoDashboardTheme) {
    window.__dittoDashboardTheme = {
      storageKey: THEME_STORAGE_KEY,
      paletteStorageKey: PALETTE_STORAGE_KEY,
      fromHour,
      readMode,
      readPalette,
      apply: applyTheme,
      applyPalette,
    };
    window.__dittoDashboardTheme.applyPalette(readPalette());
    window.__dittoDashboardTheme.apply(readMode());
  }
  return window.__dittoDashboardTheme;
}

export function ThemeSwitcher(): JSX.Element {
  const theme = themeBootstrap();
  const root = document.documentElement;
  const [mode, setMode] = createSignal(root.dataset.theme || "system");
  const [phase, setPhase] = createSignal(root.dataset.timePhase || "afternoon");
  const [palette, setPalette] = createSignal(root.dataset.dittoTheme || DEFAULT_PALETTE);
  const [resolved, setResolved] = createSignal(root.dataset.dittoMode || "light");

  function sync(): void {
    setMode(root.dataset.theme || "system");
    setPhase(root.dataset.timePhase || "afternoon");
    setPalette(root.dataset.dittoTheme || DEFAULT_PALETTE);
    setResolved(root.dataset.dittoMode || "light");
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

  function choosePalette(choice: Palette): void {
    const next = theme.applyPalette(choice);
    try {
      localStorage.setItem(theme.paletteStorageKey, next);
    } catch {
      // Storage is optional; the palette still applies for this page view.
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
          title="Always use the light mode of the chosen palette"
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
          title="Always use the dark mode of the chosen palette"
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
          title="Follow the time of day: warm at dawn and dusk, dark at night"
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
      <div class="palette-switch" role="group" aria-label="Color palette">
        <For each={PALETTE_OPTIONS}>
          {(option) => (
            <button
              class="palette-option"
              type="button"
              data-palette-choice={option.id}
              aria-pressed={palette() === option.id ? "true" : "false"}
              aria-label={option.label}
              title={option.title}
              onClick={() => choosePalette(option.id)}
            >
              {/* The swatch previews the palette in the currently resolved
                  mode, so the row reads as five variants of one appearance. */}
              <span
                class="palette-swatch"
                data-ditto-theme={option.id}
                data-ditto-mode={resolved()}
                aria-hidden="true"
              />
            </button>
          )}
        </For>
      </div>
    </div>
  );
}
