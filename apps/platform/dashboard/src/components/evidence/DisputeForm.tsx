// The one-shot screening dispute (monolith renderScreeningDispute
// 7461–7503, disputeSigningMessage 7505–7512, shellQuote/
// disputeSigningCommand 7514–7522, bindScreeningDispute 7524–7587): a
// rejected submission may file exactly one private dispute, signed locally
// with btcli. Wallet details stay in this browser and are not submitted —
// only the message and the 128-hex hotkey signature go to the API.
import { Match, Show, Switch, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { postJSON } from "../../lib/api";
import { relTime } from "../../lib/format";
import { CopyButton } from "../shell/CopyButton";
import type { Dispute } from "../../types/pipeline";

/** "ditto-dispute-v1:<agentId>:<sha256(message)>" (7505–7512). */
export function disputeSigningMessage(agentId: string, message: string): Promise<string> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(message)).then((digest) => {
    const hash = Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    return "ditto-dispute-v1:" + agentId + ":" + hash;
  });
}

export function shellQuote(value: string): string {
  return "'" + String(value).replace(/'/g, "'\"'\"'") + "'";
}

export function disputeSigningCommand(
  walletName: string,
  hotkeyName: string,
  payload: string,
): string {
  return (
    "btcli wallet sign --wallet-name " +
    shellQuote(walletName) +
    " --wallet-hotkey " +
    shellQuote(hotkeyName) +
    " --use-hotkey --message " +
    shellQuote(payload) +
    " --json-output"
  );
}

const SIGNATURE_PATTERN = /^[0-9a-fA-F]{128}$/;

/** The resolved-dispute card (renderScreeningDispute's dispute branch). */
function DisputeOutcome(props: { dispute: Dispute }): JSX.Element {
  const heading = () =>
    props.dispute.status === "pending"
      ? "Dispute awaiting review"
      : props.dispute.resolution === "release"
        ? "Dispute accepted"
        : "Dispute upheld";
  const detail = () =>
    props.dispute.status === "pending"
      ? "Your one dispute was submitted " +
        relTime(props.dispute.submitted_at) +
        ". An operator will review your private message and the source artifact."
      : props.dispute.resolution === "release"
        ? "An operator accepted the dispute and released this submission from quarantine."
        : "An operator reviewed the dispute and upheld the screening rejection. This appeal is final.";
  return (
    <section class="pipeline-section screening-dispute" aria-labelledby="pipeline-dispute-title">
      <div class="pipeline-section-heading">
        <h4 id="pipeline-dispute-title">{heading()}</h4>
      </div>
      <p class="screening-dispute-copy">{detail()}</p>
    </section>
  );
}

