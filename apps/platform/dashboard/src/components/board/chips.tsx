// The board's chip vocabulary (monolith 5607–5786): continual seed-rounds,
// quality-gate, token-penalty, mid-rollout settlement, and rank-movement
// chips. The tooltip trigger itself is the shared `TipTarget` — this module
// used to carry its own near-identical copy (same descHost, same counter,
// same `tipdesc-` prefix), which is exactly how the two of them minted
// colliding aria-describedby ids.
import { Show } from "solid-js";
import type { JSX } from "solid-js";

import { esc, fx } from "../../lib/format";
import {
  curveV3ScoreAdjustment,
  efficiencyFoldIsApplied,
  efficiencyTieBreakChipLabel,
  qualityGateChipLabel,
  scoreQuorum,
  continualWaves,
  continualSampleCount,
  tokenPenaltyChipLabel,
} from "../../lib/scoring";
import { TipTarget } from "../ui/Tooltip";
import type { BoardEntry } from "./leaderboard-data";
import { rankMoveState } from "./board-state";

/**
 * The seed-round / retained-sample chip (continualScoreChip, 5607–5628). The
 * per-sample dot plot that used to sit here was a debugging view of the
 * continual mean's spread; the active/recorded depth is the number a miner
 * acts on, so the chip carries it and the mean's full composition lives in
 * the tooltip; per-score values remain in row detail.
 */
export function RetestSeedChip(props: { count: number }): JSX.Element {
  const count = (): number => Math.max(0, Number(props.count) || 0);
  return (
    <Show when={count()}>
      {(value) => (
        <TipTarget
          class="rollout-chip settled retest-seed-chip tip"
          tabindex={0}
          text={
            "Recorded " +
            value() +
            " accepted continual-retest " +
            (value() === 1 ? "seed" : "seeds") +
            " for this exact submission. Accepted evidence stays attached to this submission even when another submission from the same owner represents the leaderboard slot."
          }
        >
          {value() + (value() === 1 ? " retest seed" : " retest seeds")}
        </TipTarget>
      )}
    </Show>
  );
}

export function ContinualScoreChip(props: { entry: BoardEntry }): JSX.Element {
  return (
    <Show
      when={props.entry.aggregate_method === "continual_mean"}
      fallback={<RetestSeedChip count={Number(props.entry.confirmation_seed_depth) || 0} />}
    >
      {(() => {
        const waves = (): number => continualWaves(props.entry);
        const recordedSeeds = (): number => Number(props.entry.confirmation_seed_depth) || 0;
        const samples = (): number => continualSampleCount(props.entry);
        const label = (): string =>
          recordedSeeds() > waves()
            ? waves() + " active / " + recordedSeeds() + " recorded"
            : recordedSeeds() + (recordedSeeds() === 1 ? " seed" : " seeds");
        const title = (): string =>
          "Current leaderboard score: arithmetic mean of " +
          samples() +
          " scores — the original three validator scores plus " +
          waves() +
          " active per-seed " +
          (waves() === 1 ? "sample" : "samples") +
          ". This submission has " +
          recordedSeeds() +
          " accepted continual-retest " +
          (recordedSeeds() === 1 ? "seed" : "seeds") +
          " in its audit history; the current scoring cap selects at most " +
          waves() +
          ". KOTH comparisons pair " +
          "agents only on seed identities they share.";
        return (
          <TipTarget class="rollout-chip settled seed-rounds-chip tip" tabindex={0} text={title()}>
            {label()}
          </TipTarget>
        );
      })()}
    </Show>
  );
}

/**
 * Why the composite is not simply the tool/memory average (qualityGateChip,
 * 5633–5647): the base IS that average, and the benchmark quality gates
 * multiply it down. Shown only when the gate meaningfully bites.
 */
