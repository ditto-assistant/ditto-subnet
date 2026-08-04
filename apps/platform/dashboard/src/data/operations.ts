import { createRoot } from "solid-js";

import type { ResourceState } from "./useEndpoint";
import { useEndpoint } from "./useEndpoint";
import type { OperationsPayload } from "../types/fleet";

let resource: ResourceState<OperationsPayload> | null = null;
let fetchIdentity: typeof globalThis.fetch | null = null;

/** One operations resource per dashboard instance; every consumer shares it. */
export function operationsResource(): ResourceState<OperationsPayload> {
  // Vitest swaps fetch fixtures between cases. Production keeps one fetch
  // identity for the page lifetime, so this only resets isolated test roots.
  if (fetchIdentity !== globalThis.fetch) {
    fetchIdentity = globalThis.fetch;
    resource = null;
  }
  if (!resource) {
    resource = createRoot(() => useEndpoint<OperationsPayload>("/public/operations"));
  }
  return resource;
}
