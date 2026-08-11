import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { agentName, agentVersionLabel, monoDisplay, relTime } from "../../lib/format";
import { entityHref } from "../../lib/router";
import type {
  SubmissionImageBuild,
  SubmissionImageBuildSnapshot,
  SubmissionImageBuildStatus,
} from "../../types/fleet";
import { StatusChip } from "../ui/StatusChip";

const STATUS: Record<SubmissionImageBuildStatus, readonly [string, string]> = {
  queued: ["Queued", ""],
  leased: ["Starting", "progress"],
  running: ["Building", "progress"],
  succeeded: ["Ready", "good"],
  consumed: ["Imported", "good"],
  fallback_required: ["Local fallback", "warn"],
  canceled: ["Canceled", "unknown"],
};

function buildRoute(build: SubmissionImageBuild): string {
  if (build.status === "fallback_required") return "Targon → local allowed";
  if (build.provider === "targon") return "Targon";
  if (build.status === "queued") return "Awaiting Targon";
  return "Not assigned";
}

function buildTime(build: SubmissionImageBuild): string {
  return relTime(build.consumed_at || build.completed_at || build.started_at || build.updated_at);
}

function imageSize(bytes: number | null | undefined): string {
  if (!Number.isFinite(bytes) || Number(bytes) < 0) return "";
  const mib = Number(bytes) / 1024 / 1024;
  return (mib >= 100 ? Math.round(mib) : mib.toFixed(1)) + " MiB";
}

export function SubmissionBuildLane(props: {
  snapshot?: SubmissionImageBuildSnapshot | null;
  unavailable?: boolean;
  loading?: boolean;
}): JSX.Element {
  const builds = () => props.snapshot?.builds ?? [];

  return (
    <section class="submission-builds" aria-labelledby="submission-builds-title">
      <header class="submission-builds-head">
        <div>
          <span class="submission-builds-eyebrow">Trusted image build</span>
          <h2 id="submission-builds-title">Targon build lane</h2>
          <p>
            Kaniko builds miner submissions on Targon before the archive returns for health, policy,
            and signing gates. The fleet table below tracks screening workers, not these builders.
          </p>
        </div>
        <span class="submission-builds-window">
          {props.snapshot ? "Last " + props.snapshot.window_hours + "h" : "Live provenance"}
        </span>
      </header>

      <div class="submission-builds-ledger" aria-label="Submission build totals">
        <div>
          <strong>{props.snapshot?.active_count ?? "–"}</strong>
          <span>Active builds</span>
        </div>
        <div>
          <strong>{props.snapshot?.targon_completed_count ?? "–"}</strong>
          <span>Targon imports</span>
        </div>
        <div>
          <strong>{props.snapshot?.fallback_authorized_count ?? "–"}</strong>
          <span>Fallback authorized</span>
        </div>
      </div>

      <div
        class="submission-builds-table-wrap"
        tabindex="0"
        role="region"
        aria-label="Recent miner submission image builds"
      >
        <table class="submission-builds-table">
          <thead>
            <tr>
              <th scope="col">Submission</th>
              <th scope="col">Route</th>
              <th scope="col">State</th>
              <th scope="col">Attempt</th>
              <th scope="col">Image</th>
              <th scope="col">Updated</th>
            </tr>
          </thead>
          <tbody>
            <Show
              when={!props.unavailable && !props.loading && builds().length > 0}
              fallback={
                <tr>
                  <td colspan="6" class="submission-builds-empty">
                    {props.unavailable
                      ? "Submission build provenance is temporarily unavailable."
                      : props.loading
                        ? "Loading Targon build provenance…"
                        : "No Kaniko builds in this window. The next eligible submission will request Targon first."}
                  </td>
                </tr>
              }
            >
              <For each={builds()}>
                {(build) => {
                  const chip = () => STATUS[build.status];
                  return (
                    <tr>
                      <td>
                        <a
                          class="submission-build-agent"
                          href={entityHref("agent", build.agent_id)}
                        >
                          {agentName(build.agent_name)}
                        </a>
                        <span class="submission-build-version">
                          {agentVersionLabel(build.agent_version)}
                        </span>
                      </td>
                      <td>
                        <span class="submission-build-route">{buildRoute(build)}</span>
                      </td>
                      <td>
                        <StatusChip
                          label={chip()[0]}
                          tone={chip()[1]}
                          title={build.error_code || undefined}
                        />
                      </td>
                      <td class="num">{build.attempt_count}</td>
                      <td>
                        {build.output_sha256 ? (
                          <span class="submission-build-image" title={build.output_sha256}>
                            {monoDisplay(build.output_sha256)}
                            <Show when={build.output_size_bytes != null}>
                              <small>{imageSize(build.output_size_bytes)}</small>
                            </Show>
                          </span>
                        ) : (
                          <span class="submission-build-none">–</span>
                        )}
                      </td>
                      <td class="submission-build-time">{buildTime(build)}</td>
                    </tr>
                  );
                }}
              </For>
            </Show>
          </tbody>
        </table>
      </div>
    </section>
  );
}
