import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import type { ReviewConclusion } from "../../types/pipeline";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACTIVITY_FILTER_LABELS,
  admissionLaneLine,
  admissionRetryLine,
  ACTIVITY_FILTERS,
  ACTIVITY_STATUSES,
  activityStage,
  deferredReviewSummary,
  isSourceReviewIncomplete,
  duplicateComparisonLabel,
  policyScreeningLabel,
  QUEUE_WITHDRAWN_LABEL,
  reviewEventLabel,
  reviewEvidenceNotes,
  reviewEvidenceText,
  scoreFloorAttribution,
  SEASON_WAIT_LABEL,
  seasonOpensText,
  seasonWaitSentence,
  validationDetail,
  validationProgress,
} from "./status";

const HERE = dirname(fileURLToPath(import.meta.url));
const STATUS_SOURCE = readFileSync(join(HERE, "status.ts"), "utf-8");

// ── Row 10: server-backed quick filters (vocabulary slice) ──────────────────
describe("status vocabulary (row 10)", () => {
  it("keeps the canonical status whitelist and the quick-filter map", () => {
    expect(ACTIVITY_STATUSES).toEqual([
      "waiting_screening",
      "screening_failed",
      "screening",
      "waiting_validator",
      "evaluating",
      "below_score_floor",
      "not_queued",
      "retired",
      "scoring_failed",
      "under_review",
      "rejected",
      "scored",
      "live",
    ]);
    expect(ACTIVITY_FILTERS.queued).toEqual([
      "waiting_screening",
      "screening",
      "waiting_validator",
      "below_score_floor",
    ]);
    expect(ACTIVITY_FILTERS.waiting_validator).toEqual(["waiting_validator", "below_score_floor"]);
  });

  it("labels every status with the stage vocabulary (admission wording, #623)", () => {
    // #623 renamed screening to the mechanical-admission vocabulary: the
    // build stage says what is happening, and the deep source review is a
    // deferred, conditional branch — never implied to have already run.
    expect(activityStage("waiting_screening")).toEqual(["Waiting for admission", "progress"]);
    expect(activityStage("screening")).toEqual(["Image build & admission", "progress"]);
    expect(activityStage("screening_passed")).toEqual(["Admitted", "good"]);
    expect(activityStage("screening_failed")).toEqual(["Screening incomplete", "warn"]);
    expect(ACTIVITY_FILTERS.queued).not.toContain("screening_failed");
    expect(activityStage("waiting_validator")).toEqual(["Waiting for validators", "progress"]);
    expect(activityStage("evaluating")).toEqual(["Scoring", "progress"]);
    expect(activityStage("below_score_floor")).toEqual(["Low-priority completion", "warn"]);
    expect(activityStage("not_queued")).toEqual(["Historical · not queued", ""]);
    expect(activityStage("retired")).toEqual(["Retired · earlier benchmark", ""]);
    expect(activityStage("scoring_failed")).toEqual(["Cannot finish scoring", "bad"]);
    // Source review runs before scoring; only a row Platform deferred until a
    // score qualified names the deferred branch.
    expect(activityStage("under_review")).toEqual(["Under review", "warn"]);
    expect(
      activityStage("under_review", {
        status: "under_review",
        deferred_review_triggers: ["top_five"],
      }),
    ).toEqual(["Deferred source review", "warn"]);
    expect(activityStage("rejected")).toEqual(["Rejected", "bad"]);
    expect(activityStage("nonsense")).toEqual(["Pending", ""]);
  });

  it("keeps a review-budget hold neutral and states no finding was made (#562)", () => {
    const held = {
      status: "under_review",
      deferred_review_triggers: ["top_five"] as const,
      review_conclusion: "budget_exhausted" as const,
    };
    expect(isSourceReviewIncomplete(held)).toBe(true);
    expect(activityStage("under_review", held)).toEqual(["Deferred source review", ""]);
    expect(deferredReviewSummary(held)).toBe(
      "Score qualified (top 5) \u00b7 automated review ran out of budget \u2014 no finding",
    );
    // A review that ran and ended inconclusive without exhausting a budget
    // never borrows the budget copy.
    const inconclusive = { ...held, review_conclusion: "no_finding" as const };
    expect(isSourceReviewIncomplete(inconclusive)).toBe(true);
    expect(activityStage("under_review", inconclusive)).toEqual(["Deferred source review", ""]);
    expect(deferredReviewSummary(inconclusive)).toBe(
      "Score qualified (top 5) \u00b7 automated review inconclusive \u2014 no finding",
    );
    // An auditless or preflight hold: claim only that no review completed with
    // a recorded conclusion, never a budget; the chip stays neutral because no
    // finding was recorded.
    const notCompleted = { ...held, review_conclusion: "not_completed" as const };
    expect(isSourceReviewIncomplete(notCompleted)).toBe(false);
    expect(activityStage("under_review", notCompleted)).toEqual(["Deferred source review", ""]);
    expect(deferredReviewSummary(notCompleted)).toBe(
      "Score qualified (top 5) \u00b7 automated review did not complete \u2014 no finding recorded",
    );
    expect(deferredReviewSummary(notCompleted)).not.toContain("budget");
    expect(deferredReviewSummary(notCompleted)).not.toContain("did not run");
    const adverse = {
      status: "under_review",
      deferred_review_triggers: ["anomaly"] as const,
      review_conclusion: "adverse_signal" as const,
    };
    expect(isSourceReviewIncomplete(adverse)).toBe(false);
    expect(activityStage("under_review", adverse)).toEqual(["Deferred source review", "warn"]);
    expect(deferredReviewSummary(adverse)).toBe(
      "Anomaly hold \u00b7 automated review raised a concern",
    );
    const pending = {
      status: "under_review",
      deferred_review_triggers: ["top_five", "anomaly"] as const,
      review_conclusion: "pending" as const,
    };
    expect(activityStage("under_review", pending)).toEqual(["Deferred source review", "progress"]);
    expect(deferredReviewSummary(pending)).toBe(
      "Score qualified (top 5) \u00b7 Anomaly hold \u00b7 automated review pending",
    );
    // A hold without an automated conclusion (copy review, older API) keeps
    // the previous warn chip and adds no summary line.
    expect(activityStage("under_review", { status: "under_review" })).toEqual([
      "Under review",
      "warn",
    ]);
    expect(deferredReviewSummary({ status: "under_review" })).toBe("");
    // The public reason text alone no longer decides the tone.
    expect(
      isSourceReviewIncomplete({
        status: "under_review",
        screening_reason: "Bounded source review was inconclusive; held for review",
      } as { status: string }),
    ).toBe(false);
    expect(deferredReviewSummary({ ...held, status: "scored" })).toBe("");
  });

  it("names the held filter without claiming the review was deferred", () => {
    expect(ACTIVITY_FILTER_LABELS.under_review).toBe("Under review");
    expect(ACTIVITY_FILTER_LABELS.waiting_validator).toBe("Waiting for validators");
  });

  it("explains a review hold without claiming the screener is idle", () => {
    expect(validationDetail({ status: "under_review" })).toBe(
      "This submission is held for review. Holding is not a finding, and existing scores do not clear the hold. " +
        "The screening history below shows whether a deep review is running or an operator decision is pending.",
    );
    expect(
      validationDetail({ status: "under_review", deferred_review_triggers: ["anomaly"] }),
    ).toContain("This submission is held for deferred source review.");
  });

  it("says 'ran out of budget' only for a recorded budget exhaustion", () => {
    const detail = (review_conclusion: ReviewConclusion) =>
      validationDetail({
        status: "under_review",
        deferred_review_triggers: ["top_five"],
        review_conclusion,
      });
    expect(detail("budget_exhausted")).toContain("ran out of budget");
    expect(detail("budget_exhausted")).toContain("which is not a finding");
    expect(detail("budget_exhausted")).toContain("its score placed it in the top five");
    expect(detail("no_finding")).toContain("without reaching a decision and made no finding");
    expect(detail("no_finding")).not.toContain("budget");
    expect(detail("not_completed")).toContain("did not complete and recorded no finding");
    expect(detail("not_completed")).not.toContain("before reviewing");
    expect(detail("not_completed")).not.toContain("budget");
    expect(detail("adverse_signal")).not.toContain("ran out of budget");
    expect(detail("adverse_signal")).toContain("raised a concern");
    expect(detail("pending")).toContain("has not reported yet");
    expect(detail("pending")).not.toContain("budget");
  });

  it("labels previous-generation and closed-generation rows (#458/#462)", () => {
    expect(validationProgress({ status: "below_score_floor", score_count: 2, quorum: 3 })).toBe(
      "2 of 3 · queued last",
    );
    expect(validationProgress({ status: "not_queued", score_count: 0, quorum: 3 })).toBe(
      "Not in active benchmark queue",
    );
    expect(validationProgress({ status: "retired", score_count: 2, quorum: 3 })).toBe(
      "2 of 3 · benchmark closed",
    );
    // Terminal copy for a closed generation reads as history, never loss.
    expect(validationDetail({ status: "retired", score_count: 2, quorum: 3 })).toContain(
      "Nothing here was rejected and nothing was lost",
    );
    expect(validationDetail({ status: "not_queued" })).toContain("retained for audit history");
  });

  it("names an operator withdrawal instead of calling the row historical (SN-470)", () => {
    const withdrawn = {
      status: "not_queued",
      queue_withdrawn: true,
      score_count: 2,
      quorum: 3,
    };
    expect(activityStage("not_queued", withdrawn)).toEqual([QUEUE_WITHDRAWN_LABEL, ""]);
    expect(validationProgress(withdrawn)).toBe("2 of 3 · withdrawn");
    const detail = validationDetail(withdrawn);
    expect(detail).toBe(
      "An operator withdrew this submission from the active benchmark queue, so validators " +
        "will not score it while the withdrawal stands. This is not a rejection: the " +
        "submission and its 2 accepted scores stay on file.",
    );
    expect(detail).not.toContain("historical");
    expect(validationDetail({ ...withdrawn, score_count: 0 })).toContain(
      "the submission and its screening record stay on file",
    );
    // The flag only refines not_queued; it never relabels another status.
    expect(activityStage("scored", { queue_withdrawn: true })).toEqual(["Scored", "good"]);
    expect(activityStage("not_queued", { queue_withdrawn: false })).toEqual([
      "Historical · not queued",
      "",
    ]);
  });

  it("keeps the generic floor copy when no floor number is quotable", () => {
    expect(validationDetail({ status: "below_score_floor", score_count: 2, quorum: 3 })).toBe(
      "Two accepted scores are below the current same-benchmark score floor. " +
        "The final score is still queued, after other unfinished submissions.",
    );
  });
});

