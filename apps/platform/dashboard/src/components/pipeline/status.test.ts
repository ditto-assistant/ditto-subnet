// Unit tests for the submission status vocabulary (assert-inventory rows 10
// and 11, the pure slices). The page-level behavior lives in
// src/pages/Submissions.test.tsx.
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACTIVITY_FILTER_LABELS,
  ACTIVITY_FILTERS,
  ACTIVITY_STATUSES,
  activityStage,
  duplicateComparisonLabel,
  policyScreeningLabel,
  reviewEventLabel,
  reviewEvidenceNotes,
  reviewEvidenceText,
  scoreFloorAttribution,
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
      "screening",
      "waiting_validator",
      "evaluating",
      "below_score_floor",
      "not_queued",
      "retired",
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
    expect(activityStage("screening_failed")).toEqual(["Admission interrupted", "warn"]);
    expect(activityStage("waiting_validator")).toEqual(["Waiting for validators", "progress"]);
    expect(activityStage("evaluating")).toEqual(["Scoring", "progress"]);
    expect(activityStage("below_score_floor")).toEqual(["Low-priority completion", "warn"]);
    expect(activityStage("not_queued")).toEqual(["Historical · not queued", ""]);
    expect(activityStage("retired")).toEqual(["Retired · earlier benchmark", ""]);
    expect(activityStage("under_review")).toEqual(["Source integrity review", "warn"]);
    expect(activityStage("rejected")).toEqual(["Rejected", "bad"]);
    expect(activityStage("nonsense")).toEqual(["Pending", ""]);
  });

  it("names the quick filters with the integrity-review vocabulary", () => {
    expect(ACTIVITY_FILTER_LABELS.under_review).toBe("Integrity review");
    expect(ACTIVITY_FILTER_LABELS.waiting_validator).toBe("Waiting for validators");
  });

  it("explains an operator review as paused automation", () => {
    expect(validationDetail({ status: "under_review" })).toBe(
      "Automated processing is paused while an operator reviews this submission. " +
        "No screener or validator is currently working on it.",
    );
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

  it("names the deferred integrity branch for a full review in screening", () => {
    expect(policyScreeningLabel({ status: "screening", screening_build_only: false })).toBe(
      "Source integrity review",
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
