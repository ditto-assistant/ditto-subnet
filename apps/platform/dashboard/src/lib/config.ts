// Boot-time config resolution. All values resolve once, from the real query
// string and the document's <meta> tags. The boot snapshot is the ONLY source
// URL minting reads config from, so stray query junk is never carried forward.

/** Snapshot of location.search at boot. */
export const bootParams: URLSearchParams = new URLSearchParams(location.search);

/** Trimmed content of a `<meta name=…>` tag, "" if absent. */
export function meta(name: string): string {
  const el = document.querySelector('meta[name="' + name + '"]');
  return el ? (el.getAttribute("content") || "").trim() : "";
}

/** API base URL, trailing slash stripped; precedence ?api > meta tag > same-origin default. */
export const API_BASE: string = (
  bootParams.get("api") ||
  meta("ditto:api-base") ||
  "/api/v1"
).replace(/\/$/, "");

/** W&B link target (sidebar + footer links). */
export const WANDB_URL: string =
  bootParams.get("wandb") || meta("ditto:wandb-url") || "https://wandb.ai/";

/** Poll interval; matches the API's Cache-Control max-age. */
export const REFRESH_MS = 30_000;

/** Fast-poll cadence for the shared operations snapshot while the viewer is
 * on the operations page (8s → 5s in the weekend drift, monolith 10253). */
export const OPS_REFRESH_MS = 5_000;