// ── Weekend drift #622/#636: review-event evidence ──────────────────────────
// #622: an operator can revise the hold reason (or reopen a review); the
// CURRENT reason is what miners act on, so it leads under its event label,
// with the initial hold kept as labeled history — never silently replaced.
// #636: past the opening event, a mechanical duplicate-claim comparison is
// the INITIAL comparison, not live evidence; it stays out of the current
// channel.
describe("review-event evidence (#622/#636)", () => {
  const revised = {
    review_event: "reopened",
    review_reason: "manual re-check of tool-call provenance",
    review_original_reason: "content near-duplicate of agent abc",
    duplicate_of: "abc",
  };

  it("labels the four review events and defaults to the plain hold", () => {
    expect(reviewEventLabel({ review_event: "opened" })).toBe("Operator review");
    expect(reviewEventLabel({ review_event: "reopened" })).toBe("Review reopened");
    expect(reviewEventLabel({ review_event: "cleared" })).toBe("Review cleared");
    expect(reviewEventLabel({ review_event: "rejected" })).toBe("Review rejected");
    expect(reviewEventLabel({})).toBe("Operator review");
    expect(reviewEventLabel({ review_event: "surprise" })).toBe("Operator review");
  });

  it("leads with the current reason and keeps the initial hold as history", () => {
    expect(reviewEvidenceNotes(revised)).toEqual([
      { label: "Review reopened", text: "manual re-check of tool-call provenance" },
      { label: "Initial hold", text: "content near-duplicate of agent abc" },
    ]);
    expect(reviewEvidenceText(revised)).toBe(
      "Review reopened: manual re-check of tool-call provenance" +
        " Initial hold: content near-duplicate of agent abc",
    );
  });

  it("collapses to one note when the reason never changed", () => {
    const unchanged = { review_event: "opened", review_reason: "held for review" };
    expect(reviewEvidenceNotes(unchanged)).toEqual([
      { label: "Operator review", text: "held for review" },
    ]);
    expect(reviewEvidenceNotes({})).toEqual([]);
    expect(reviewEvidenceText({})).toBe("");
  });

  it("keeps the mechanical claim out of the duplicate channel after the opening event", () => {
    expect(duplicateComparisonLabel(revised)).toBe("Initial comparison");
    expect(duplicateComparisonLabel({ review_event: "opened", duplicate_of: "abc" })).toBe(
      "Compared with",
    );
    expect(duplicateComparisonLabel({ duplicate_of: "abc" })).toBe("Compared with");
  });

  it("names a same-miner match as the previously rejected ancestor", () => {
    const sameMiner = {
      review_event: "rejected",
      duplicate_of: "7020bd00",
      miner_hotkey: "5Di44xhKsame",
      duplicate_hotkey: "5Di44xhKsame",
    };
    expect(duplicateComparisonLabel(sameMiner)).toBe("Previously rejected as");
    expect(duplicateComparisonLabel({ ...sameMiner, review_event: "opened" })).toBe(
      "Previously rejected as",
    );
    expect(
      duplicateComparisonLabel({
        ...sameMiner,
        duplicate_hotkey: "5OtherHotkey",
      }),
    ).toBe("Initial comparison");
  });
});

