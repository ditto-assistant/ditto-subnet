package protocol

// V9BaseDetails is the typed, signature-bound ordinary-score evidence emitted
// only by Bench v9. Scores use integer millionths and factors use basis points
// so independent Go and Python verifiers never depend on float formatting.
type V9BaseDetails struct {
	SchemaVersion            int                     `json:"schema_version"`
	BenchVersion             int                     `json:"bench_version"`
	ScoreContract            V9ScoreContractIdentity `json:"score_contract"`
	RunID                    string                  `json:"run_id"`
	ArtifactSHA256           string                  `json:"artifact_sha256"`
	DatasetSHA256            string                  `json:"dataset_sha256"`
	TranscriptSHA256         string                  `json:"transcript_sha256"`
	OrdinaryCompositeMicros  int64                   `json:"ordinary_composite_micros"`
	OrdinaryStderrMicros     int64                   `json:"ordinary_stderr_micros"`
	ScoreGates               V9ScoreGateEvidence     `json:"score_gates"`
	ScoreGatesSHA256         string                  `json:"score_gates_sha256"`
	SemanticGateFactorBPS    int                     `json:"semantic_gate_factor_bps"`
	AppliedGateFactorBPS     int                     `json:"applied_gate_factor_bps"`
	EffectiveCompositeMicros int64                   `json:"effective_composite_micros"`
	EffectiveStderrMicros    int64                   `json:"effective_stderr_micros"`
}

type V9ScoreContractIdentity struct {
	Revision       string `json:"revision"`
	ManifestSHA256 string `json:"manifest_sha256"`
}

type V9ScoreGateThresholdProfile struct {
	ID             string `json:"id"`
	ManifestSHA256 string `json:"manifest_sha256"`
}

type V9ScoreGateExclusionCounts struct {
	Preflight      int `json:"preflight"`
	Ablation       int `json:"ablation"`
	Undelivered    int `json:"undelivered"`
	ValidatorFault int `json:"validator_fault"`
}

type V9ModelUseGateEvidence struct {
	AdministeredCases        int                        `json:"administered_cases"`
	EligibleCases            int                        `json:"eligible_cases"`
	SuccessfulInferenceCases int                        `json:"successful_inference_cases"`
	MissingInferenceCases    int                        `json:"missing_inference_cases"`
	ObservedRequests         uint64                     `json:"observed_requests"`
	SuccessfulRequests       uint64                     `json:"successful_requests"`
	PromptTokens             uint64                     `json:"prompt_tokens"`
	CompletionTokens         uint64                     `json:"completion_tokens"`
	Excluded                 V9ScoreGateExclusionCounts `json:"excluded"`
	CaseAttributionComplete  bool                       `json:"case_attribution_complete"`
	RequestCoverageBPS       int                        `json:"request_coverage_bps"`
	CoverageBPS              int                        `json:"coverage_bps"`
	ThresholdBPS             int                        `json:"threshold_bps"`
	Result                   string                     `json:"result"`
	FactorBPS                int                        `json:"factor_bps"`
}

type V9AuthoritativeToolGateEvidence struct {
	ExpectedExecutions   int    `json:"expected_executions"`
	MatchedExecutions    int    `json:"matched_executions"`
	MissingExecutions    int    `json:"missing_executions"`
	UnexpectedExecutions int    `json:"unexpected_executions"`
	ObservedExecutions   int    `json:"observed_executions"`
	CoverageBPS          int    `json:"coverage_bps"`
	ThresholdBPS         int    `json:"threshold_bps"`
	Result               string `json:"result"`
	FactorBPS            int    `json:"factor_bps"`
}

// V9ModelDependenceGateEvidence is the causal model-dependence gate added by
// Bench v12. It is carried as an omitzero value so v9..v11 evidence
// omits it entirely (and its struct equality/JSON stay byte-identical); it is
// populated only for bench_version>=12.
type V9ModelDependenceGateEvidence struct {
	AdministeredCases        int    `json:"administered_cases"`
	EligibleCases            int    `json:"eligible_cases"`
	DependentCases           int    `json:"dependent_cases"`
	IndependentCases         int    `json:"independent_cases"`
	SliceAttributionComplete bool   `json:"slice_attribution_complete"`
	DependenceBPS            int    `json:"dependence_bps"`
	ThresholdBPS             int    `json:"threshold_bps"`
	Result                   string `json:"result"`
	FactorBPS                int    `json:"factor_bps"`
}

