import { For, Show, createEffect, createSignal } from "solid-js";

import { authJSON } from "../../lib/api";
import { minerSession, sessionAuthHeader } from "../../stores/sessionStore";

interface Run {
  run_id: string;
  agent_id: string;
  phase: "competition" | "confirmation";
  state: string;
  estimate_microusd: number;
  cap_microusd: number;
  approval_url: string;
}
interface Candidate {
  agent_id: string;
  bundle_id?: string;
  phase?: "competition" | "confirmation";
}
interface Budgets {
  enabled: boolean;
  runs: Run[];
  payer_candidates: Candidate[];
}
const dollars = (micros: number) =>
  (micros / 1_000_000).toLocaleString("en-US", { style: "currency", currency: "USD" });

/** The miner proposes the payer; the linked Ditto user separately approves. */
export function EvaluationBudgets() {
  const [data, setData] = createSignal<Budgets | null>(null);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");
  async function load() {
    const token = minerSession()?.token;
    if (!token) {
      setData(null);
      return;
    }
    try {
      const response = await authJSON<Budgets>("/me/evaluation-budgets", {
        headers: sessionAuthHeader(),
      });
      if (minerSession()?.token === token) {
        setData(response);
        setError("");
      }
    } catch (e) {
      if (minerSession()?.token === token)
        setError(e instanceof Error ? e.message : "Could not load evaluation budgets.");
    }
  }
  createEffect(() => {
    setData(null);
    if (minerSession()?.token) void load();
  });
  async function propose(candidate: Candidate) {
    const token = minerSession()?.token;
    setBusy(true);
    setError("");
    try {
      await authJSON<Run>("/me/evaluation-budget", {
        method: "POST",
        headers: { ...sessionAuthHeader(), "content-type": "application/json" },
        body: JSON.stringify({ ...candidate, phase: candidate.phase ?? "confirmation" }),
      });
      if (minerSession()?.token === token) await load();
    } catch (e) {
      if (minerSession()?.token === token)
        setError(e instanceof Error ? e.message : "Could not prepare the comparison budget.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Show when={minerSession()}>
      <section class="account-card" aria-label="Evaluation budgets">
        <h3>Evaluation budgets</h3>
        <Show when={data() && !data()?.enabled}>
          <p class="muted">
            Evaluation credit billing is not enabled on this deployment. You can review funding
            availability in Ditto.
          </p>
        </Show>
        <p>
          Pay inference costs with your linked Ditto credits. Your submission fee stays separate.
          Approval resumes this evaluation without another submission fee.
        </p>
        <a
          class="btn ghost"
          href="https://app.heyditto.ai/settings/usage"
          target="_blank"
          rel="noopener noreferrer"
        >
          Pay with TAO or alpha
        </a>{" "}
        <button class="btn ghost" disabled={busy()} onClick={() => void load()}>
          Refresh budgets
        </button>
        <For each={data()?.runs?.slice(0, 10)}>
          {(run) => (
            <div class="account-card">
              <p>
                <strong>
                  {run.phase === "confirmation"
                    ? "Whole confirmation comparison"
                    : "Competition evaluation"}
                </strong>{" "}
                · {run.state}
              </p>
              <p class="mono muted">Submission {run.agent_id}</p>
              <p>
                Estimated {dollars(run.estimate_microusd)} · maximum {dollars(run.cap_microusd)}
              </p>
              <Show when={run.state === "proposed"}>
                <a class="btn" href={run.approval_url} target="_blank" rel="noopener noreferrer">
                  Review and approve in Ditto
                </a>
              </Show>
              <Show when={run.state === "approved"}>
                <p class="muted">Funded. This evaluation can enter the queue.</p>
              </Show>
              <Show when={run.state === "closed" && run.phase === "competition"}>
                <button
                  class="btn ghost"
                  disabled={busy() || !minerSession()?.scopes.includes("upload")}
                  onClick={() => void propose({ agent_id: run.agent_id, phase: "competition" })}
                >
                  Prepare a fresh evaluation budget
                </button>
              </Show>
              <Show when={run.state === "closing"}>
                <p class="muted">Waiting for provider receipts before releasing unused credits.</p>
              </Show>
            </div>
          )}
        </For>
        <For each={data()?.payer_candidates}>
          {(candidate) => (
            <div class="account-card">
              <p>
                <strong>Confirmation needs a payer</strong>
              </p>
              <p class="mono muted">Submission {candidate.agent_id}</p>
              <p>
                Prepare the full comparison estimate for your linked account. You will review its
                maximum cost in Ditto before any inference starts.
              </p>
              <button
                class="btn"
                disabled={busy() || !minerSession()?.scopes.includes("upload")}
                onClick={() => void propose(candidate)}
              >
                Prepare whole comparison budget
              </button>
            </div>
          )}
        </For>
        <Show when={error()}>
          <p class="account-error" role="alert">
            {error()}
          </p>
        </Show>
      </section>
    </Show>
  );
}