// ── Weekend drift #623: the screening-policy chip ───────────────────────────
// The active build stage already says what is happening; calling the image
// "verified" before that build finishes was both redundant and temporally
// false. A full review during screening names the deferred branch instead.
describe("policy screening label (#623 + row 14 chip)", () => {
  it("says nothing for a build-only screening pass", () => {
    expect(
      policyScreeningLabel({
        status: "screening",
        screening_build_only: true,
        screening_policy_version: 3,
        required_screening_policy_version: 5,
      }),
    ).toBe("");
  });

  it("names a full source review in screening", () => {
    expect(policyScreeningLabel({ status: "screening", screening_build_only: false })).toBe(
      "Full source review",
    );
  });

  it("keeps the policy-rescreen vocabulary for lagging policy versions", () => {
    expect(
      policyScreeningLabel({ screening_policy_version: 3, required_screening_policy_version: 5 }),
    ).toBe("Rescreen · policy v3 → v5");
    expect(
      policyScreeningLabel({ screening_policy_version: 0, required_screening_policy_version: 5 }),
    ).toBe("Policy v5 screening");
    expect(
      policyScreeningLabel({ screening_policy_version: 5, required_screening_policy_version: 5 }),
    ).toBe("");
    expect(policyScreeningLabel({})).toBe("");
  });
});

