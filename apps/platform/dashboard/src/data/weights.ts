import { createRoot } from "solid-js";

import type { ResourceState } from "./useEndpoint";
import { useEndpoint } from "./useEndpoint";
import type { WeightsSnapshot } from "../types/leaderboard";

let resource: ResourceState<WeightsSnapshot> | null = null;
let fetchIdentity: typeof globalThis.fetch | null = null;

/**
 * One `/public/weights` resource per dashboard instance; every consumer shares it.
 *
 * Three independent callers had grown around this endpoint — the shell (for the
 * rail's payout clock), the leaderboard store (for the matrix panel and the
 * per-row weight chips), and the fleet page — each with its own poll timer, so
 * a viewer on Fleet issued three requests per cadence for one body. The caches
 * in front of it meant that cost almost nothing (most landed on the browser's
 * own `max-age=30`, and none ever reached Subtensor), which is exactly why it
 * went unnoticed. It is still three times the requests for one answer, and it
 * left three copies that could report different blocks to different parts of
 * the same screen.
 *
 * Deliberately carries no `pollMs`: the shell's master tick already refreshes
 * it every `REFRESH_MS`, and a timer here would race that one instead of
 * replacing it. Same shape as `operationsResource`.
 */
export function weightsResource(): ResourceState<WeightsSnapshot> {
  // Vitest swaps fetch fixtures between cases. Production keeps one fetch
  // identity for the page lifetime, so this only resets isolated test roots.
  if (fetchIdentity !== globalThis.fetch) {
    fetchIdentity = globalThis.fetch;
    resource = null;
  }
  if (!resource) {
    resource = createRoot(() => useEndpoint<WeightsSnapshot>("/public/weights"));
  }
  return resource;
}
