// The subnet's payout clock, in the rail so it is on screen on every page and
// at every scroll position. It began life as one line under the leaderboard's
// emissions strip, which is the one place a miner asking "when do I get paid"
// never scrolls to.
//
// The instrument, not a metric card: the gauge is the epoch and the digits are
// one reading of it. Strip every word and the shape still says a fixed cycle is
// this far along and resets — which is the actual mechanic. Validators commit
// weights on their own staggered schedules; the epoch tick is the one moment
// that is the same for every miner, and it is what the question means.
import { Index, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { countdownClock, epochCountdown } from "../../lib/scoring";
import type { EpochCountdown } from "../../lib/scoring";
import type { ChainEpoch } from "../../types/leaderboard";

/** Gauge segments. A read of epoch position, deliberately NOT one per block —
 * 360 ticks in a 208px rail would be a texture, and claiming a block each
 * would be a mapping the widget does not actually have. */
const SEGMENTS = 24;

/** Below this the payout is imminent enough to say so rather than to keep
 * counting quietly: the digits take the warn hue and the pulse doubles. */
const IMMINENT_SECONDS = 60;

const TICKS = Array.from({ length: SEGMENTS }, (_, index) => index);

/** Coarse text for assistive tech. The digits change every second and must
 * never be announced at that rate, so the region is aria-live="off" and this
 * label carries the same fact at a granularity worth speaking. */
function spokenLabel(countdown: EpochCountdown): string {
  const seconds = countdown.secondsRemaining;
  if (seconds < 60) return "Next payout in under a minute.";
  const minutes = Math.round(seconds / 60);
  return "Next payout in about " + minutes + " minute" + (minutes === 1 ? "" : "s") + ".";
}

export function EpochClock(props: {
  epoch: () => ChainEpoch | null | undefined;
  /** DOM id; the rail owns the default, a second mount (the overview
   * masthead) names its own so the page never carries two `#epoch-clock`. */
  id?: string;
}): JSX.Element {
  const [now, setNow] = createSignal(Date.now());
  onMount(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    onCleanup(() => clearInterval(timer));
  });

  const countdown = createMemo<EpochCountdown | null>(() => epochCountdown(props.epoch(), now()));
  const elapsed = createMemo(() => {
    const c = countdown();
    if (!c || c.epochSeconds <= 0) return 0;
    return Math.min(1, Math.max(0, 1 - c.secondsRemaining / c.epochSeconds));
  });
  const filled = createMemo(() => Math.round(elapsed() * SEGMENTS));
  const imminent = createMemo(() => {
    const c = countdown();
    return c != null && c.secondsRemaining <= IMMINENT_SECONDS;
  });

  return (
    <section
      class="epoch-clock"
      id={props.id ?? "epoch-clock"}
      classList={{ imminent: imminent(), projected: countdown()?.projected }}
      aria-label={countdown() ? spokenLabel(countdown() as EpochCountdown) : undefined}
      // Never announced per tick; `spokenLabel` carries it coarsely instead.
      aria-live="off"
    >
      <Show
        when={countdown()}
        fallback={
          // Stated absence, in place. The rail is chrome on every page, so a
          // widget that vanished would shift the nav under the pointer and
          // read as "no payout" rather than "no reading".
          <>
            <div class="epoch-clock-label">
              <span class="epoch-clock-label-text">Next payout in</span>
            </div>
            <div class="epoch-clock-time unread" aria-hidden="true">
              --:--
            </div>
            <div class="epoch-clock-foot">Chain read unavailable</div>
          </>
        }
      >
        {(c) => (
          <>
            <div class="epoch-clock-label">
              <span class="epoch-clock-dot" aria-hidden="true" />
              <span class="epoch-clock-label-text">Next payout in</span>
            </div>
            <div class="epoch-clock-time">{countdownClock(c().secondsRemaining)}</div>
            <div class="epoch-clock-gauge" aria-hidden="true">
              {/* Decorative: the same reading is in the region's aria-label.
                  Index, not For: the gauge is a fixed 24 segments and what
                  changes is each one's state, not the set of them. */}
              <Index each={TICKS}>
                {(tick) => (
                  <i classList={{ on: tick() < filled(), head: tick() === filled() - 1 }} />
                )}
              </Index>
            </div>
            <div class="epoch-clock-foot">
              <Show when={c().nextEpochBlock != null}>
                <span class="epoch-clock-block">
                  block {Number(c().nextEpochBlock).toLocaleString()}
                </span>
              </Show>
              <span class="epoch-clock-tempo">{c().tempoBlocks}-block epoch</span>
              <Show when={c().projected}>
                <span class="epoch-clock-flag">projected</span>
              </Show>
            </div>
          </>
        )}
      </Show>
    </section>
  );
}