// ── Row 11: test_score_floor_message_attributes_the_number_it_quotes ────────
// "The low-priority explanation has to be falsifiable from public data." Old
// copy said "below the current fifth-place score of 0.886" — uncheckable
// because the floor was 5th-highest `composite` while the rank column
// ordered by `official_composite`, so displayed rank 5 was routinely a
// different agent/number; this disagreement generated a support report. Both
// surfaces now cut with the one canonical ordering (ditto.score_order) on
// official_composite; the copy must say so and name the floor holder.
describe("score-floor attribution (row 11)", () => {
  const entry = {
    status: "below_score_floor",
    score_count: 2,
    quorum: 3,
    score_floor: 0.922119,
    active_bench_version: 7,
    score_floor_agent_id: "182ade18-a23b-4282-bed0-af27094fd845",
    score_floor_agent_name: "granite",
    score_floor_agent_version: 3,
    provisional_scores: [{ composite: 0.41 }, { composite: 0.4 }],
  };

  it("attributes the floor to the canonical ordering and names the holder", () => {
    const text = scoreFloorAttribution(entry);
    expect(text).toContain(
      "That floor is the 5th-highest finalized official_composite on Bench v7",
    );
    expect(text).toContain("the same score and the same ordering the leaderboard ranks by");
    expect(text).toContain("held by granite, Submission v3");
    expect(text).toContain("Open that submission to read the same number back.");
  });

  it("ends at the basis when no holder is named", () => {
    const text = scoreFloorAttribution({ ...entry, score_floor_agent_id: null });
    expect(text).toMatch(/the leaderboard ranks by\.$/);
    expect(text).not.toContain("held by");
  });

  it("quotes the floor through the attribution in the table copy", () => {
    const text = validationDetail(entry);
    expect(text).toContain(
      "Two accepted scores mean even a perfect third run could not raise the median above 0.410, " +
        "below the continuation floor of 0.922.",
    );
    expect(text).toContain(scoreFloorAttribution(entry));
    expect(text).toContain("The third score is still queued at low priority");
  });

  it("never resurrects the unfalsifiable phrasings", () => {
    // The unfalsifiable phrasing must not come back, and the retired claim —
    // true only while the two surfaces used different orderings — must not
    // survive the unification that made it false.
    expect(STATUS_SOURCE).not.toContain("fifth-place score of");
    expect(STATUS_SOURCE).not.toContain("the current fifth-place score");
    expect(STATUS_SOURCE).not.toContain("can belong to a different agent");
  });
});

