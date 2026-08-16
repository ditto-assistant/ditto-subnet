// Circular miner profile picture. The Platform API returns a same-origin
// path (`/api/v1/public/miners/{hotkey}/avatar`) so the dashboard can render
// it without a world-readable Hippius URL.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

export function MinerAvatar(props: { url?: string | null; size?: "sm" | "lg" }): JSX.Element {
  const px = (): number => (props.size === "lg" ? 36 : 22);
  return (
    <Show when={props.url}>
      {(url) => (
        <img
          class={"miner-avatar" + (props.size === "lg" ? " lg" : "")}
          src={url()}
          alt=""
          width={px()}
          height={px()}
        />
      )}
    </Show>
  );
}
