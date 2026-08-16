// Shared reserved / pending / stricken handle chip. The public leaderboard,
// submissions table, agent drawer, reviews, and operator pipeline all render
// the same annotation the Platform API attaches as `name_handle`.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import type { NameHandle } from "../../types/leaderboard";
import { ChipTip } from "../board/chips";

const COPY: Record<
  "reserved" | "disputed" | "pending",
  { label: string; tip: (stem: string) => string }
> = {
  reserved: {
    label: "handle reserved",
    tip: (stem) =>
      "This payment-owner family holds a signed, endorsed reservation for handle “" +
      stem +
      "”. Other families cannot upload a colliding name.",
  },
  disputed: {
    label: "name stricken",
    tip: () =>
      "This name was stricken after three entrenched miner families endorsed a handle claim. Upload again under a different name.",
  },
  pending: {
    label: "handle pending",
    tip: (stem) =>
      "A signed claim for handle “" +
      stem +
      "” is waiting on endorsements from entrenched miner families.",
  },
};

export function HandleBadge(props: { handle?: NameHandle | null }): JSX.Element {
  const status = (): "reserved" | "disputed" | "pending" | null => {
    const value = props.handle?.status;
    return value === "reserved" || value === "disputed" || value === "pending" ? value : null;
  };
  return (
    <Show when={status()}>
      {(kind) => {
        const copy = () => COPY[kind()];
        return (
          <ChipTip
            class={"handle-badge " + kind() + " tip-chip"}
            text={copy().tip(props.handle?.stem ?? "")}
          >
            {copy().label}
          </ChipTip>
        );
      }}
    </Show>
  );
}