export function QualityGateChip(props: { entry: BoardEntry }): JSX.Element {
  const label = (): string | null => qualityGateChipLabel(props.entry.composite_breakdown);
  const tip = (): string => {
    const b = props.entry.composite_breakdown;
    const mult = Number(b?.benchmark_quality_multiplier);
    const reduction = Math.max(0, 1 - mult);
    return (
      "The composite base is the ½·tool + ½·memory average (" +
      (b?.base_accuracy != null ? fx(b.base_accuracy) : "avg") +
      "). The benchmark quality gates multiply that by " +
      fx(mult) +
      " (−" +
      (reduction * 100).toFixed(1) +
      "%), which is why the composite sits below the average. The largest gate is " +
      "conversational sanity: leaking on a greeting or missing a plain declarative floors it " +
      "at 0.5. Consistency, canary integrity, and wasteful tool use also count. Open the row " +
      "for the full breakdown."
    );
  };
  return (
    <Show when={label()}>
      {(text) => (
        <TipTarget class="gate-chip tip-chip" text={tip()}>
          {text()}
        </TipTarget>
      )}
    </Show>
  );
}

/** Token-efficiency chip (tokenPenaltyChip, 5648–5658). */
export function TokenPenaltyChip(props: { entry: BoardEntry }): JSX.Element {
  const state = (): { label: string; penalized: boolean } | null =>
    tokenPenaltyChipLabel(props.entry.composite_breakdown);
  const tip = (): string => {
    const b = props.entry.composite_breakdown;
    const penalty = Math.max(0, Number(b?.token_penalty) || 0);
    return state()?.penalized
      ? "Token efficiency reduced the pre-token composite by " +
          (penalty * 100).toFixed(1) +
          "%. The maximum is 10%."
      : "Token use stayed within the v5 budget, so token efficiency did not reduce this " +
          "score. The maximum possible penalty is 10%.";
  };
  return (
    <Show when={state()}>
      {(s) => (
        <TipTarget class={"token-chip tip-chip" + (s().penalized ? " penalized" : "")} text={tip()}>
          {s().label}
        </TipTarget>
      )}
    </Show>
  );
}

/** Frozen relative-efficiency adjustment, applied after authoritative quality. */
export function EfficiencyBonusChip(props: { entry: BoardEntry }): JSX.Element {
  const active = (): boolean =>
    (props.entry.efficiency_factor != null || props.entry.efficiency_bonus != null) &&
    props.entry.pre_efficiency_composite != null;
  const bonus = (): number => Math.max(0, Number(props.entry.efficiency_bonus) || 0);
  const factor = (): number | null =>
    props.entry.efficiency_factor == null ? null : Number(props.entry.efficiency_factor);
  const applied = (): boolean => efficiencyFoldIsApplied(props.entry);
  const tieBreak = () => efficiencyTieBreakChipLabel(props.entry, { applied: applied() });
  const effective = (): string =>
    props.entry.effective_composite == null
      ? "unavailable"
      : fx(Number(props.entry.effective_composite));
  const official = (): string =>
    fx(Number(props.entry.official_composite ?? props.entry.composite));
  const scoreAdjustment = () => curveV3ScoreAdjustment(props.entry);
  const adjustmentExplanation = (): string => {
    const adjustment = scoreAdjustment();
    if (adjustment == null) return "";
    if (adjustment.mode === "headroom") {
      return (
        " Bench v9+ applies positive efficiency only to remaining quality headroom: " +
        fx(adjustment.quality) +
        " + (" +
        fx(adjustment.factor) +
        " − 1) × (1 − " +
        fx(adjustment.quality) +
        ") = " +
        fx(adjustment.adjusted) +
        "."
      );
    }
    return (
      " Bench v9+ multiplies quality by neutral-or-downside efficiency: " +
      fx(adjustment.quality) +
      " × " +
      fx(adjustment.factor) +
      " = " +
      fx(adjustment.adjusted) +
      "."
    );
  };
  const delta = (): number => ((factor() ?? 1 + bonus()) - 1) * 100;
  const signedDelta = (): string => (delta() >= 0 ? "+" : "−") + Math.abs(delta()).toFixed(1) + "%";
  const tip = (): string => {
    if (factor() == null && applied()) {
      // Keep the established v7/v8 explanation unchanged. Curve-v3 factors
      // use different reference and downside semantics below.
      return (
        "Relative token efficiency adds " +
        (bonus() * 100).toFixed(1) +
        "% after continual retest aggregation: " +
        fx(Number(props.entry.pre_efficiency_composite)) +
        " becomes " +
        effective() +
        ". This is the score used for ranking and emissions."
      );
    }
    const source =
      factor() == null
        ? "The legacy frozen cohort bonus"
        : "The bounded factor against this epoch's frozen cohort P25 reference";
    if (!applied()) {
      return (
        source +
        (factor() == null
          ? " would change the authoritative quality score by " + signedDelta() + ": "
          : " would apply a " + signedDelta() + " factor to the authoritative quality score: ") +
        fx(Number(props.entry.pre_efficiency_composite)) +
        " projects to " +
        effective() +
        "." +
        adjustmentExplanation() +
        " This is an audit-only projection; current ranking and emissions remain based on " +
        official() +
        "."
      );
    }
    if (factor() != null) {
      return (
        "Token efficiency is active as the exact-quality tie-break. The quality score stays " +
        official() +
        " and remains the primary ranking key; lower quality never passes higher quality. " +
        "The bounded factor against this epoch's frozen cohort P25 reference is " +
        signedDelta() +
        " and produces tie-break value " +
        effective() +
        "." +
        adjustmentExplanation() +
        " Ranking, KOTH, and emissions use it only when quality is exactly equal."
      );
    }
    return (
      source +
      " changes the authoritative quality score by " +
      signedDelta() +
      ": " +
      fx(Number(props.entry.pre_efficiency_composite)) +
      " becomes " +
      effective() +
      "." +
      adjustmentExplanation() +
      " This is the score used for ranking and emissions."
    );
  };
  return (
    <Show when={active()}>
      <TipTarget
        class={
          "rollout-chip " +
          (applied() ? "settled" : "partial") +
          " efficiency-bonus-chip tip" +
          (delta() < 0 ? " penalized" : "")
        }
        tabindex={0}
        text={tip()}
      >
        {tieBreak()?.label ?? "efficiency " + (applied() ? "" : "projection ") + signedDelta()}
      </TipTarget>
    </Show>
  );
}

