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

type V9ScoreGateEvidence struct {
	SchemaVersion     int                             `json:"schema_version"`
	BenchVersion      int                             `json:"bench_version"`
	RolloutMode       string                          `json:"rollout_mode"`
	ThresholdProfile  V9ScoreGateThresholdProfile     `json:"threshold_profile"`
	ModelUse          V9ModelUseGateEvidence          `json:"model_use"`
	AuthoritativeTool V9AuthoritativeToolGateEvidence `json:"authoritative_tool"`
}