export function ScreeningDispute(props: {
  agentId: string;
  status: string | undefined;
  dispute: Dispute | null | undefined;
  /** Called after a successful POST so the drawer refetches the pipeline. */
  onSubmitted?: () => void;
}): JSX.Element {
  const [message, setMessage] = createSignal("");
  const [wallet, setWallet] = createSignal("");
  const [hotkey, setHotkey] = createSignal("");
  const [signature, setSignature] = createSignal("");
  const [command, setCommand] = createSignal(
    "Enter your dispute, wallet name, and hotkey name to generate the command.",
  );
  const [commandReady, setCommandReady] = createSignal(false);
  const [status, setStatus] = createSignal<{ text: string; error: boolean }>({
    text: "",
    error: false,
  });
  const [submitting, setSubmitting] = createSignal(false);
  let updateToken = 0;

  const messageValid = () => {
    const value = message().trim();
    return value.length >= 20 && value.length <= 1000;
  };
  const signatureValid = () => SIGNATURE_PATTERN.test(signature().trim());
  const submitDisabled = () => submitting() || !messageValid() || !signatureValid();

  // The btcli command re-derives whenever any input changes; a stale digest
  // computation is fenced by token exactly like the original's update().
  function update(): void {
    const value = message().trim();
    const token = ++updateToken;
    setCommandReady(false);
    if (value.length < 20 || value.length > 1000) {
      setCommand("Write at least 20 characters to generate the command.");
      return;
    }
    void disputeSigningMessage(props.agentId, value).then((valueToSign) => {
      if (token !== updateToken) return;
      const walletName = wallet().trim();
      const hotkeyName = hotkey().trim();
      if (walletName && hotkeyName) {
        setCommand(disputeSigningCommand(walletName, hotkeyName, valueToSign));
        setCommandReady(true);
      } else {
        setCommand("Enter your wallet name and hotkey name to generate the command.");
      }
    });
  }

  function onSubmit(event: Event): void {
    event.preventDefault();
    const appeal = message().trim();
    if (appeal.length < 20 || appeal.length > 1000 || !signatureValid()) return;
    setSubmitting(true);
    setStatus({ text: "Submitting dispute…", error: false });
    postJSON("/public/agent/" + encodeURIComponent(props.agentId) + "/dispute", {
      message: appeal,
      signature: signature().trim(),
    })
      .then(() => {
        setStatus({ text: "Dispute submitted.", error: false });
        props.onSubmitted?.();
      })
      .catch((error: unknown) => {
        setSubmitting(false);
        setStatus({
          text: (error instanceof Error && error.message) || "The dispute could not be submitted.",
          error: true,
        });
      });
  }

  return (
    <Switch>
      <Match when={props.dispute}>{(dispute) => <DisputeOutcome dispute={dispute()} />}</Match>
      <Match when={props.status === "rejected"}>
        <section
          class="pipeline-section screening-dispute"
          aria-labelledby="pipeline-dispute-title"
        >
          <div class="pipeline-section-heading">
            <h4 id="pipeline-dispute-title">Dispute screening decision</h4>
          </div>
          <p class="screening-dispute-copy">
            You may submit one private dispute for this submission. Explain specifically why the
            screening decision is wrong. An operator will review your note and source artifact. Once
            submitted, this dispute cannot be edited or replaced.
          </p>
          <form class="screening-dispute-form" id="screening-dispute-form" onSubmit={onSubmit}>
            <label for="screening-dispute-message">
              Your dispute
              <textarea
                id="screening-dispute-message"
                minlength="20"
                maxlength="1000"
                required
                placeholder="Explain what the screener misidentified and where the relevant behavior appears in your source."
                onInput={(ev) => {
                  setMessage(ev.currentTarget.value);
                  update();
                }}
              />
              <span class="screening-dispute-meta">
                <span>20–1000 characters</span>
                <span id="screening-dispute-count">{message().length} / 1000</span>
              </span>
            </label>
            <div class="screening-dispute-wallets">
              <label for="screening-dispute-wallet">
                Wallet name
                <input
                  id="screening-dispute-wallet"
                  autocomplete="off"
                  spellcheck={false}
                  placeholder="default"
                  onInput={(ev) => {
                    setWallet(ev.currentTarget.value);
                    update();
                  }}
                />
              </label>
              <label for="screening-dispute-hotkey">
                Hotkey name
                <input
                  id="screening-dispute-hotkey"
                  autocomplete="off"
                  spellcheck={false}
                  placeholder="miner"
                  onInput={(ev) => {
                    setHotkey(ev.currentTarget.value);
                    update();
                  }}
                />
              </label>
            </div>
            <label>
              Ready-to-run btcli command
              <span class="screening-dispute-command">
                <code id="screening-dispute-command">{command()}</code>
                <Show
                  when={commandReady()}
                  fallback={
                    <button
                      type="button"
                      class="copy"
                      id="screening-dispute-command-copy"
                      data-key=""
                      data-copy-label="btcli signing command"
                      aria-label="Copy btcli signing command"
                      title="Copy btcli signing command"
                      disabled
                    >
                      <span aria-hidden="true">⧉</span>
                    </button>
                  }
                >
                  <CopyButton
                    id="screening-dispute-command-copy"
                    value={command()}
                    label="btcli signing command"
                  />
                </Show>
              </span>
              <span class="screening-dispute-meta">
                Run this locally, then paste the <code>signed_message</code> value below. Wallet
                details stay in this browser and are not submitted.
              </span>
            </label>
            <label for="screening-dispute-signature">
              Hotkey signature
              <input
                id="screening-dispute-signature"
                inputmode="text"
                autocomplete="off"
                maxlength="128"
                pattern="[0-9a-fA-F]{128}"
                required
                placeholder="Paste the 128-character hexadecimal signature"
                onInput={(ev) => {
                  setSignature(ev.currentTarget.value);
                  update();
                }}
              />
            </label>
            <div class="screening-dispute-actions">
              <button class="screening-dispute-submit" type="submit" disabled={submitDisabled()}>
                Submit final dispute
              </button>
              <p
                class={"screening-dispute-status" + (status().error ? " error" : "")}
                id="screening-dispute-status"
                role="status"
                aria-live="polite"
              >
                {status().text}
              </p>
            </div>
          </form>
        </section>
      </Match>
    </Switch>
  );
}
