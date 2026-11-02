import { Show, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { authJSON } from "../../lib/api";
import { sessionAuthHeader } from "../../stores/sessionStore";

export function SubmissionDownload(props: {
  agentId: string;
  name: string;
  canDownload: boolean;
}): JSX.Element {
  const [busy, setBusy] = createSignal(false);
  const [status, setStatus] = createSignal("");
  const [failed, setFailed] = createSignal(false);

  async function download(): Promise<void> {
    if (busy() || !props.canDownload) return;
    setBusy(true);
    setStatus("");
    setFailed(false);
    try {
      const body = await authJSON<{ download_url: string }>(
        "/me/agents/" + encodeURIComponent(props.agentId) + "/artifact",
        { headers: sessionAuthHeader(), cache: "no-store" },
      );
      if (typeof body.download_url !== "string" || !body.download_url) {
        throw new Error("The download link is missing. Try again.");
      }
      // Same-tab attachment navigation also works after async requests in Safari.
      location.assign(body.download_url);
      setStatus("Download started. The private link expires in five minutes.");
    } catch (err) {
      setFailed(true);
      setStatus(
        err instanceof DOMException && err.name === "AbortError"
          ? "The download request timed out. Try again."
          : err instanceof Error
            ? err.message
            : "Could not prepare the download. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="account-submission-download">
      <button
        type="button"
        class="btn ghost"
        aria-label={"Download submitted source for " + props.name}
        disabled={busy() || !props.canDownload}
        onClick={() => void download()}
      >
        {busy() ? "Preparing download…" : "Download"}
      </button>
      <Show when={!props.canDownload}>
        <p class="muted">Sign in again with Download enabled to get your source.</p>
      </Show>
      <Show when={status()}>
        <p class={failed() ? "account-error" : "muted"} role="status" aria-live="polite">
          {status()}
        </p>
      </Show>
    </div>
  );
}
