// Circular miner profile picture. The Platform API returns a same-origin
// path (`/api/v1/public/miners/{hotkey}/avatar`) so the dashboard can render
// it without a world-readable Hippius URL.
//
// The avatar is the only image on an otherwise typographic board, so it
// carries identity at a glance: the ring, lift, and rank tinting live in
// CSS (`.miner-avatar` in widgets.css, rank-aware overrides in the
// leaderboard sheet) and key off the row, not off props.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

export function MinerAvatar(props: { url?: string | null; size?: "sm" | "lg" }): JSX.Element {
  const px = (): number => (props.size === "lg" ? 42 : 26);
  return (
    <Show when={props.url}>
      {(url) => (
        <img
          class={"miner-avatar" + (props.size === "lg" ? " lg" : "")}
          src={url()}
          alt=""
          width={px()}
          height={px()}
          loading="lazy"
          decoding="async"
        />
      )}
    </Show>
  );
}
