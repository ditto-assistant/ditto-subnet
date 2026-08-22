import assert from "node:assert/strict";
import test from "node:test";

import tombstone from "./cloudflare-pages-tombstone.mjs";
import worker from "./cloudflare-pages-worker.mjs";

test("reports an explicit untrusted preview health mode", async () => {
  const response = await worker.fetch(
    new Request("https://pr-12.example.pages.dev/__preview/health"),
    {},
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    status: "ok",
    host: "pr-12.example.pages.dev",
    mode: "untrusted-read-only-dashboard-preview",
  });
  assert.match(response.headers.get("content-security-policy"), /connect-src 'self'/);
  assert.match(response.headers.get("content-security-policy"), /form-action 'none'/);
});

test("serves static assets behind trusted browser restrictions", async () => {
  const response = await worker.fetch(new Request("https://preview.example/leaderboard"), {
    ASSETS: { fetch: async () => new Response("asset") },
  });
  assert.equal(await response.text(), "asset");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  assert.equal(response.headers.get("x-frame-options"), "DENY");
});

test("rejects production mutations and non-public API paths", async () => {
  const mutation = await worker.fetch(
    new Request("https://preview.example/api/v1/public/agent/123/dispute", { method: "POST" }),
    {},
  );
  assert.equal(mutation.status, 405);
  assert.equal(mutation.headers.get("allow"), "GET, HEAD");

  const privateRead = await worker.fetch(
    new Request("https://preview.example/api/v1/admin/submissions"),
    {},
  );
  assert.equal(privateRead.status, 404);
});

test("forwards only allowlisted headers to the production public API", async () => {
  const originalFetch = globalThis.fetch;
  let forwarded;
  globalThis.fetch = async (input, init) => {
    forwarded = { input: String(input), init };
    return new Response('{"ok":true}', {
      headers: { "content-type": "application/json", "set-cookie": "secret=bad" },
    });
  };
  try {
    const response = await worker.fetch(
      new Request("https://preview.example/api/v1/public/health?detail=1", {
        headers: {
          accept: "application/json",
          authorization: "Bearer must-not-forward",
          cookie: "session=must-not-forward",
        },
      }),
      {},
    );
    assert.equal(response.status, 200);
    assert.equal(response.headers.has("set-cookie"), false);
    assert.equal(response.headers.get("x-ditto-preview-api"), "production-public-read-only");
    assert.equal(
      forwarded.input,
      "https://platform-api.heyditto.ai/api/v1/public/health?detail=1",
    );
    assert.equal(forwarded.init.method, "GET");
    assert.equal(forwarded.init.headers.get("authorization"), null);
    assert.equal(forwarded.init.headers.get("cookie"), null);
    assert.equal(forwarded.init.headers.get("accept"), "application/json");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("retires the stable branch alias with a no-store tombstone", async () => {
  const response = await tombstone.fetch();
  assert.equal(response.status, 410);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.match(await response.text(), /retired/);
});
