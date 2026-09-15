// Circular miner profile picture. The Platform API returns a same-origin
// path (`/api/v1/public/miners/{hotkey}/avatar`) so the dashboard can render
// it without a world-readable Hippius URL.
//
// The avatar is the only image on an otherwise typographic board, so it
// carries identity at a glance: the ring, lift, and rank tinting live in
// CSS (`.miner-avatar` in widgets.css, rank-aware overrides in the
// leaderboard sheet) and key off the row, not off props.
//
// Click opens a scaled thumbnail lightbox; Escape / backdrop dismisses it.
// The trigger is a button so board row clicks do not open the entity panel.
import { Show, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";
import { Portal } from "solid-js/web";

export function MinerAvatar(props: { url?: string | null; size?: "sm" | "lg" }): JSX.Element {
  const [open, setOpen] = createSignal(false);
  const px = (): number => (props.size === "lg" ? 42 : 26);

  return (
    <Show when={props.url}>
      {(url) => (
        <>
          <button
            type="button"
            class="miner-avatar-trigger"
            aria-label="View avatar"
            aria-expanded={open() ? "true" : "false"}
            aria-haspopup="dialog"
            onClick={(ev) => {
              ev.preventDefault();
              ev.stopPropagation();
              setOpen(true);
            }}
          >
            <img
              class={"miner-avatar" + (props.size === "lg" ? " lg" : "")}
              src={url()}
              alt=""
              width={px()}
              height={px()}
              loading="lazy"
              decoding="async"
            />
          </button>
          <Show when={open()}>
            <AvatarLightbox url={url()} onClose={() => setOpen(false)} />
          </Show>
        </>
      )}
    </Show>
  );
}

function AvatarLightbox(props: { url: string; onClose: () => void }): JSX.Element {
  onMount(() => {
    const onKey = (ev: KeyboardEvent): void => {
      if (ev.key === "Escape") props.onClose();
    };
    window.addEventListener("keydown", onKey);
    onCleanup(() => window.removeEventListener("keydown", onKey));
  });

  return (
    <Portal>
      <div
        class="miner-avatar-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="Avatar preview"
        onClick={props.onClose}
      >
        <img
          class="miner-avatar-lightbox-img"
          src={props.url}
          alt=""
          onClick={(ev) => ev.stopPropagation()}
        />
      </div>
    </Portal>
  );
}
