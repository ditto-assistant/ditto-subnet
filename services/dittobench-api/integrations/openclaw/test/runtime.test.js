import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { OpenClawRunner } from "../src/runtime.js";
import { OpenClawState } from "../src/state.js";
import { loadAgentRuntime, loadMemoryRuntime } from "../src/openclaw-runtime.js";

test("installed OpenClaw exposes the native runtime entry points", async () => {
  const [agent, memory] = await Promise.all([loadAgentRuntime(), loadMemoryRuntime()]);
  assert.equal(typeof agent.runEmbeddedAgent, "function");
  assert.equal(typeof memory.getMemorySearchManager, "function");
});

test("native agent responds through a synthetic model endpoint", async (t) => {
  const server = http.createServer((request, response) => {
    request.resume();
    request.on("end", () => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      const chunks = [
        { delta: { role: "assistant", content: "synthetic answer" }, finish_reason: null },
        { delta: {}, finish_reason: "stop" },
      ];
      for (const chunk of chunks) {
        response.write(`data: ${JSON.stringify({
          id: "synthetic", object: "chat.completion.chunk", created: 1,
          model: "synthetic", choices: [{ index: 0, ...chunk }],
        })}\n\n`);
      }
      response.end("data: [DONE]\n\n");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "openclaw-native-run-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const previous = {
    OPENAI_BASE_URL: process.env.OPENAI_BASE_URL,
    OPENAI_API_KEY: process.env.OPENAI_API_KEY,
    CHUTES_BASE_URL: process.env.CHUTES_BASE_URL,
  };
  process.env.OPENAI_BASE_URL = `http://127.0.0.1:${server.address().port}/v1`;
  process.env.OPENAI_API_KEY = "synthetic";
  delete process.env.CHUTES_BASE_URL;
  t.after(() => {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  const result = await new OpenClawRunner(new OpenClawState(root)).run({
    caseId: "synthetic-case", userId: "synthetic-user", userInput: "Hello",
    systemPrompt: "Reply briefly", toolEndpoint: "http://127.0.0.1:9/tool",
    tools: [{ name: "search_memories", description: "search", parameters: {
      type: "object", properties: { query: { type: "string" } },
    } }],
  });
  assert.equal(result.final_text, "synthetic answer");
});

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
