import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { clearRunContext, setRunContext } from "./context.js";
import { loadAgentRuntime, loadMemoryRuntime } from "./openclaw-runtime.js";

const pluginDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../plugin");
const MEMORY_TOOL_NAMES = new Set([
  "search_memories",
  "search_subjects",
  "fetch_memories",
  "search_memories_in_subjects",
  "save_memory",
  "update_memory",
  "delete_memory",
]);

const NATIVE_RECALL_GUIDANCE =
  "Before answering anything about prior work, decisions, dates, people, preferences, or todos, use the supplied memory tools to search the user's imported OpenClaw memory files, then fetch focused details when needed. If recall remains low-confidence after search, say that you checked.";

function modelConfig(state, request) {
  const model = process.env.DITTOBENCH_MODEL || "qwen/qwen3-32b";
  const baseUrl = process.env.CHUTES_BASE_URL || process.env.OPENAI_BASE_URL;
  if (!baseUrl) throw new Error("CHUTES_BASE_URL or OPENAI_BASE_URL is required");
  const apiKey = process.env.CHUTES_API_KEY || process.env.OPENAI_API_KEY || "relay";
  const agentId = state.agentId(request.userId);
  return {
    models: {
      mode: "merge",
      providers: {
        dittobench: {
          baseUrl,
          apiKey,
          api: "openai-completions",
          models: [{
            id: model,
            name: model,
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 131072,
            maxTokens: 8192,
          }],
        },
      },
    },
    agents: {
      defaults: {
        model: { primary: `dittobench/${model}` },
        memorySearch: {
          enabled: true,
          provider: "none",
          fallback: "none",
          sources: ["memory"],
          sync: { onSessionStart: false, watch: false },
          query: {
            maxResults: Number(process.env.OPENCLAW_DITTOBENCH_SEARCH_LIMIT || 10),
          },
        },
      },
      list: [{ id: agentId, workspace: state.workspace(request.userId) }],
    },
    plugins: {
      enabled: true,
      allow: ["dittobench-wire", "memory-core"],
      load: { paths: [pluginDir] },
      entries: { "dittobench-wire": { enabled: true } },
      slots: { memory: "memory-core" },
    },
    tools: {
      allow: ["dittobench-wire"],
      deny: ["exec", "process", "read", "write", "edit", "apply_patch", "browser"],
      toolSearch: { enabled: false },
    },
  };
}

function textResult(result) {
  return (result.payloads ?? [])
    .map((payload) => payload.text)
    .filter((value) => typeof value === "string" && value.trim())
    .join("\n")
    .trim();
}

export class OpenClawRunner {
  constructor(state, dependencies = {}) {
    this.state = state;
    this.loadAgentRuntime = dependencies.loadAgentRuntime ?? loadAgentRuntime;
    this.loadMemoryRuntime = dependencies.loadMemoryRuntime ?? loadMemoryRuntime;
    this.fetch = dependencies.fetch ?? globalThis.fetch;
  }

  async configFor(request) {
    return modelConfig(this.state, request);
  }

  async sync(request) {
    const cfg = await this.configFor({ ...request, tools: request.tools ?? [] });
    const { getMemorySearchManager } = await this.loadMemoryRuntime();
    const resolved = await getMemorySearchManager({
      cfg,
      agentId: this.state.agentId(request.userId),
      purpose: "status",
    });
    if (!resolved.manager) throw new Error(resolved.error || "OpenClaw memory manager unavailable");
    await resolved.manager.sync?.({ reason: "dittobench-seed", force: true });
  }

  async preflight(request) {
    if (!request.toolEndpoint) throw new Error("preflight requires tool_endpoint");
    const started = performance.now();
    const args = { queries: ["dittobench reachability preflight"] };
    const response = await this.fetch(request.toolEndpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        case_id: request.caseId,
        user_id: request.userId,
        name: "search_web",
        args,
        hop: 0,
      }),
    });
    if (!response.ok) throw new Error(`preflight tool endpoint returned HTTP ${response.status}`);
    return {
      final_text: "DittoBench tool endpoint is reachable.",
      tool_calls: [{ name: "search_web", args, hop: 0 }],
      prompt_tokens: 0,
      output_tokens: 0,
      latency_ms: Math.round(performance.now() - started),
    };
  }

  async run(request) {
    if (request.caseId.startsWith("preflight:")) return this.preflight(request);

    await this.state.ensureUser(request.userId);
    const config = await this.configFor(request);
    const agentId = this.state.agentId(request.userId);
    const sessionId = `dittobench-${crypto.createHash("sha256").update(request.caseId).digest("hex").slice(0, 32)}`;
    const sessionKey = `agent:${agentId}:dittobench:${sessionId}`;
    const sessionFile = this.state.sessionFile(request.userId, request.caseId);
    await fs.rm(sessionFile, { force: true });
    await fs.mkdir(path.dirname(sessionFile), { recursive: true });

    const trace = [];
    setRunContext(sessionKey, {
      request,
      state: this.state,
      trace,
      memoryToolNames: MEMORY_TOOL_NAMES,
    });

    const { runEmbeddedAgent } = await this.loadAgentRuntime();
    const started = performance.now();
    try {
      const result = await runEmbeddedAgent({
        sessionId,
        sessionKey,
        agentId,
        trigger: "user",
        sessionFile,
        workspaceDir: this.state.workspace(request.userId),
        agentDir: this.state.agentDir(request.userId),
        config,
        provider: "dittobench",
        model: process.env.DITTOBENCH_MODEL || "qwen/qwen3-32b",
        prompt: request.userInput,
        transcriptPrompt: request.userInput,
        extraSystemPrompt: [request.systemPrompt, NATIVE_RECALL_GUIDANCE].filter(Boolean).join("\n\n"),
        promptMode: "minimal",
        bootstrapContextMode: "lightweight",
        skillsSnapshot: { prompt: "", skills: [] },
        toolsAllow: request.tools.map((tool) => tool.name),
        thinkLevel: "off",
        reasoningLevel: "off",
        verboseLevel: "off",
        fastMode: true,
        timeoutMs: Number(process.env.OPENCLAW_DITTOBENCH_TIMEOUT_MS || 120000),
        runTimeoutOverrideMs: Number(process.env.OPENCLAW_DITTOBENCH_TIMEOUT_MS || 120000),
        runId: crypto.randomUUID(),
        suppressToolErrorWarnings: true,
        suppressLiveStreamOutput: true,
      });
      const usage = result.meta?.agentMeta?.usage ?? {};
      return {
        final_text: textResult(result),
        tool_calls: trace,
        prompt_tokens: Number(usage.input || 0),
        output_tokens: Number(usage.output || 0),
        latency_ms: Math.round(performance.now() - started),
      };
    } finally {
      clearRunContext(sessionKey);
    }
  }
}