describe("admissionRetryLine (#1215)", () => {
  const now = new Date("2026-08-28T10:00:00Z");

  it("names a running attempt", () => {
    expect(admissionRetryLine({ state: "running", attempt_count: 3 }, now)).toBe(
      "Admission attempt 3 is running now.",
    );
  });

  it("names the queued state before any attempt", () => {
    expect(admissionRetryLine({ state: "queued", attempt_count: 0 }, now)).toBe(
      "Waiting for a screener slot; no attempt has started yet.",
    );
  });

  it("parks source-review and provider failures without promising a retry", () => {
    const line = admissionRetryLine(
      {
        state: "parked",
        attempt_count: 2,
        next_retry_at: "2026-08-28T10:07:30Z",
        last_failure_infrastructure: true,
      },
      now,
    );
    expect(line).toBe(
      "Parked after one provider or source-review failure. It will not retry automatically; an operator must authorize the next attempt.",
    );
  });

  it("identifies a stuck Ditto infrastructure failure", () => {
    const line = admissionRetryLine(
      {
        state: "stuck",
        attempt_count: 1,
        next_retry_at: "2026-08-28T09:59:00Z",
        last_failure_infrastructure: false,
      },
      now,
    );
    expect(line).toBe(
      "Stuck after a Ditto infrastructure failure, not a miner failure. It will not retry automatically; an operator must authorize the next attempt.",
    );
  });

  it("names an operator-authorized retry", () => {
    expect(admissionRetryLine({ state: "retry_queued", attempt_count: 1 }, now)).toBe(
      "A guarded retry was authorized and is waiting for a screener slot (attempt 2).",
    );
  });

  it("promises an automatic retry only when a retry time is scheduled", () => {
    const line = (next_retry_at: string) =>
      admissionRetryLine(
        {
          state: "retry_queued",
          attempt_count: 2,
          next_retry_at,
          last_failure_infrastructure: true,
        },
        now,
      );
    const head =
      "A Ditto build infrastructure failure, not a miner failure. It retries automatically with backoff";
    // A UTC wall clock, whatever offset the wire timestamp carries.
    expect(line("2099-08-28T10:20:45Z")).toBe(head + ", no earlier than 2099-08-28 10:20 UTC.");
    expect(line("2099-08-28T12:20:00+02:00")).toBe(
      head + ", no earlier than 2099-08-28 10:20 UTC.",
    );
    // Already due, and an unparseable time, never print a raw or invalid date.
    expect(line("2000-01-01T00:00:00Z")).toBe(head + " and is due for another attempt.");
    expect(line("not-a-date")).toBe(head + ".");
  });

  it("renders nothing without the block or for unknown states", () => {
    expect(admissionRetryLine(null, now)).toBe("");
    expect(admissionRetryLine({ state: "finished" }, now)).toBe("");
  });
});

