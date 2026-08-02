import { QueryClient } from "@tanstack/solid-query";

import { HTTPError } from "../lib/api";

/**
 * Public dashboard query cache.
 *
 * API responses are cacheable for 10-30 seconds. Keep a short matching
 * in-memory freshness window, retain inactive entity records long enough for
 * back/forward navigation, and retry one transient failure. Unlike ditto-app,
 * this public surface does not persist data across browser sessions.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      gcTime: 5 * 60_000,
      retry: (failureCount, error) =>
        failureCount < 1 &&
        !(error instanceof HTTPError && error.status < 500) &&
        !(error instanceof DOMException && error.name === "AbortError"),
      retryDelay: 250,
      refetchOnWindowFocus: false,
    },
  },
});

export const publicQueryKeys = {
  agentSummary: (agentId: string) => ["public", "agent", agentId, "summary"] as const,
  agentPipeline: (agentId: string) => ["public", "agent", agentId, "pipeline"] as const,
};
