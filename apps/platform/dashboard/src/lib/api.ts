// All network access goes through here: GET/POST against the configured API
// base with an 8s abort timeout, plus a small promise pool for fan-out.

import { API_BASE } from "./config";
import { sharedOperationsJSON } from "../data/operations-cache";

const TIMEOUT_MS = 8000;

export class HTTPError extends Error {
  constructor(public readonly status: number) {
    super("HTTP " + status);
    this.name = "HTTPError";
  }
}

/** GET API_BASE+path as JSON. Non-2xx rejects with `Error("HTTP <status>")`.
 * A caller signal (Solid Query supplies one per query) cancels obsolete route
 * reads; the local controller still enforces the dashboard-wide deadline. */
async function fetchJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const ctrl = new AbortController();
  const abortFromCaller = (): void => ctrl.abort(signal?.reason);
  if (signal?.aborted) abortFromCaller();
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  const to = setTimeout(() => {
    ctrl.abort();
  }, TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(API_BASE + path, {
      signal: ctrl.signal,
      headers: { accept: "application/json" },
    });
  } finally {
    // Cleared on both fulfillment and rejection; the timer only guards the
    // fetch itself, not the body parse.
    clearTimeout(to);
    signal?.removeEventListener("abort", abortFromCaller);
  }
  if (!response.ok) throw new HTTPError(response.status);
  return (await response.json()) as T;
}

export function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (path === "/public/operations") {
    // The payload is public and user-agnostic. One same-origin tab performs
    // each refresh; every other loaded dashboard reuses its broadcast result.
    // The shared request owns its timeout so one component abort cannot cancel
    // the snapshot all other tabs are waiting for.
    return sharedOperationsJSON(() => fetchJSON<T>(path));
  }
  return fetchJSON<T>(path, signal);
}

/** The API's typed error detail when it sent one, else a generic message. */
function errorDetail(data: unknown, status: number): string {
  if (typeof data === "object" && data !== null) {
    const detail = (data as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return "Request failed (HTTP " + status + ").";
}

/** POST a JSON body to API_BASE+path. The response body is parsed even on
 * error responses (tolerating non-JSON bodies) so the API's `detail` message
 * can surface to the caller. */
export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const ctrl = new AbortController();
  const to = setTimeout(() => {
    ctrl.abort();
  }, TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(API_BASE + path, {
      method: "POST",
      signal: ctrl.signal,
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } finally {
    clearTimeout(to);
  }
  const data: unknown = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorDetail(data, response.status));
  return data as T;
}

/** Run `worker` over `items` with at most `limit` promises in flight;
 * results preserve input order. */
export function poolMap<T, R>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = [];
  results.length = items.length;
  let cursor = 0;
  function step(): Promise<void> {
    if (cursor >= items.length) return Promise.resolve();
    const i = cursor;
    cursor += 1;
    return worker(items[i] as T).then((value) => {
      results[i] = value;
      return step();
    });
  }
  const runners: Array<Promise<void>> = [];
  for (let k = 0; k < Math.min(limit, items.length); k += 1) runners.push(step());
  return Promise.all(runners).then(() => results);
}
