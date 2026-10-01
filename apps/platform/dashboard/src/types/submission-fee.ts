// Public submission-fee wire shape (/public/submission-fee). The fee is an
// exact rao amount in the fixed_tao denomination; operator identity and
// reasons are private and never present.

export interface SubmissionFeeRevision {
  revision: number;
  fee_denomination: string;
  fee_amount_rao: number;
  fee_amount_tao: string;
  previous_fee_amount_rao: number | null;
  previous_fee_amount_tao: string | null;
  effective_at: string | null;
}

export interface SubmissionFeePayload {
  policy_revision: number;
  fee_denomination: string;
  fee_amount_rao: number;
  fee_amount_tao: string;
  /** 0 while the built-in default applies; null when the scan could not find it. */
  fee_revision: number | null;
  fee_effective_at: string | null;
  quote_lifetime_seconds: number;
  history: SubmissionFeeRevision[];
  history_truncated?: boolean;
}
