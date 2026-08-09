package longmemeval

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const maxSignedEvidenceJSONBytes = 1 << 20

// UnmarshalJSON makes every signed provider field presence-sensitive. Several
// valid values are zero (fallback_used=false, cost_usd_micros=0, token counts
// on a receipt), so validation of the decoded value alone cannot distinguish a
// present zero from an omitted field.
func (e *ProviderEvidence) UnmarshalJSON(raw []byte) error {
	type wire struct {
		Lane              *string `json:"lane"`
		CostSource        *string `json:"cost_source"`
		Currency          *string `json:"currency"`
		Provider          *string `json:"provider"`
		ProfileRevision   *string `json:"profile_revision"`
		Model             *string `json:"model"`
		FallbackUsed      *bool   `json:"fallback_used"`
		Requests          *uint64 `json:"requests"`
		Successes         *uint64 `json:"successes"`
		ReceiptedRequests *uint64 `json:"receipted_requests"`
		PromptTokens      *uint64 `json:"prompt_tokens"`
		CompletionTokens  *uint64 `json:"completion_tokens"`
		TotalTokens       *uint64 `json:"total_tokens"`
		CostUSDmicros     *uint64 `json:"cost_usd_micros"`
		ReceiptSetSHA256  *string `json:"receipt_set_sha256"`
	}
	var decoded wire
	if err := decodeStrictJSON(raw, &decoded); err != nil {
		return err
	}
	if decoded.Lane == nil || decoded.CostSource == nil || decoded.Currency == nil ||
		decoded.Provider == nil || decoded.ProfileRevision == nil || decoded.Model == nil ||
		decoded.FallbackUsed == nil || decoded.Requests == nil || decoded.Successes == nil ||
		decoded.ReceiptedRequests == nil || decoded.PromptTokens == nil ||
		decoded.CompletionTokens == nil || decoded.TotalTokens == nil ||
		decoded.CostUSDmicros == nil || decoded.ReceiptSetSHA256 == nil {
		return errors.New("provider evidence JSON is missing a required signed field")
	}
	*e = ProviderEvidence{
		Lane:              *decoded.Lane,
		CostSource:        *decoded.CostSource,
		Currency:          *decoded.Currency,
		Provider:          *decoded.Provider,
		ProfileRevision:   *decoded.ProfileRevision,
		Model:             *decoded.Model,
		FallbackUsed:      *decoded.FallbackUsed,
		Requests:          *decoded.Requests,
		Successes:         *decoded.Successes,
		ReceiptedRequests: *decoded.ReceiptedRequests,
		PromptTokens:      *decoded.PromptTokens,
		CompletionTokens:  *decoded.CompletionTokens,
		TotalTokens:       *decoded.TotalTokens,
		CostUSDmicros:     *decoded.CostUSDmicros,
		ReceiptSetSHA256:  *decoded.ReceiptSetSHA256,
	}
	return nil
}

func (s *CapabilityScore) UnmarshalJSON(raw []byte) error {
	type wire struct {
		Capability *Capability `json:"capability"`
		Correct    *int        `json:"correct"`
		Count      *int        `json:"count"`
		Mean       *float64    `json:"mean"`
	}
	var decoded wire
	if err := decodeStrictJSON(raw, &decoded); err != nil {
		return err
	}
	if decoded.Capability == nil || decoded.Correct == nil || decoded.Count == nil || decoded.Mean == nil {
		return errors.New("capability score JSON is missing a required signed field")
	}
	*s = CapabilityScore{
		Capability: *decoded.Capability,
		Correct:    *decoded.Correct,
		Count:      *decoded.Count,
		Mean:       *decoded.Mean,
	}
	return nil
}

