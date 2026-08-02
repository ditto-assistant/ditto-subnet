import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { getRunContext } from "../src/context.js";

const require = createRequire(import.meta.url);
const openclawRoot = path.dirname(path.dirname(require.resolve("openclaw")));
let memoryRuntimePromise;

const TOOL_NAMES = [
  "create_image", "edit_image", "read_links", "search_web",
  "search_memories", "search_subjects", "fetch_memories", "search_memories_in_subjects",
  "artifacts", "execute_agent_job", "run_code", "search_tools",
  "execute_agent_workflow", "get_agent_job_status", "list_agent_jobs",
  "file_feedback_for_team", "set_theme", "set_main_model", "set_reasoning_effort",
  "set_chat_tool_preferences", "create_automation", "list_automations",
  "create_recipe", "apply_recipe", "discover_capabilities",
  "save_memory", "update_memory", "delete_memory",
  "calendar_create_event", "calendar_search_events", "gmail_send",
  "set_accent_color", "set_chat_font",
];

const MEMORY_READS = new Set([
  "search_memories", "search_subjects", "fetch_memories", "search_memories_in_subjects",
]);

function jsonResult(value) {
  return { content: [{ type: "text", text: JSON.stringify(value) }], details: value };
}

function queryFor(name, args) {
  if (name === "fetch_memories") return (args.pairIds ?? []).map(String).join(" OR ");
  const queries = args.queries ?? args.query ?? [];
  const query = Array.isArray(queries) ? queries.map(String).join(" OR ") : String(queries);
  if (name === "search_memories_in_subjects") {
    return [args.subject_id, query].filter(Boolean).join(" ");
  }
  return query;
}

async function memoryManager(cfg, agentId) {
  memoryRuntimePromise ??= import(
    pathToFileURL(path.join(openclawRoot, "dist/extensions/memory-core/runtime-api.js")).href
  );
  const { getMemorySearchManager } = await memoryRuntimePromise;
  const resolved = await getMemorySearchManager({ cfg, agentId });
  if (!resolved.manager) throw new Error(resolved.error || "OpenClaw memory manager unavailable");
  return resolved.manager;
}

async function searchNative(ctx, name, args) {
  const cfg = ctx.runtimeConfig ?? ctx.config;
  if (!cfg || !ctx.agentId) throw new Error("OpenClaw memory context is unavailable");
  const manager = await memoryManager(cfg, ctx.agentId);
  const query = queryFor(name, args);
  let results = await manager.search(query || "memory", {
    maxResults: Number(process.env.OPENCLAW_DITTOBENCH_SEARCH_LIMIT || 10),
    sources: ["memory"],
    sessionKey: ctx.sessionKey,
  });
  if (results.length === 0 && manager.sync) {
    await manager.sync({ reason: "search", force: true });
    results = await manager.search(query || "memory", {
      maxResults: Number(process.env.OPENCLAW_DITTOBENCH_SEARCH_LIMIT || 10),
      sources: ["memory"],
      sessionKey: ctx.sessionKey,
    });
  }
  return { results, engine: "openclaw-memory-core" };
}

async function forward(context, name, args, hop) {
  const endpoint = context.request.toolEndpoint;
  if (!endpoint) return { error: "no validator tool_endpoint was provided" };
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      case_id: context.request.caseId,
      user_id: context.request.userId,
      name,
      args,
      hop,
    }),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) return { error: `validator tool endpoint returned HTTP ${response.status}` };
  return response.json();
}

function createTool(name, ctx) {
  const context = getRunContext(ctx.sessionKey);
  const definition = context?.request.tools.find((tool) => tool.name === name);
  if (!context || !definition) return null;
  return {
    name,
    label: name,
    description: definition.description,
    parameters: definition.parameters,
    async execute(_callId, rawArgs) {
      const args = rawArgs && typeof rawArgs === "object" ? rawArgs : {};
      const hop = context.trace.length;
      context.trace.push({ name, args, hop });

      if (MEMORY_READS.has(name)) return jsonResult(await searchNative(ctx, name, args));
      if (name === "save_memory") {
        const pairId = await context.state.saveMemory(context.request.userId, String(args.content ?? ""));
        await (await memoryManager(ctx.runtimeConfig ?? ctx.config, ctx.agentId)).sync?.({ reason: "write", force: true });
        return jsonResult({ ok: true, pair_id: pairId });
      }
      if (name === "update_memory") {
        const pairId = await context.state.updateMemory(
          context.request.userId,
          String(args.pair_id ?? ""),
          String(args.content ?? ""),
        );
        await (await memoryManager(ctx.runtimeConfig ?? ctx.config, ctx.agentId)).sync?.({ reason: "write", force: true });
        return jsonResult({ ok: true, pair_id: pairId });
      }
      if (name === "delete_memory") {
        const deleted = await context.state.deleteMemory(context.request.userId, String(args.pair_id ?? ""));
        await (await memoryManager(ctx.runtimeConfig ?? ctx.config, ctx.agentId)).sync?.({ reason: "write", force: true });
        return jsonResult({ ok: true, deleted });
      }

      const result = await forward(context, name, args, hop);
      return jsonResult(result.error ? { error: result.error } : result.result ?? "");
    },
  };
}

export default definePluginEntry({
  id: "dittobench-wire",
  name: "DittoBench wire adapter",
  description: "Exposes the validator catalog through native OpenClaw tools.",
  register(api) {
    for (const name of TOOL_NAMES) {
      api.registerTool((ctx) => createTool(name, ctx), { names: [name] });
    }
  },
});