/** Reward-authority state for Bench v9's separate confirmation composite. */
export function V9ConfirmationChip(props: {
  entry: BoardEntry;
  mode: "shadow" | "enforce" | null | undefined;
}): JSX.Element {
  const status = () => props.entry.v9_confirmation_status;
  const longmem = () => props.entry.v9_longmem_mean_composite;
  const mix = () => props.entry.v9_shadow_quality_composite;
  const measuredLabel = (): string => {
    const score = longmem();
    if (score == null) return "LongMem measured";
    return "LongMem " + score.toFixed(3);
  };
  const measuredTip = (): string => {
    const score = longmem();
    const mixScore = mix();
    const parts = [
      "Independent LongMemEval evidence is complete.",
      "Shadow does not change ranking or emissions.",
    ];
    if (score != null) parts.unshift("LongMemEval mean " + score.toFixed(3) + ".");
    if (mixScore != null) parts.push("70/30 mix " + mixScore.toFixed(3) + ".");
    return parts.join(" ");
  };
  const state = (): { label: string; class: string; tip: string } | null => {
    if (props.mode === "shadow") {
      if (longmem() != null || mix() != null) {
        return {
          label: measuredLabel(),
          class: "settled",
          tip: measuredTip(),
        };
      }
      switch (status()) {
        case "full_confirmed":
          return {
            label: "Bench " + props.entry.bench_version + " LongMem shadow measured",
            class: "settled",
            tip: "Independent LongMemEval and ablation evidence is complete. Shadow evidence is visible, but the ordinary base score remains authoritative for ranking and emissions until Enforce begins with this exact profile.",
          };
        case "provisional":
          return {
            label: "Bench " + props.entry.bench_version + " LongMem shadow running",
            class: "partial",
            tip: "LongMemEval and ablation evidence is in progress. Shadow mode does not change this agent's ranking or emissions.",
          };
        case "base_only":
          return {
            label: "Bench " + props.entry.bench_version + " LongMem shadow queued",
            class: "pending",
            tip: "Only the ordinary base score is available so far. Shadow mode keeps that base score authoritative while LongMemEval evidence is collected.",
          };
        default:
          return null;
      }
    }
    if (props.mode !== "enforce") return null;
    switch (status()) {
      case "full_confirmed":
        return {
          label: "Bench " + props.entry.bench_version + " full confirmed",
          class: "settled",
          tip: "Independent confirmation is complete. The verified full composite is authoritative for ranking and emissions.",
        };
      case "provisional":
        return {
          label: "Bench " + props.entry.bench_version + " confirmation pending",
          class: "partial",
          tip: "Confirmation evidence is in progress. This base score remains visible but is not ranked and cannot earn emissions in enforce mode.",
        };
      case "base_only":
        return {
          label: "Bench " + props.entry.bench_version + " base only",
          class: "pending",
          tip: "Only the ordinary base score is available. It remains unranked and cannot earn emissions until full confirmation succeeds.",
        };
      default:
        return null;
    }
  };
  return (
    <Show when={state()}>
      {(value) => (
        <TipTarget
          class={"rollout-chip v9-confirmation-chip tip-chip " + value().class}
          text={value().tip}
        >
          {value().label}
        </TipTarget>
      )}
    </Show>
  );
}