describe("admissionLaneLine (#1215)", () => {
  it("names the lane a running attempt is in", () => {
    expect(admissionLaneLine({ state: "running", lane: "build" })).toBe(
      "Current lane: image build.",
    );
    expect(admissionLaneLine({ state: "running", lane: "runtime_smoke" })).toBe(
      "Current lane: runtime smoke test.",
    );
    expect(admissionLaneLine({ state: "running", lane: "source_review" })).toBe(
      "Current lane: source review.",
    );
  });

  it("names the lane a waiting attempt stopped in", () => {
    expect(admissionLaneLine({ state: "stuck", lane: "runtime_smoke" })).toBe(
      "Last attempt stopped in: runtime smoke test.",
    );
    expect(admissionLaneLine({ state: "retry_queued", lane: "build" })).toBe(
      "Last attempt stopped in: image build.",
    );
    expect(admissionLaneLine({ state: "parked", lane: "source_review" })).toBe(
      "Last attempt stopped in: source review.",
    );
  });

  it("never guesses an unevidenced lane", () => {
    expect(admissionLaneLine(null)).toBe("");
    expect(admissionLaneLine({ state: "running", lane: null })).toBe("");
    expect(admissionLaneLine({ state: "running", lane: "verdict" })).toBe("");
    expect(admissionLaneLine({ state: "queued", lane: "build" })).toBe("");
  });
});

// ── Terminal scoring failure (#3442) ────────────────────────────────────────
describe("a submission that cannot finish scoring", () => {
  // kgmin1105: four validators each spent the whole inference allowance. The
  // page said "0 of 3 scores received. No validator is assigned yet. Waiting
  // for 3 validators." Nothing was ever going to be assigned.
  const attempts = Array.from({ length: 4 }, (_, index) => ({
    validator_hotkey: "validator-" + index,
    status: "expired",
    bench_version: 17,
    failure_code: "inference_allowance_exhausted",
  }));

  it("names the cause instead of waiting, once closed out", () => {
    const entry = {
      status: "scoring_failed",
      score_count: 0,
      quorum: 3,
      active_bench_version: 17,
      validation_attempts: attempts,
      validator_retry: {
        state: "exhausted",
        disposition: "terminal_artifact_failure",
        terminal_failure_code: "inference_allowance_exhausted",
      },
    } as unknown as Parameters<typeof validationDetail>[0];
    const detail = validationDetail(entry);
    expect(detail).toContain("cannot finish scoring");
    expect(detail).toContain("used its whole inference allowance");
    expect(detail).toContain("submit a new version");
    expect(detail).not.toContain("Waiting for");
    expect(detail).not.toContain("No validator is assigned yet");
    expect(validationProgress(entry)).toBe("0 of 3 · cannot finish");
  });

  it("reads the same for a row stranded before close-outs existed", () => {
    const board = {
      status: "waiting_validator",
      score_count: 0,
      quorum: 3,
      retry_disposition: "terminal_artifact_failure",
      terminal_failure_code: "inference_allowance_exhausted",
    };
    expect(activityStage(board.status, board)).toEqual(["Cannot finish scoring", "bad"]);
    const detail = validationDetail(board as unknown as Parameters<typeof validationDetail>[0]);
    expect(detail).toContain("used its whole inference allowance");
    expect(detail).not.toContain("Waiting for");
  });

  it("wins over the low-priority lane copy for a below-floor row", () => {
    const belowFloor = {
      status: "below_score_floor",
      score_count: 2,
      quorum: 3,
      score_floor: 0.5,
      provisional_scores: [{ composite: 0.2 }, { composite: 0.3 }],
      retry_disposition: "terminal_artifact_failure",
      terminal_failure_code: "inference_allowance_exhausted",
    } as unknown as Parameters<typeof validationDetail>[0];
    expect(validationProgress(belowFloor)).toBe("2 of 3 · cannot finish");
    const detail = validationDetail(belowFloor);
    expect(detail).toContain("cannot finish scoring");
    expect(detail).not.toContain("queued");
  });

  it("keeps an operator hold on the waiting copy, never blaming the artifact", () => {
    const held = {
      status: "waiting_validator",
      score_count: 0,
      quorum: 3,
      retry_disposition: "operator_hold",
    };
    expect(activityStage(held.status, held)).toEqual(["Waiting for validators", "progress"]);
    expect(
      validationDetail(held as unknown as Parameters<typeof validationDetail>[0]),
    ).not.toContain("cannot finish");
  });
});

