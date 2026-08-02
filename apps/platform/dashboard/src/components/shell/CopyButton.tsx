// Copy control for operational identifiers (hotkeys, agent IDs, SHA-256s):
// port of copyButton (monolith 6055–6062) + doCopy (6396–6420) + the
// delegated click/keydown wiring (6421–6427, 6442–6448). Success/failure
// state lives on the button (copied/failed classes, swapped labels) and is
// announced through the shared #copy-status live region. The copy itself
// (async clipboard with the execCommand fallback) is lib/copy.
import { Show, createSignal, onCleanup } from "solid-js";
import type { JSX } from "solid-js";

import { copyText } from "../../lib/copy";

export interface CopyButtonProps {
  /** Nothing renders without a value (the original returned ""). */
  value: string | null | undefined;
  /** Announced label, e.g. "miner hotkey", "dataset SHA-256". */
  label: string;
  id?: string;
  class?: string;
  /** Custom button content (e.g. the review-details text button). */
  children?: JSX.Element;
}

/** Announce into the shared visually-hidden live region (#copy-status). */
function announce(text: string): void {
  const status = document.getElementById("copy-status");
  if (status) status.textContent = text;
}

export function CopyButton(props: CopyButtonProps): JSX.Element {
  const [state, setState] = createSignal<"" | "copied" | "failed">("");
  let timer: ReturnType<typeof setTimeout> | undefined;
  onCleanup(() => clearTimeout(timer));

  const ariaLabel = () => {
    if (state() === "copied") return "Copied " + props.label;
    if (state() === "failed") return "Could not copy " + props.label;
    return "Copy " + props.label;
  };

  function doCopy(): void {
    const value = props.value;
    if (!value) return;
    copyText(String(value))
      .then(() => {
        setState("copied");
        announce("Copied " + props.label + " to the clipboard.");
        clearTimeout(timer);
        timer = setTimeout(() => setState(""), 1600);
      })
      .catch(() => {
        setState("failed");
        announce("Could not copy " + props.label + ". Select the full value and copy it manually.");
        clearTimeout(timer);
        timer = setTimeout(() => setState(""), 2400);
      });
  }

  function onClick(ev: MouseEvent): void {
    ev.preventDefault();
    ev.stopPropagation();
    doCopy();
  }

  // Explicit keyboard activation, mirroring the original's delegated keydown
  // (Enter and Space; preventDefault also suppresses the synthetic click so
  // the copy never double-fires).
  function onKeyDown(ev: KeyboardEvent): void {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    ev.stopPropagation();
    doCopy();
  }

  return (
    <Show when={props.value}>
      {(value) => (
        <button
          type="button"
          id={props.id}
          class={"copy" + (props.class ? " " + props.class : "")}
          classList={{ copied: state() === "copied", failed: state() === "failed" }}
          data-key={String(value())}
          data-copy-label={props.label}
          aria-label={ariaLabel()}
          aria-describedby="copy-status"
          title={ariaLabel()}
          onClick={onClick}
          onKeyDown={onKeyDown}
        >
          {props.children ?? <span aria-hidden="true">⧉</span>}
        </button>
      )}
    </Show>
  );
}
