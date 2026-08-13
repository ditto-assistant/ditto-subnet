// The leaderboard fold shared by the block's two homes (overview pane and
// the dedicated Leaderboard page) plus the overview's champion box and
// snapshot ledger. One module-scope root owns the /public/leaderboard (with
// the selected bench view's ?bench_version), /public/weights and
// /public/bench/rollout resources, mirroring the monolith's
// Promise.allSettled trio in load() (9674–9707):
// - a leaderboard failure renders stated absence (lastLeaderboardData = null,
//   never stale-as-fresh data),
// - a weights failure keeps the previous matrix (anti-flicker; the API also
//   serves last-known-good with `stale` set),
// - a historical view nulls the chain matrix outright (the overlay genuinely
//   does not apply to an archived board).
import { createMemo, createRoot } from "solid-js";
import type { Accessor } from "solid-js";

import { useEndpoint } from "../../data/useEndpoint";
import { REFRESH_MS } from "../../lib/config";
import type { ResourceState } from "../../data/useEndpoint";
import { leaderboardBenchState } from "../../lib/bench-state";
import { foldChainWeights, rankEntries, rolloutSettledView } from "../../lib/scoring";
import type { ChainWeightFold, ContinualAggregate } from "../../lib/scoring";
import type {
  ChainWeightsSnapshot,
  EfficiencyBoardState,
  EmissionRecipient,
  EmissionsFold,
  LeaderboardEntry,
  LeaderboardPayload,
  RolloutState,
} from "../../types/leaderboard";
import { leaderboardVersionView } from "./board-state";

/** The board's working entry shape: lean wire entry + continual-aggregate
 * scalars and the client-assigned display rank. Family children carry only
 * identity/version/score; full family and score evidence loads after a click. */
export type BoardEntry = LeaderboardEntry &
  ContinualAggregate & {
    rank: number | null;
  };

/** /public/weights with the staleness markers the API adds when a chain
 * re-read is failing. */
export type WeightsSnapshot = ChainWeightsSnapshot & {
  stale?: boolean;
  age_seconds?: number | null;
};

export interface BenchContext {
  active: number | null;
  desired: number | null;
  current: number | null;
}

export interface LeaderboardStore {
  /** The last leaderboard payload for the selected view; null while loading
   * or after a failed fetch (failures render stated absence). */
  payload: Accessor<LeaderboardPayload | null>;
  /** The leaderboard fetch failed (drives the explicit unavailable states). */
  unavailable: Accessor<boolean>;
  /** Ranked, sorted entries (dual rank counters; ineligible rank = null). */
  entries: Accessor<BoardEntry[]>;
  settledView: Accessor<boolean>;
  emissions: Accessor<EmissionsFold | null>;
  /** Board-level relative-efficiency state, or null when the payload omits it
   * (a benchmark version the adjustment cannot apply to at all). */
  efficiency: Accessor<EfficiencyBoardState | null>;
  /** The reigning champion's board entry (resolved from the emissions fold's
   * champion_agent_id), or null when the fold or the entry is absent. The
   * KOTH incumbent can sit below raw #1, so consumers must read its `rank`
   * rather than assume the top row. */
  champion: Accessor<BoardEntry | null>;
  emissionFor: (agentId: string | null | undefined) => EmissionRecipient | null;
  chainWeights: Accessor<WeightsSnapshot | null>;
  chainFold: Accessor<ChainWeightFold | null>;
  rollout: Accessor<RolloutState | null>;
  bench: Accessor<BenchContext>;
  refreshAll: () => void;
  /** Refetch everything unless this is the store's very first consumer (the
   * resources already fetched at creation). */
  ensureFresh: () => void;
}

function safeData<T>(resource: ResourceState<T>): T | undefined {
  if (resource.error()) return undefined;
  try {
    return resource.data();
  } catch {
    return undefined;
  }
}