// ── Dueling-season wait (bench v16+) ────────────────────────────────────────
describe("a submission waiting for the next dueling season", () => {
  const NOW = new Date("2026-11-10T22:14:00Z");
  const wait = {
    reason: "dueling_season_closed" as const,
    bench_version: 18,
    next_season_opens_at: "2026-11-11T00:45:00Z",
    next_round_epoch: 44,
  };

  it("reads as waiting for the next season, not for validators", () => {
    expect(activityStage("waiting_validator", { scoring_wait: wait })).toEqual([
      SEASON_WAIT_LABEL,
      "progress",
    ]);
    expect(activityStage("below_score_floor", { scoring_wait: wait })[0]).toBe(SEASON_WAIT_LABEL);
    expect(activityStage("waiting_validator", { scoring_wait: null })[0]).toBe(
      "Waiting for validators",
    );
    // Only the waiting lanes: a finished row ignores a stale wait.
    expect(activityStage("scored", { scoring_wait: wait })[0]).toBe("Scored");
  });

  it("a terminal failure still outranks the season wait", () => {
    expect(
      activityStage("waiting_validator", {
        scoring_wait: wait,
        retry_disposition: "terminal_artifact_failure",
      })[0],
    ).toBe("Cannot finish scoring");
  });

  it("names the bench and the approximate opening time in UTC", () => {
    expect(
      validationDetail(
        { status: "waiting_validator", score_count: 1, quorum: 3, scoring_wait: wait },
        NOW,
      ),
    ).toBe(
      "1 of 3 scores received. No dueling season is open for bench 18. Scoring resumes when " +
        "the next season opens, about 00:45 UTC (in 2h 31m).",
    );
  });

  it("falls back to the round boundary without an estimate", () => {
    expect(seasonWaitSentence(17, null, NOW, false)).toBe(
      "No dueling season is scheduled for bench 17 right now. Scoring resumes when a bench 17 season opens.",
    );
    expect(seasonWaitSentence(18, null, NOW)).toBe(
      "No dueling season is open for bench 18. Scoring resumes when the next season opens, " +
        "at the next generator-round boundary.",
    );
    // A past estimate is stale, not "any moment now".
    expect(seasonWaitSentence(18, "2026-11-10T21:00:00Z", NOW)).toContain(
      "at the next generator-round boundary",
    );
  });

  it("formats the opening time in UTC, naming the date a day or more ahead", () => {
    expect(seasonOpensText("2026-11-10T22:40:00Z", NOW)).toBe("about 22:40 UTC (in 26m)");
    expect(seasonOpensText("2026-11-11T00:45:00Z", NOW)).toBe("about 00:45 UTC (in 2h 31m)");
    expect(seasonOpensText("2026-11-12T03:00:00Z", NOW)).toBe(
      "about 2026-11-12 03:00 UTC (in 1d 4h)",
    );
    expect(seasonOpensText("not a date", NOW)).toBe("");
  });
});
