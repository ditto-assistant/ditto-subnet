// The one-shot dispute (monolith renderScreeningDispute
// 7461–7503, disputeSigningMessage 7505–7512, shellQuote/
// disputeSigningCommand 7514–7522, bindScreeningDispute 7524–7587): a
// rejected submission may file exactly one private dispute of its screening
// decision, and -- since bench v13 (#1852) -- a scored, live, evaluating or
// held submission may spend that one dispute on the gate notes it cites, so a
// shadow would-be zero is appealable before any gate enforces. Signed locally
// with btcli. Wallet details stay in this browser and are not submitted —
// only the message, the 128-hex hotkey signature and the cited note ids go to
// the API.
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

const GATE_NOTE_ID_PATTERN = /^[0-9a-f]{16}$/;

/** Parse the optional bench v13+ gate `note_id` list a dispute contests.
 *
 * Ids are the 16-hex values shown on the Account page's "Gate notes" panel;
 * they may be separated by commas, whitespace, or newlines. Returns the
 * de-duplicated list, `[]` for an empty field, or `null` when any token is
 * not an id (the form refuses to submit rather than spend the one dispute on
 * a malformed appeal). */
export function parseGateNoteIds(raw: string): string[] | null {
  const tokens = raw
    .split(/[\s,]+/)
    .map((token) => token.trim().toLowerCase())
    .filter(Boolean);
  if (tokens.some((token) => !GATE_NOTE_ID_PATTERN.test(token))) return null;
  if (tokens.length > 64) return null;
  return [...new Set(tokens)];
}

/** Statuses whose owner may spend the one dispute on cited bench v13+ gate
 * notes: every status an accepted score can exist under. */
export const GATE_NOTE_DISPUTE_STATUSES: ReadonlySet<string> = new Set([
  "scored",
  "live",
  "evaluating",
  "ath_pending_review",
]);

/** The resolved-dispute card (renderScreeningDispute's dispute branch). */
function DisputeOutcome(props: { dispute: Dispute }): JSX.Element {
  const gateNotes = () => props.dispute.kind === "gate_notes";
  const heading = () =>
    props.dispute.status === "pending"
      ? gateNotes()
        ? "Gate-note dispute awaiting review"
        : "Dispute awaiting review"
      : props.dispute.resolution === "release"
        ? "Dispute accepted"
        : "Dispute upheld";
  const detail = () =>
    props.dispute.status === "pending"
      ? "Your one dispute was submitted " +
        relTime(props.dispute.submitted_at) +
        (gateNotes()
          ? ". An operator will review your private message against the cited gate notes."
          : ". An operator will review your private message and the source artifact.")
      : props.dispute.resolution === "release"
        ? gateNotes()
          ? "An operator accepted the dispute: the cited gate notes are recorded as contested. Your score and status are unchanged."
          : "An operator accepted the dispute and released this submission from quarantine."
        : gateNotes()
          ? "An operator reviewed the dispute and upheld the cited gate notes. This appeal is final; your score and status are unchanged."
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
  /** True when an accepted score of this submission carries bench v13+ gate
   * evidence, which is what makes a gate-note dispute possible. */
  gateNotes?: boolean;
  /** Called after a successful POST so the drawer refetches the pipeline. */
  onSubmitted?: () => void;
}): JSX.Element {
  // A rejected submission disputes its screening decision; a scored one with
  // v13 gate evidence disputes the notes it cites (ids required).
  const gateNotesMode = () =>
    props.status !== "rejected" &&
    props.gateNotes === true &&
    GATE_NOTE_DISPUTE_STATUSES.has(props.status ?? "");
  const [message, setMessage] = createSignal("");
  const [wallet, setWallet] = createSignal("");
  const [hotkey, setHotkey] = createSignal("");
  const [signature, setSignature] = createSignal("");
  const [gateNoteIds, setGateNoteIds] = createSignal("");
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
  const gateNoteIdsValid = () => {
    const ids = parseGateNoteIds(gateNoteIds());
    return ids !== null && (!gateNotesMode() || ids.length > 0);
  };
  const submitDisabled = () =>
    submitting() || !messageValid() || !signatureValid() || !gateNoteIdsValid();

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
    const noteIds = parseGateNoteIds(gateNoteIds());
    if (noteIds === null || (gateNotesMode() && noteIds.length === 0)) return;
    postJSON("/public/agent/" + encodeURIComponent(props.agentId) + "/dispute", {
      message: appeal,
      signature: signature().trim(),
      ...(noteIds.length ? { gate_note_ids: noteIds } : {}),
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
      <Match when={props.status === "rejected" || gateNotesMode()}>
        <section
          class="pipeline-section screening-dispute"
          aria-labelledby="pipeline-dispute-title"
          data-dispute-mode={gateNotesMode() ? "gate_notes" : "screening"}
        >
          <div class="pipeline-section-heading">
            <h4 id="pipeline-dispute-title">
              {gateNotesMode() ? "Dispute bench v13 gate notes" : "Dispute screening decision"}
            </h4>
          </div>
          <p class="screening-dispute-copy">
            {gateNotesMode()
              ? "You may submit one private dispute for this submission. Cite the gate note ids from " +
                "Account → Gate notes and explain specifically why each verdict is wrong. An operator " +
                "will review your note against the cited evidence; the outcome is recorded and never " +
                "changes your score or status. Once submitted, this dispute cannot be edited or replaced."
              : "You may submit one private dispute for this submission. Explain specifically why the " +
                "screening decision is wrong. An operator will review your note and source artifact. Once " +
                "submitted, this dispute cannot be edited or replaced."}
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
            <label for="screening-dispute-gate-notes">
              Gate note ids{" "}
              <span class="muted">{gateNotesMode() ? "(required)" : "(optional)"}</span>
              <input
                id="screening-dispute-gate-notes"
                autocomplete="off"
                spellcheck={false}
                required={gateNotesMode()}
                placeholder="16-hex note ids from Account → Gate notes, comma separated"
                onInput={(ev) => {
                  setGateNoteIds(ev.currentTarget.value);
                }}
              />
              <span class="screening-dispute-meta">
                <span>
                  Cite the bench v13+ gate verdicts you contest. Ids are checked against this
                  submission's own scores; the ids are not part of the signed message.
                </span>
                <Show when={gateNoteIds().trim() && parseGateNoteIds(gateNoteIds()) === null}>
                  <span class="error">Each id is 16 hexadecimal characters.</span>
                </Show>
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