function buildStore(): LeaderboardStore {
  const path = (): string =>
    "/public/leaderboard" +
    (leaderboardVersionView() === "current"
      ? ""
      : "?bench_version=" + encodeURIComponent(leaderboardVersionView()));
  const board = useEndpoint<LeaderboardPayload>(path, { pollMs: REFRESH_MS });
  const weights = useEndpoint<WeightsSnapshot>("/public/weights", { pollMs: REFRESH_MS });
  const rollout = useEndpoint<RolloutState>("/public/bench/rollout", { pollMs: REFRESH_MS });

  const payload = createMemo<LeaderboardPayload | null>(() => safeData(board) ?? null);
  const unavailable = (): boolean => Boolean(board.error());
  const settledView = createMemo(() => {
    const d = payload();
    return d ? rolloutSettledView(d) : false;
  });
  const entries = createMemo<BoardEntry[]>(() => {
    const d = payload();
    if (!d) return [];
    return rankEntries((d.entries ?? []) as BoardEntry[], settledView());
  });
  const emissions = createMemo<EmissionsFold | null>(() => payload()?.emissions ?? null);
  const efficiency = createMemo<EfficiencyBoardState | null>(() => payload()?.efficiency ?? null);
  const champion = createMemo<BoardEntry | null>(() => {
    const fold = emissions();
    if (!fold || fold.champion_agent_id == null) return null;
    return (
      entries().find((entry) => String(entry.agent_id) === String(fold.champion_agent_id)) ?? null
    );
  });
  const emissionByAgent = createMemo<Record<string, EmissionRecipient>>(() => {
    const out: Record<string, EmissionRecipient> = {};
    (emissions()?.recipients ?? []).forEach((recipient) => {
      out[String(recipient.agent_id)] = recipient;
    });
    return out;
  });

  // Keep the previous matrix across a failed weights tick rather than
  // blanking the chain observation for one poll and refilling it on the next
  // — that one-poll disappearance is what read as "flickering" (9683–9695).
  const chainWeights = createMemo<WeightsSnapshot | null>((prev) => {
    if (leaderboardVersionView() !== "current") return null;
    return safeData(weights) ?? prev ?? null;
  }, null);
  const chainFold = createMemo(() => foldChainWeights(chainWeights()));

  const rolloutState = createMemo<RolloutState | null>(() => safeData(rollout) ?? null);

  // Benchmark version context, sticky the way the monolith's module vars
  // were (render() 4831–4844): a payload that omits a value never regresses
  // a previously-learned one.
  const bench = createMemo<BenchContext>(
    (prev) => {
      const d = payload();
      if (!d) return prev;
      const maxBv = (d.entries ?? [])
        .map((e) => e.bench_version)
        .filter((v): v is number => v != null)
        .reduce((a, b) => Math.max(a, b), 0);
      const state = leaderboardBenchState(
        d.selection_mode,
        d.current_bench_version,
        d.active_bench_version,
        d.desired_bench_version,
        maxBv || prev.current,
      );
      const active = state.active || prev.active;
      return {
        active,
        desired: state.desired || prev.desired || active,
        current: state.selected || prev.current,
      };
    },
    { active: null, desired: null, current: null },
  );

  let booted = false;
  function refreshAll(): void {
    board.refresh();
    weights.refresh();
    rollout.refresh();
  }

  return {
    payload,
    unavailable,
    entries,
    settledView,
    emissions,
    efficiency,
    champion,
    emissionFor: (agentId) =>
      agentId == null ? null : (emissionByAgent()[String(agentId)] ?? null),
    chainWeights,
    chainFold,
    rollout: rolloutState,
    bench,
    refreshAll,
    ensureFresh: () => {
      // The resources fetch once at creation; the first mount must not
      // double-fetch, but every later mount re-syncs with the live API.
      if (!booted) {
        booted = true;
        return;
      }
      refreshAll();
    },
  };
}

let store: LeaderboardStore | null = null;

/** The shared store (created on first use, kept for the session). */
export function leaderboardStore(): LeaderboardStore {
  if (!store) store = createRoot(() => buildStore());
  return store;
}