/**
 * Mid-rollout settlement state for the row (rolloutChip, 5662–5680): the
 * agent's median on the incoming benchmark version so far and how many of
 * the 3 independent scores are in. Empty outside a rollout.
 */
export function RolloutChip(props: {
  entry: BoardEntry;
  settledView: boolean;
  desiredVersion: number | null;
}): JSX.Element {
  const active = (): boolean => props.settledView && props.entry.rollout_score_count != null;
  const quorum = (): number => scoreQuorum(props.entry.score_quorum);
  const count = (): number => Number(props.entry.rollout_score_count) || 0;
  const vlabel = (): string => "v" + props.desiredVersion;
  return (
    <Show when={active()}>
      <Show
        when={count() && props.entry.rollout_composite != null}
        fallback={
          <TipTarget
            class="rollout-chip pending tip-chip"
            text={
              "No accepted " +
              vlabel() +
              " score yet. This agent still ranks and earns on its settled score."
            }
          >
            {vlabel() + " pending · 0/" + quorum()}
          </TipTarget>
        }
      >
        {(() => {
          const settled = (): boolean => count() >= quorum();
          const tip = (): string =>
            settled()
              ? esc(vlabel()) +
                " reached quorum: this median is locked in and takes over when the " +
                esc(vlabel()) +
                " rollout fully activates."
              : "Preliminary " +
                esc(vlabel()) +
                " median from " +
                count() +
                " of " +
                quorum() +
                " scores. It can change until quorum and does not affect rank or weights yet.";
          return (
            <TipTarget
              class={"rollout-chip tip-chip" + (settled() ? " settled" : " partial")}
              text={tip()}
            >
              {vlabel() +
                " " +
                fx(props.entry.rollout_composite as number) +
                " · " +
                count() +
                "/" +
                quorum() +
                (settled() ? " settled" : "")}
            </TipTarget>
          );
        })()}
      </Show>
    </Show>
  );
}

/** Rank movement since the viewer's last visit (rankMove, 5737–5746). */
export function RankMove(props: { hotkey: string; rank: number }): JSX.Element {
  const state = (): ReturnType<typeof rankMoveState> => rankMoveState(props.hotkey, props.rank);
  return (
    <Show when={state()}>
      {(move) => (
        <Show
          when={move().kind !== "new"}
          fallback={
            <TipTarget class="rankmove new tip-chip" text="New since your last visit">
              NEW
            </TipTarget>
          }
        >
          <TipTarget
            class={"rankmove " + move().kind + " tip-chip"}
            text={
              (move().kind === "up" ? "Up " : "Down ") + move().delta + " since your last visit"
            }
          >
            {(move().kind === "up" ? "▲" : "▼") + move().delta}
          </TipTarget>
        </Show>
      )}
    </Show>
  );
}
