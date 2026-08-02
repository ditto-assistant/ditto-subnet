// Recorded public-API payloads for tests (see fixtures/README.md). The
// fixture set is one coherent production snapshot: leaderboard entries,
// activity rows, and the per-agent drilldowns reference the same agents.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Agent id of the leaderboard's top entry at capture time. */
export const FIXTURE_TOP_AGENT_ID = "c77140d8-3ac5-42c3-8f5c-a99cd283365d";
/** A rejected submission's agent id (thin pipeline, no scores). */
export const FIXTURE_REJECTED_AGENT_ID = "8a5d27ee-6ed4-45d4-aaa4-e73957eb8217";

// Resolved with path math rather than `new URL(rel, import.meta.url)`: Vite
// statically rewrites that pattern into a server-root asset URL, which under
// vitest resolves to file:///fixtures/… and breaks every fixture read.
const FIXTURES_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures");

export function loadFixture<T = unknown>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURES_DIR, `${name}.json`), "utf-8")) as T;
}

/** Resolve an API path (as passed to getJSON, no API base) to a fixture name,
 * or null for paths the fixture set does not cover. */
export function fixtureNameFor(path: string): string | null {
  const [pathname, query = ""] = path.split("?", 2) as [string, string?];
  const params = new URLSearchParams(query);
  switch (pathname) {
    case "/public/health":
      return "health";
    case "/public/operations":
      return "operations";
    case "/public/weights":
      return "weights";
    case "/public/validator-names":
      return "validator-names";
    case "/public/screeners":
      return "screeners";
    case "/public/bench/glossary":
      return "bench-glossary";
    case "/public/bench/config":
      return "bench-config";
    case "/public/bench/rollout":
      return "bench-rollout";
    case "/public/bench/timeline":
      return "bench-timeline";
    case "/public/leaderboard": {
      const version = params.get("bench_version");
      return version === null || version === "7" ? "leaderboard" : "leaderboard-v6";
    }
    case "/public/activity": {
      if (params.get("review") === "ath") return "activity-ath";
      if (params.get("q")) return "activity-single";
      return "activity";
    }
    default: {
      const agent = /^\/public\/agent\/([^/]+)\/(pipeline|scores|summary)$/.exec(pathname);
      if (!agent) return null;
      const [, id, kind] = agent;
      if (kind === "scores") return id === FIXTURE_TOP_AGENT_ID ? "agent-top-scores" : null;
      // Only the two recorded agents have a summary: an unknown id must 404
      // the way the endpoint does, which is the deep-link failure path.
      if (kind === "summary") {
        if (id === FIXTURE_TOP_AGENT_ID) return "agent-top-summary";
        return id === FIXTURE_REJECTED_AGENT_ID ? "agent-rejected-summary" : null;
      }
      return id === FIXTURE_TOP_AGENT_ID ? "agent-top-pipeline" : "agent-rejected-pipeline";
    }
  }
}

/** Stub global fetch to serve fixtures for API paths (any API base, same-origin
 * or absolute). Returns a restore function. */
export function installFixtureFetch(): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://fixtures.test");
    const path = url.pathname.replace(/^.*?(?=\/public\/)/, "") + url.search;
    const name = path.startsWith("/public/") ? fixtureNameFor(path) : null;
    if (name === null) {
      return Promise.resolve(
        new Response(JSON.stringify({ detail: "no fixture for " + raw }), { status: 404 }),
      );
    }
    return Promise.resolve(
      new Response(JSON.stringify(loadFixture(name)), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}
