// Generator identity, the way the agent leaderboard shows an agent: the
// miner's profile picture, the generator's chosen name (or a short id when
// unnamed), its per-hotkey version, any handle annotation, and the owning
// miner as a link to the miner profile.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import { shortKey } from "../../lib/format";
import { spaHref, spaQuery } from "../../lib/router";
import { EntityButton } from "../ui/EntityButton";
import { HandleBadge } from "../ui/HandleBadge";
import { MinerAvatar } from "../ui/MinerAvatar";
import type { GeneratorIdentity } from "./generator-data";
import { generatorLabel } from "./generator-data";

/** The URL that opens one generator's panel over the generators page. */
export function generatorHref(submissionId: string | null): string {
  const params = spaQuery();
  params.delete("generator");
  if (submissionId !== null) params.set("generator", submissionId);
  return spaHref("generators", params);
}

/** Open (or with null, close) a generator's panel without reloading. */
export function showGenerator(submissionId: string | null): void {
  history.pushState(history.state, "", generatorHref(submissionId));
  window.dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
}

function plainClick(ev: MouseEvent): boolean {
  return !(
    ev.defaultPrevented ||
    ev.button !== 0 ||
    ev.metaKey ||
    ev.ctrlKey ||
    ev.shiftKey ||
    ev.altKey
  );
}

/** A link that opens the generator panel; modified clicks keep native
 * navigation so a reader can open it in a new tab. */
export function GeneratorLink(props: {
  submissionId: string;
  class?: string;
  children: JSX.Element;
  title?: string;
}): JSX.Element {
  return (
    <a
      class={props.class}
      href={generatorHref(props.submissionId)}
      title={props.title}
      onClick={(ev) => {
        if (!plainClick(ev)) return;
        ev.preventDefault();
        showGenerator(props.submissionId);
      }}
    >
      {props.children}
    </a>
  );
}

export function GeneratorIdentityCell(props: {
  item: GeneratorIdentity & { submission_id: string };
  minerHotkey: string | null | undefined;
  /** The public default generator has no owner and no panel. */
  publicDefault?: boolean;
}): JSX.Element {
  return (
    <span class="gen-identity">
      <Show
        when={props.item.avatar_url}
        fallback={<span class="gen-avatar-blank" aria-hidden="true" />}
      >
        <MinerAvatar url={props.item.avatar_url} />
      </Show>
      <span class="gen-identity-text">
        <span class="gen-identity-name">
          <Show
            when={!props.publicDefault}
            fallback={<span class="gen-default">Public default generator</span>}
          >
            <GeneratorLink
              submissionId={props.item.submission_id}
              class="gen-name"
              title="Open this generator"
            >
              {generatorLabel(props.item)}
            </GeneratorLink>
            <Show when={props.item.version != null}>
              <span class="gen-version" title="This miner's generator upload number">
                v{props.item.version}
              </span>
            </Show>
            <HandleBadge handle={props.item.name_handle} />
          </Show>
        </span>
        <Show when={props.minerHotkey && !props.publicDefault}>
          <EntityButton
            kind="miner"
            id={props.minerHotkey}
            label={shortKey(props.minerHotkey)}
            class="gen-miner mono"
            title="Open the miner profile"
          />
        </Show>
      </span>
    </span>
  );
}
