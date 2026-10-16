// Build bounties (#2942): the public board, a bounty's detail and one claim.
// Mirrors ditto_screening_protocol.bounty; the recorded fixtures are pinned
// to those models by Platform's test_bounty_board_fixtures.py.

export type FundingMode = "rehearsal" | "live";

export type BountySlotStatus =
  | "open"
  | "held"
  | "claimed"
  | "submitted"
  | "rework"
  | "accepted"
  | "rejected"
  | "expired"
  | "withdrawn"
  | "cancelled";

export type BountyEventKind =
  | "revision_published"
  | "round_opened"
  | "slot_held"
  | "hold_expired"
  | "hold_released"
  | "slot_claimed"
  | "fee_refund_owed"
  | "claims_paused"
  | "claims_resumed"
  | "submitted"
  | "withdrawn"
  | "deadline_missed"
  | "review_overdue"
  | "rework_requested"
  | "accepted"
  | "rejected"
  | "reviewer_reassigned"
  | "appeal_filed"
  | "appeal_upheld"
  | "appeal_denied"
  | "slot_cancelled"
  | "slot_reopened"
  | "fee_retained"
  | "award_recorded"
  | "obligation_approved"
  | "transfer_dispatched"
  | "transfer_confirmed"
  | "transfer_failed"
  | "transfer_uncertain"
  | "fact_recorded";

export type BountyClaimState = "held" | "hold_expired" | "claimed" | "refund_owed";
export type BountyObligationKind = "fee_refund" | "fee_retained" | "award";
export type BountyObligationReason =
  | "hold_expired"
  | "slot_lost"
  | "identity_changed"
  | "duplicate_payment"
  | "cancelled"
  | "system_fault"
  | "accepted"
  | "rejected"
  | "expired"
  | "withdrawn"
  | "accepted_work";
export type BountyRefundReason = Exclude<
  BountyObligationReason,
  "rejected" | "expired" | "withdrawn" | "accepted_work"
>;
export type BountyObligationState =
  | "retained"
  | "awaiting_approval"
  | "over_policy_limit"
  | "approved"
  | "dispatched"
  | "uncertain"
  | "confirmed"
  | "failed";
export type BountyTransferState = "dispatched" | "confirmed" | "failed" | "expired" | "uncertain";
export type BountyFactKind = "merged" | "deployed" | "adopted";
export type BountyReviewDecision = "accept" | "changes_requested" | "reject";

export interface BountyTerms {
  title: string;
  scope: string;
  public_interfaces: string;
  acceptance_evidence: string;
  dependencies: string[];
  reviewer_login: string;
  entry_fee_alpha_rao: number;
  reward_pool_alpha_rao: number;
  attempts_per_round: number;
  delivery_days: number;
  review_days: number;
  hold_minutes: number;
  funding_mode: FundingMode;
  fee_escrow_coldkey: string;
  fee_escrow_hotkey: string;
}

export interface BountySlotCounts {
  open: number;
  held: number;
  claimed: number;
  submitted: number;
  rework: number;
  closed: number;
}

export interface BountySummary {
  bounty_id: string;
  repo: string;
  issue: number;
  title: string;
  current_revision: number;
  spec_digest: string;
  entry_fee_alpha_rao: number;
  reward_pool_alpha_rao: number;
  delivery_days: number;
  review_days: number;
  reviewer_login: string;
  funding_mode: FundingMode;
  current_round: number;
  claims_paused: boolean;
  slots: BountySlotCounts;
  reviews_overdue: number;
  awarded_alpha_rao: number;
  published_at: string;
  updated_at: string;
}

export interface BountyBoardPage {
  items: BountySummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface BountyRevisionView {
  revision: number;
  spec_digest: string;
  terms: BountyTerms;
  published_at: string;
}

export interface BountyRoundView {
  round: number;
  opened_revision: number;
  opened_at: string;
}

export interface BountySlotClaimant {
  claim_id: string;
  hotkey: string;
  payee_coldkey: string;
  github_login: string;
  revision: number;
}

export interface BountySlotView {
  slot_id: string;
  round: number;
  position: number;
  status: BountySlotStatus;
  status_changed_at: string;
  hold_expires_at: string | null;
  deadline_at: string | null;
  review_due_at: string | null;
  review_overdue: boolean;
  claimant: BountySlotClaimant | null;
}

export interface BountyEventView {
  event_id: number;
  kind: BountyEventKind;
  revision: number | null;
  round: number | null;
  slot_id: string | null;
  claim_id: string | null;
  recorded_at: string;
}

export interface BountyDetail {
  summary: BountySummary;
  revisions: BountyRevisionView[];
  rounds: BountyRoundView[];
  slots: BountySlotView[];
  events: BountyEventView[];
}

export interface BountyFeeReceipt {
  block_hash: string;
  block_number: number;
  extrinsic_index: number;
}

export interface BountySubmissionView {
  submission_id: string;
  pr_number: number;
  head_sha: string;
  submitted_at: string;
  review_due_at: string;
  decision: BountyReviewDecision | null;
  decision_reason: string | null;
  attribution: string | null;
  rework_deadline_at: string | null;
  decided_at: string | null;
}

export interface BountyAppealView {
  appeal_id: string;
  subject_event_id: number;
  reason_digest: string;
  filed_at: string;
  upheld: boolean | null;
  decided_at: string | null;
}

export interface BountyTransferView {
  transfer_id: string;
  round: number;
  state: BountyTransferState;
  receipt: BountyFeeReceipt | null;
  simulated: boolean;
  dispatched_at: string;
  settled_at: string | null;
}

export interface BountyObligationView {
  obligation_id: string;
  kind: BountyObligationKind;
  reason: BountyObligationReason;
  payee_coldkey: string;
  amount_alpha_rao: number;
  state: BountyObligationState;
  round: number;
  approvals: number;
  // Null while no payout policy is configured or the amount is over its limit.
  approvals_required: number | null;
  attribution: string | null;
  simulated: boolean;
  created_at: string;
  transfers: BountyTransferView[];
}

export interface BountyFactView {
  kind: BountyFactKind;
  reference: string;
  recorded_at: string;
}

export interface BountyRefundView {
  obligation_id: string;
  reason: BountyRefundReason;
  payee_coldkey: string;
  amount_alpha_rao: number;
  receipt: BountyFeeReceipt;
  simulated: boolean;
  created_at: string;
}

export interface BountyClaimView {
  claim_id: string;
  bounty_id: string;
  repo: string;
  issue: number;
  revision: number;
  spec_digest: string;
  slot_id: string;
  round: number;
  position: number;
  state: BountyClaimState;
  slot_status: BountySlotStatus;
  hotkey: string;
  payee_coldkey: string;
  github_login: string;
  funding_mode: FundingMode;
  fee_alpha_rao: number;
  fee_escrow_coldkey: string;
  fee_escrow_hotkey: string;
  held_at: string;
  hold_expires_at: string;
  claimed_at: string | null;
  deadline_at: string | null;
  simulated_receipt: BountyFeeReceipt | null;
  receipts: BountyFeeReceipt[];
  refunds: BountyRefundView[];
  submissions: BountySubmissionView[];
  appeals: BountyAppealView[];
  obligations: BountyObligationView[];
  facts: BountyFactView[];
}
