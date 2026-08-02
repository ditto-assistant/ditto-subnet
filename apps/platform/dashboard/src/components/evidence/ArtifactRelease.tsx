// The "Submitted source" release card + the five-minute signed download
// (monolith renderArtifactRelease 3594–3607, downloadArtifact 3609–3662,
// #278). The download navigates same-tab: a synthetic _blank click after the
// async fetch is popup-blocked in Safari, and `download` is ignored
// cross-origin. The tarball URL answers with an attachment, so the page
// stays put.
import { Show, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { API_BASE } from "../../lib/config";
import { artifactReleaseCopy } from "../pipeline/artifact-release";
import type { ArtifactRelease } from "../pipeline/artifact-release";

interface DownloadState {
  busy: boolean;
  status: string;
  error: boolean;
}

export function ArtifactReleaseCard(props: {
  release: ArtifactRelease | null | undefined;
  agentId: string;
}): JSX.Element {
  const copy = () => artifactReleaseCopy(props.release);
  const [download, setDownload] = createSignal<DownloadState>({
    busy: false,
    status: "",
    error: false,
  });

  function downloadArtifact(): void {
    if (!props.agentId || download().busy) return;
    setDownload({ busy: true, status: "Creating a private five-minute link…", error: false });
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 8000);
    fetch(API_BASE + "/public/agent/" + encodeURIComponent(props.agentId) + "/artifact", {
      signal: ctrl.signal,
      cache: "no-store",
      headers: { accept: "application/json" },
    })
      .then(
        (response) => {
          clearTimeout(to);
          return (response.json() as Promise<Record<string, unknown>>)
            .catch(() => ({}) as Record<string, unknown>)
            .then((data) => {
              if (!response.ok) {
                const message =
                  typeof data.message === "string"
                    ? data.message
                    : typeof data.detail === "string"
                      ? data.detail
                      : "Source is not available yet (HTTP " + response.status + ").";
                throw new Error(message);
              }
              return data;
            });
        },
        (error: unknown) => {
          clearTimeout(to);
          throw error;
        },
      )
      .then((data) => {
        if (typeof data.download_url !== "string" || !data.download_url) {
          throw new Error("The download credential was missing.");
        }
        window.location.assign(data.download_url);
        setDownload({
          busy: false,
          status: "Download started · the private link expires in five minutes.",
          error: false,
        });
      })
      .catch((error: unknown) => {
        const aborted = error instanceof DOMException && error.name === "AbortError";
        const message = error instanceof Error ? error.message : "";
        setDownload({
          busy: false,
          status: aborted
            ? "The download request timed out. Try again."
            : message || "Could not prepare the download. Try again.",
          error: true,
        });
      });
  }

  return (
    <Show when={copy()}>
      {(c) => (
        <section class="artifact-release-card" aria-label="Submitted source release">
          <div class="artifact-release-head">
            <h4>Submitted source</h4>
            <span class={"artifact-release-state " + c().state}>{c().label}</span>
          </div>
          <p class="artifact-release-detail">{c().detail}</p>
          <Show when={c().state === "available" && props.release?.download_available === true}>
            <div class="artifact-release-actions">
              <button
                class="btn ghost artifact-download"
                type="button"
                data-artifact-download={props.agentId}
                disabled={download().busy}
                onClick={downloadArtifact}
              >
                <svg class="ic" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 3v12" />
                  <path d="m7 10 5 5 5-5" />
                  <path d="M5 21h14" />
                </svg>
                <span class="artifact-download-label">
                  {download().busy ? "Preparing download…" : "Download submitted source"}
                </span>
              </button>
              <span
                class="artifact-download-status"
                classList={{ error: download().error }}
                data-artifact-status={props.agentId}
                role="status"
                aria-live="polite"
              >
                {download().status}
              </span>
            </div>
          </Show>
        </section>
      )}
    </Show>
  );
}
