import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const entry = require.resolve("openclaw");
export const openclawRoot = path.dirname(path.dirname(entry));

let memoryRuntimePromise;
export async function loadMemoryRuntime() {
  memoryRuntimePromise ??= import(
    pathToFileURL(path.join(openclawRoot, "dist/extensions/memory-core/runtime-api.js")).href
  );
  return memoryRuntimePromise;
}

export async function loadAgentRuntime() {
  return import("openclaw/extension-api");
}