// V9InferenceLatencyGateEvidence is the inference-latency review gate added by
// Bench v12 (adversary review C2). It flags a run whose inference-required,
// model-reached cases returned below a plausibility floor for a real completion.
// It is carried as an omitzero value so v9..v11 evidence omits it entirely (and
// its struct equality/JSON stay byte-identical), and it is populated only for
// bench_version>=12 runs where the gate is administered.
type V9InferenceLatencyGateEvidence struct {
	AdministeredCases   int    `json:"administered_cases"`
	EligibleCases       int    `json:"eligible_cases"`
	FlaggedCases        int    `json:"flagged_cases"`
	UnflaggedCases      int    `json:"unflagged_cases"`
	FloorMS             int64  `json:"floor_ms"`
	AttributionComplete bool   `json:"attribution_complete"`
	Posture             string `json:"posture"`
	SubFloorBPS         int    `json:"sub_floor_bps"`
	ThresholdBPS        int    `json:"threshold_bps"`
	Result              string `json:"result"`
	FactorBPS           int    `json:"factor_bps"`
}

// V9AnswerStuffingGateEvidence is the Class-D answer-stuffing detection gate
// added by Bench v12. It flags a run whose COMPUTED-answer cases (cases whose
// expected answer is not verbatim present in their seeded evidence) had the
// finished answer value fed into a model INPUT the harness sent BEFORE that
// value appeared in any model COMPLETION -- the signature of a harness that
// computes the answer in engine code and injects it for the model to parrot.
// It is carried as an omitzero value so v9..v11 evidence omits it entirely (and
// its struct equality/JSON stay byte-identical), and it is populated only for
// bench_version>=12 runs where the gate is administered.
type V9AnswerStuffingGateEvidence struct {
	AdministeredCases   int  `json:"administered_cases"`
	EligibleCases       int  `json:"eligible_cases"`
	StuffedCases        int  `json:"stuffed_cases"`
	CleanCases          int  `json:"clean_cases"`
	AttributionComplete bool `json:"attribution_complete"`
	// ReviewRequired is true when at least one COMPUTED case's model I/O overflowed
	// the capture ceiling (a prompt beyond the model's full context window), so the
	// run was routed to human review (Result "review_required", full factor) instead
	// of a pass. Prompt size is never itself a signal; this is a last-resort safety
	// net that never fires on a legitimate full-context prompt.
	ReviewRequired bool   `json:"review_required"`
	Posture        string `json:"posture"`
	StuffedBPS     int    `json:"stuffed_bps"`
	ThresholdBPS   int    `json:"threshold_bps"`
	// MinCases is the LOW count threshold the gate flags on: the run zeroes when the
	// number of PROVABLE stuffed cases is >= this (default 1). It is the primary
	// gating lever; StuffedBPS/ThresholdBPS are retained only as the published share
	// statistic. Present only for bench_version>=12.
	MinCases int `json:"min_cases"`
	// LooseEligibleCases / LooseStuffedCases / LooseStuffedBPS are the "loose"
	// systematic-review signal: over the COMPUTED-answer population (answers not
	// verbatim-recalled), REGARDLESS of whether the value also appears elsewhere in
	// seeded memory, how many cases fed the finished answer into a model INPUT before
	// any COMPLETION. Unlike the provable slice (StuffedCases) this cannot be
	// auto-proven against RAG, so it only ROUTES TO REVIEW (never auto-zeroes) when
	// the share crosses ReviewShareThresholdBPS. An honest RAG/legit-compute agent's
	// computed answers appear in COMPLETIONS, not inputs, so its loose share is ~0.
	// Present only for bench_version>=12; retained as observability + the review
	// governor.
	LooseEligibleCases      int    `json:"loose_eligible_cases"`
	LooseStuffedCases       int    `json:"loose_stuffed_cases"`
	LooseStuffedBPS         int    `json:"loose_stuffed_bps"`
	ReviewShareThresholdBPS int    `json:"review_share_threshold_bps"`
	Result                  string `json:"result"`
	FactorBPS               int    `json:"factor_bps"`
}

type V9ScoreGateEvidence struct {
	SchemaVersion     int                             `json:"schema_version"`
	BenchVersion      int                             `json:"bench_version"`
	RolloutMode       string                          `json:"rollout_mode"`
	ThresholdProfile  V9ScoreGateThresholdProfile     `json:"threshold_profile"`
	ModelUse          V9ModelUseGateEvidence          `json:"model_use"`
	AuthoritativeTool V9AuthoritativeToolGateEvidence `json:"authoritative_tool"`
	// ModelDependence is present only for bench_version>=12; the zero value is
	// omitted for v9..v11 so their wire JSON and struct equality are
	// byte-identical.
	ModelDependence V9ModelDependenceGateEvidence `json:"model_dependence,omitzero"`
	// InferenceLatency is present only for bench_version>=12 runs where the gate
	// is administered; the zero value is omitted for v9..v11 (and for a v12 run
	// built without the gate) so their wire JSON and struct equality stay
	// byte-identical.
	InferenceLatency V9InferenceLatencyGateEvidence `json:"inference_latency,omitzero"`
	// AnswerStuffing is present only for bench_version>=12 runs where the gate is
	// administered; the zero value is omitted for v9..v11 (and for a v12 run built
	// without the gate) so their wire JSON and struct equality stay byte-identical.
	AnswerStuffing V9AnswerStuffingGateEvidence `json:"answer_stuffing,omitzero"`
}
