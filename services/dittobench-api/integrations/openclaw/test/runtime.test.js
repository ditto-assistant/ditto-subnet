import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { OpenClawRunner } from "../src/runtime.js";
import { OpenClawState } from "../src/state.js";

test("preflight reaches the validator callback without a model", async () => {
  const calls = [];
  const runner = new OpenClawRunner({}, {
    fetch: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return { ok: true, status: 200 };
    },
  });
  const result = await runner.preflight({
    caseId: "preflight:test", userId: "alice", toolEndpoint: "http://validator/tool",
  });
  assert.equal(calls[0].body.name, "search_web");
  assert.equal(result.tool_calls[0].hop, 0);
});

test("run uses the native embedded agent entry point and locked catalog", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "openclaw-runner-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const state = new OpenClawState(root);
  const calls = [];
  const runner = new OpenClawRunner(state, {
    loadAgentRuntime: async () => ({
      runEmbeddedAgent: async (params) => {
        calls.push(params);
        return {
          payloads: [{ text: "native answer" }],
          meta: { agentMeta: { usage: { input: 12, output: 4 } } },
        };
      },
    }),
  });
  const previous = process.env.OPENAI_BASE_URL;
  process.env.OPENAI_BASE_URL = "http://relay/v1";
  t.after(() => {
    if (previous === undefined) delete process.env.OPENAI_BASE_URL;
    else process.env.OPENAI_BASE_URL = previous;
  });
  const result = await runner.run({
    caseId: "case-1",
    userId: "alice",
    systemPrompt: "benchmark system",
    userInput: "what do you remember?",
    toolEndpoint: "http://validator/tool",
    tools: [{ name: "search_memories", description: "search", parameters: { type: "object" } }],
  });
  assert.equal(result.final_text, "native answer");
  assert.equal(result.prompt_tokens, 12);
  assert.deepEqual(calls[0].toolsAllow, ["search_memories"]);
  assert.match(calls[0].extraSystemPrompt, /benchmark system/);
  assert.match(calls[0].extraSystemPrompt, /prior work/);
  assert.equal(calls[0].thinkLevel, "off");
});