func (s *Score) UnmarshalJSON(raw []byte) error {
	type wire struct {
		LongMemMean   *float64           `json:"longmem_mean"`
		LongMemStdErr *float64           `json:"longmem_stderr"`
		CaseCount     *int               `json:"case_count"`
		PerCapability *[]CapabilityScore `json:"per_capability"`
	}
	var decoded wire
	if err := decodeStrictJSON(raw, &decoded); err != nil {
		return err
	}
	if decoded.LongMemMean == nil || decoded.LongMemStdErr == nil || decoded.CaseCount == nil ||
		decoded.PerCapability == nil {
		return errors.New("score JSON is missing a required signed field")
	}
	*s = Score{
		LongMemMean:   *decoded.LongMemMean,
		LongMemStdErr: *decoded.LongMemStdErr,
		CaseCount:     *decoded.CaseCount,
		PerCapability: append([]CapabilityScore(nil), (*decoded.PerCapability)...),
	}
	return nil
}

func (e *Evidence) UnmarshalJSON(raw []byte) error {
	type wire struct {
		SchemaVersion    *int                `json:"schema_version"`
		ArtifactSHA256   *string             `json:"artifact_sha256"`
		BenchVersion     *int                `json:"bench_version"`
		ProfileChecksum  *string             `json:"profile_checksum"`
		CaseSetDigest    *string             `json:"case_set_digest"`
		DatasetRevision  *string             `json:"dataset_revision"`
		DatasetSHA256    *string             `json:"dataset_sha256"`
		LatencyMS        *uint64             `json:"latency_ms"`
		Score            *Score              `json:"score"`
		ProviderEvidence *[]ProviderEvidence `json:"provider_evidence"`
	}
	var decoded wire
	if err := decodeStrictJSON(raw, &decoded); err != nil {
		return err
	}
	if decoded.SchemaVersion == nil || decoded.ArtifactSHA256 == nil || decoded.BenchVersion == nil ||
		decoded.ProfileChecksum == nil || decoded.CaseSetDigest == nil || decoded.DatasetRevision == nil ||
		decoded.DatasetSHA256 == nil || decoded.LatencyMS == nil || decoded.Score == nil ||
		decoded.ProviderEvidence == nil {
		return errors.New("evidence JSON is missing a required signed field")
	}
	*e = Evidence{
		SchemaVersion:    *decoded.SchemaVersion,
		ArtifactSHA256:   *decoded.ArtifactSHA256,
		BenchVersion:     *decoded.BenchVersion,
		ProfileChecksum:  *decoded.ProfileChecksum,
		CaseSetDigest:    *decoded.CaseSetDigest,
		DatasetRevision:  *decoded.DatasetRevision,
		DatasetSHA256:    *decoded.DatasetSHA256,
		LatencyMS:        *decoded.LatencyMS,
		Score:            *decoded.Score,
		ProviderEvidence: append([]ProviderEvidence(nil), (*decoded.ProviderEvidence)...),
	}
	return nil
}

func decodeStrictJSON(raw []byte, destination any) error {
	if len(raw) == 0 || len(raw) > maxSignedEvidenceJSONBytes {
		return errors.New("signed JSON size is outside the canonical bound")
	}
	if err := rejectDuplicateJSONFields(raw); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var trailing struct{}
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return errors.New("signed JSON contains trailing content")
		}
		return err
	}
	return nil
}

func rejectDuplicateJSONFields(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := scanJSONValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return errors.New("signed JSON contains trailing content")
		}
		return err
	}
	return nil
}

func scanJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delim, isDelim := token.(json.Delim)
	if !isDelim {
		return nil
	}
	switch delim {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return errors.New("signed JSON object contains a non-string field name")
			}
			if _, duplicate := seen[key]; duplicate {
				return fmt.Errorf("signed JSON contains duplicate field %q", key)
			}
			seen[key] = struct{}{}
			if err := scanJSONValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil {
			return err
		}
		if closing != json.Delim('}') {
			return errors.New("signed JSON object is not closed")
		}
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil {
			return err
		}
		if closing != json.Delim(']') {
			return errors.New("signed JSON array is not closed")
		}
	default:
		return errors.New("signed JSON begins with an unexpected delimiter")
	}
	return nil
}
