// Digest-verified run telemetry (monolith telemetryMetric/Outcome/
// AttemptTrail 7216–7235, renderRunTelemetry 7237–7280, loadRunTelemetry
// 7282–7311): fetched from the external transcript mirror only on demand,
// capped at 32 MiB, and rejected unless the payload names the exact digest
// it was requested for. A failure states itself — the transcript download
// stays available for independent verification.
import { For, Match, Show, Switch, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { telemetryCount, telemetryDuration } from "../../lib/format";
import type { RunTelemetry, TelemetryAttempt, TelemetryCase } from "../../types/pipeline";

export const TRANSCRIPT_MAX_BYTES = 32 * 1024 * 1024;

const telemetryCache: Record<string, RunTelemetry> = Object.create(null) as Record<
  string,
  RunTelemetry
>;

/** Outcome slug, sanitized for class + display (7220–7223). */
export function telemetryOutcomeSlug(value: unknown): string {
  return (
    String(value || "unknown")
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, "")
      .slice(0, 40) || "unknown"
  );
}

/** One attempt line: "#N outcome · duration · HTTP s" (7225–7235). */
export function telemetryAttemptLine(attempt: TelemetryAttempt, index: number): string {
  const status = Number(attempt.http_status);
  let detail =
    "#" +
    telemetryCount(attempt.attempt ?? index + 1) +
    " " +
    String(attempt.outcome || "unknown")
      .replace(/[^a-zA-Z0-9 _-]/g, "")
      .slice(0, 40) +
    " · " +
    telemetryDuration(attempt.duration_ms);
  if (Number.isInteger(status) && status >= 100 && status <= 599) detail += " · HTTP " + status;
  return detail;
}

function Metric(props: { label: string; value: string }): JSX.Element {
  return (
    <div>
      <dt>{props.label}</dt>
      <dd>{props.value}</dd>
    </div>
  );
}

export function RunTelemetryView(props: { transcript: RunTelemetry }): JSX.Element {
  const execution = () => {
    const e = props.transcript.execution;
    return e && typeof e === "object" && !Array.isArray(e) ? e : null;
  };
  const relay = () => {
    const r = props.transcript.model_relay;
    return r && typeof r === "object" && !Array.isArray(r) ? r : null;
  };
  const caseEntries = (): TelemetryCase[] =>
    Array.isArray(props.transcript.cases) ? props.transcript.cases.slice(0, 500) : [];
  return (
    <Show when={execution()} fallback={<p>Execution telemetry was not recorded for this run.</p>}>
      {(exec) => (
        <>
          <dl class="run-telemetry-summary">
            <Metric
              label="Completed"
              value={telemetryCount(exec().succeeded) + " / " + telemetryCount(exec().cases)}
            />
            <Metric label="Median" value={telemetryDuration(exec().median_duration_ms)} />
            <Metric label="p95" value={telemetryDuration(exec().p95_duration_ms)} />
            <Metric label="Maximum" value={telemetryDuration(exec().max_duration_ms)} />
            <Metric label="Retries" value={String(telemetryCount(exec().retried))} />
            <Metric label="Timeouts" value={String(telemetryCount(exec().timed_out))} />
            <Metric label="Cancelled" value={String(telemetryCount(exec().cancelled))} />
            <Metric label="Attempts" value={String(telemetryCount(exec().total_attempts))} />
          </dl>
          <Show when={relay()}>
            {(r) => (
              <p class="run-telemetry-relay">
                <b>Model relay:</b> {telemetryCount(r().successes)} successes /{" "}
                {telemetryCount(r().requests)} requests · {telemetryCount(r().retries)} retries ·{" "}
                {telemetryCount(r().caller_cancellations)} caller cancellations ·{" "}
                {telemetryCount(r().infrastructure_failures)} infrastructure failures ·{" "}
                {telemetryCount(r().upstream_attempts)} upstream attempts
              </p>
            )}
          </Show>
          <Show when={caseEntries().length}>
            <details class="run-telemetry-details">
              <summary>Per-question execution ({caseEntries().length})</summary>
              <div class="run-telemetry-table-wrap">
                <table class="run-telemetry-table">
                  <thead>
                    <tr>
                      <th>Question</th>
                      <th>Outcome</th>
                      <th>Duration</th>
                      <th>Attempts</th>
                      <th>Attempt trail</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={caseEntries()}>
                      {(caseEntry, index) => {
                        const caseExecution =
                          caseEntry.execution && typeof caseEntry.execution === "object"
                            ? caseEntry.execution
                            : {};
                        const attempts = Array.isArray(caseExecution.attempts)
                          ? caseExecution.attempts
                          : [];
                        const outcome = telemetryOutcomeSlug(caseExecution.terminal_outcome);
                        return (
                          <tr>
                            <td>{caseEntry.position || index() + 1}</td>
                            <td>
                              <span class={"run-telemetry-outcome " + outcome}>
                                {outcome.replace(/_/g, " ")}
                              </span>
                            </td>
                            <td>{telemetryDuration(caseExecution.total_duration_ms)}</td>
                            <td>{attempts.length}</td>
                            <td>
                              <For each={attempts.slice(0, 12)}>
                                {(attempt, i) => (
                                  <>
                                    {i() > 0 ? <br /> : null}
                                    {telemetryAttemptLine(attempt, i())}
                                  </>
                                )}
                              </For>
                              <Show when={!attempts.length}>—</Show>
                            </td>
                          </tr>
                        );
                      }}
                    </For>
                  </tbody>
                </table>
              </div>
            </details>
          </Show>
        </>
      )}
    </Show>
  );
}

