// "When do I get emissions?" — the most-asked question on the subnet, and one
// the weight matrix beside it cannot answer. Validators commit weights
// asynchronously: the chain holds each one only to WeightsSetRateLimit blocks
// between its OWN submissions, so they never move together and there is no
// moment at which "the validators set weights". What is synchronised is the
// subnet's epoch tick — every Tempo blocks Subtensor folds whatever is
// revealed through Yuma consensus and pays out the emission accrued since the
// last tick, at the same instant for every miner. That tick is the countdown.
//
// Phase and length both come from chain state (LastMechansimStepBlock + Tempo)
// via /public/weights, never from reimplementing Subtensor's epoch predicate:
// the predicate has changed shape across runtimes, and a stale copy of it here
// would render a confidently wrong clock instead of no clock.
import { Show, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import type { JSX } from "solid-js";

import { countdownClock, epochCountdown } from "../../lib/scoring";
import type { EpochCountdown as Countdown } from "../../lib/scoring";
import { Tip } from "../ui/Tooltip";
import type { LeaderboardStore } from "./leaderboard-data";

/** Prose for the tooltip: why this is one clock and not one per validator. */
function explainer(countdown: Countdown): string {
  const minutes = Math.round(countdown.epochSeconds / 60);
  let text =
    "SN118 runs on a " +
    countdown.tempoBlocks +
    "-block tempo (~" +
    minutes +
    " min). At each tick the chain folds every validator's revealed weights " +
    "through Yuma consensus and pays out the emission accrued since the last " +
    "tick — the same instant for every miner. Validators commit their weights " +
    "on their own staggered schedules, so no single submission is the payout.";
  if (countdown.commitRevealEnabled) {
    text +=
      " Commit-reveal is on" +
      (countdown.revealPeriodEpochs != null
        ? ", with a " +
          countdown.revealPeriodEpochs +
          "-epoch reveal delay, so weights committed now are folded " +
          (countdown.revealPeriodEpochs === 1 ? "one tick later" : "later still")
        : "") +
      ".";
  }
  if (countdown.projected) {
    text +=
      " The chain read behind this is older than one full epoch, so the tick " +
      "shown is projected forward from it rather than freshly observed.";
  }
  return text;
}

export function EpochCountdownLine(props: { store: LeaderboardStore }): JSX.Element {
  // The one number on this board that changes every second gets its own
  // per-second signal; everything else on the strip repaints on the poll.
  const [now, setNow] = createSignal(Date.now());
  onMount(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    onCleanup(() => clearInterval(timer));
  });
  const countdown = createMemo<Countdown | null>(() =>
    props.store.unavailable() ? null : epochCountdown(props.store.chainWeights()?.epoch, now()),
  );
  return (
    <Show when={countdown()}>
      {(c) => (
        <div class="epoch-countdown" id="epoch-countdown" role="status" aria-live="off">
          <span class="chain-badge">Next payout</span>
          <span class="epoch-countdown-copy">
            <Tip text={explainer(c())}>
              {"Weights fold into emissions in "}
              <b class="epoch-countdown-clock">{countdownClock(c().secondsRemaining)}</b>
            </Tip>
            <Show when={c().nextEpochBlock != null}>
              {" · block " + Number(c().nextEpochBlock).toLocaleString()}
            </Show>
            {" · every " +
              c().tempoBlocks +
              " blocks (~" +
              Math.round(c().epochSeconds / 60) +
              " min)"}
            <Show when={c().projected}>
              <span class="epoch-countdown-projected"> projected</span>
            </Show>
          </span>
        </div>
      )}
    </Show>
  );
}
