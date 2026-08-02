import { afterEach, describe, expect, it, vi } from "vitest";

import { getJSON, poolMap, postJSON } from "./api";

type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

function brokenJsonResponse(init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    json: () => Promise.reject(new SyntaxError("Unexpected token")),
  } as unknown as Response;
}

function stubFetch(impl: FetchFn) {
  const mock = vi.fn(impl);
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("getJSON", () => {
  it("GETs API_BASE+path with the accept header and parses the body", async () => {
    const mock = stubFetch(() => Promise.resolve(jsonResponse({ miners: 4 })));
    const result = await getJSON<{ miners: number }>("/public/health");
    expect(result).toEqual({ miners: 4 });
    expect(mock).toHaveBeenCalledTimes(1);
    const call = mock.mock.calls[0];
    expect(call).toBeDefined();
    const [url, init] = call as Parameters<FetchFn>;
    expect(url).toBe("/api/v1/public/health");
    expect(init.headers).toEqual({ accept: "application/json" });
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("rejects with 'HTTP <status>' on a non-ok response", async () => {
    stubFetch(() => Promise.resolve(jsonResponse({ detail: "down" }, { ok: false, status: 503 })));
    await expect(getJSON("/public/health")).rejects.toThrow("HTTP 503");
  });

  it("propagates network errors", async () => {
    const failure = new TypeError("Failed to fetch");
    stubFetch(() => Promise.reject(failure));
    await expect(getJSON("/x")).rejects.toBe(failure);
  });

  it("cancels a request when its query signal becomes obsolete", async () => {
    const queryController = new AbortController();
    const mock = stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    );
    const pending = getJSON("/agent/old", queryController.signal);
    queryController.abort("route changed");
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    const call = mock.mock.calls[0];
    expect(call).toBeDefined();
    expect((call as Parameters<FetchFn>)[1].signal).toHaveProperty("aborted", true);
  });

  it("aborts the request after the 8s timeout", async () => {
    vi.useFakeTimers();
    stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    );
    const pending = getJSON("/slow");
    const guard = pending.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(8000);
    const error = await guard;
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe("AbortError");
  });

  it("clears the timeout once the response arrives", async () => {
    vi.useFakeTimers();
    stubFetch(() => Promise.resolve(jsonResponse({})));
    await getJSON("/fast");
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("postJSON", () => {
  it("POSTs a JSON body with both content-type and accept headers", async () => {
    const mock = stubFetch(() => Promise.resolve(jsonResponse({ status: "pending" })));
    const result = await postJSON<{ status: string }>("/public/agent/a1/dispute", {
      message: "m",
      signature: "s",
    });
    expect(result).toEqual({ status: "pending" });
    const call = mock.mock.calls[0];
    expect(call).toBeDefined();
    const [url, init] = call as Parameters<FetchFn>;
    expect(url).toBe("/api/v1/public/agent/a1/dispute");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({
      accept: "application/json",
      "content-type": "application/json",
    });
    expect(init.body).toBe(JSON.stringify({ message: "m", signature: "s" }));
  });

  it("surfaces the API's string detail on error responses", async () => {
    stubFetch(() =>
      Promise.resolve(jsonResponse({ detail: "Signature invalid." }, { ok: false, status: 400 })),
    );
    await expect(postJSON("/p", {})).rejects.toThrow("Signature invalid.");
  });

  it("falls back to a generic message when detail is not a string", async () => {
    stubFetch(() => Promise.resolve(jsonResponse({ detail: 42 }, { ok: false, status: 400 })));
    await expect(postJSON("/p", {})).rejects.toThrow("Request failed (HTTP 400).");
  });

  it("tolerates a non-JSON error body", async () => {
    stubFetch(() => Promise.resolve(brokenJsonResponse({ ok: false, status: 500 })));
    await expect(postJSON("/p", {})).rejects.toThrow("Request failed (HTTP 500).");
  });

  it("tolerates a null error body", async () => {
    stubFetch(() => Promise.resolve(jsonResponse(null, { ok: false, status: 422 })));
    await expect(postJSON("/p", {})).rejects.toThrow("Request failed (HTTP 422).");
  });

  it("coerces a non-JSON success body to an empty object", async () => {
    stubFetch(() => Promise.resolve(brokenJsonResponse()));
    await expect(postJSON("/p", {})).resolves.toEqual({});
  });

  it("propagates network errors", async () => {
    const failure = new TypeError("Failed to fetch");
    stubFetch(() => Promise.reject(failure));
    await expect(postJSON("/p", {})).rejects.toBe(failure);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe("poolMap", () => {
  it("resolves an empty item list to an empty array", async () => {
    const worker = vi.fn(() => Promise.resolve(1));
    await expect(poolMap([], 4, worker)).resolves.toEqual([]);
    expect(worker).not.toHaveBeenCalled();
  });

  it("preserves input order even when workers finish out of order", async () => {
    const gates = [deferred<void>(), deferred<void>(), deferred<void>()];
    const result = poolMap([0, 1, 2], 3, (i) =>
      (gates[i] as { promise: Promise<void> }).promise.then(() => i * 10),
    );
    gates[2]?.resolve();
    gates[0]?.resolve();
    gates[1]?.resolve();
    await expect(result).resolves.toEqual([0, 10, 20]);
  });

  it("never runs more than `limit` workers at once", async () => {
    const items = [0, 1, 2, 3, 4];
    const gates = items.map(() => deferred<void>());
    const started: number[] = [];
    const result = poolMap(items, 2, (i) => {
      started.push(i);
      return (gates[i] as { promise: Promise<void> }).promise.then(() => i);
    });
    // Workers start synchronously up to the limit.
    expect(started).toEqual([0, 1]);
    gates[1]?.resolve();
    await flush();
    expect(started).toEqual([0, 1, 2]);
    gates[0]?.resolve();
    await flush();
    expect(started).toEqual([0, 1, 2, 3]);
    gates[2]?.resolve();
    gates[3]?.resolve();
    await flush();
    expect(started).toEqual([0, 1, 2, 3, 4]);
    gates[4]?.resolve();
    await expect(result).resolves.toEqual([0, 1, 2, 3, 4]);
  });

  it("caps the runner count at the item count when limit exceeds it", async () => {
    const worker = vi.fn((i: number) => Promise.resolve(i + 1));
    await expect(poolMap([5, 6], 10, worker)).resolves.toEqual([6, 7]);
    expect(worker).toHaveBeenCalledTimes(2);
  });

  it("rejects when a worker rejects", async () => {
    const failure = new Error("worker failed");
    await expect(
      poolMap([1, 2], 2, (i) => (i === 2 ? Promise.reject(failure) : Promise.resolve(i))),
    ).rejects.toBe(failure);
  });
});