/** "View run telemetry" loader: template + sha-gated, cached per digest. */
export function TelemetryLoader(props: {
  sha256: string;
  urlTemplate: string | null | undefined;
}): JSX.Element {
  const [state, setState] = createSignal<"idle" | "loading" | "loaded" | "error">("idle");
  const [data, setData] = createSignal<RunTelemetry | null>(null);
  const sha = () => String(props.sha256 || "").toLowerCase();

  function load(): void {
    const digest = sha();
    const template = props.urlTemplate;
    if (!template || !/^[0-9a-f]{64}$/.test(digest)) return;
    const cached = telemetryCache[digest];
    if (cached) {
      setData(cached);
      setState("loaded");
      return;
    }
    setState("loading");
    fetch(template.replace("{sha256}", digest))
      .then((response) => {
        if (!response.ok) throw new Error("Transcript unavailable");
        const contentLength = Number(response.headers.get("content-length"));
        if (Number.isFinite(contentLength) && contentLength > TRANSCRIPT_MAX_BYTES) {
          throw new Error("Transcript too large");
        }
        return response.json() as Promise<RunTelemetry>;
      })
      .then((telemetry) => {
        if (!telemetry || telemetry.source_sha256 !== digest) {
          throw new Error("Transcript digest mismatch");
        }
        telemetryCache[digest] = telemetry;
        setData(telemetry);
        setState("loaded");
      })
      .catch(() => {
        setState("error");
      });
  }

  return (
    <Show when={props.urlTemplate}>
      <br />
      <Show when={state() !== "loaded"}>
        <button
          type="button"
          class="run-telemetry-load"
          data-transcript-sha={sha()}
          disabled={state() === "loading"}
          onClick={load}
        >
          View run telemetry
        </button>
      </Show>
      <div
        class="run-telemetry-panel"
        data-run-telemetry
        role="status"
        aria-live="polite"
        aria-busy={state() === "loading" ? "true" : undefined}
      >
        <Switch>
          <Match when={state() === "loading"}>Loading digest-verified run telemetry…</Match>
          <Match when={state() === "error"}>
            <p class="run-telemetry-error">
              Run telemetry could not be verified or loaded. The transcript download remains
              available for independent verification.
            </p>
          </Match>
          <Match when={state() === "loaded" && data()}>
            {(telemetry) => <RunTelemetryView transcript={telemetry()} />}
          </Match>
        </Switch>
      </div>
    </Show>
  );
}
