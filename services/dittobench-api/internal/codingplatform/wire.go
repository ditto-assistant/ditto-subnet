package codingplatform

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type responseEnvelope struct {
	Schema                   string          `json:"schema"`
	CodingContractVersion    int             `json:"coding_contract_version"`
	WeightEligible           *bool           `json:"weight_eligible"`
	Sequence                 uint32          `json:"sequence"`
	Settlement               json.RawMessage `json:"settlement"`
	NormalizedResponseBase64 json.RawMessage `json:"normalized_response_base64"`
	FailureProjectionBase64  json.RawMessage `json:"failure_response_projection_base64"`
}

func parseDispatchResponse(
	body []byte,
	policy codingcontract.InferencePolicy,
) (dispatchResponse, error) {
	var zero dispatchResponse
	maximum := dispatchResponseMaximum(policy)
	if err := codingcontract.ValidateJSONDocument(body, maximum); err != nil {
		return zero, err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var shape map[string]json.RawMessage
	if err := decoder.Decode(&shape); err != nil {
		return zero, err
	}
	if err := requireEOF(decoder); err != nil {
		return zero, err
	}
	for _, required := range []string{
		"schema",
		"coding_contract_version",
		"weight_eligible",
		"sequence",
		"settlement",
		"normalized_response_base64",
		"failure_response_projection_base64",
	} {
		if _, present := shape[required]; !present {
			return zero, errors.New("coding Platform response lacks required authority")
		}
	}
	decoder = json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var envelope responseEnvelope
	if err := decoder.Decode(&envelope); err != nil {
		return zero, err
	}
	if err := requireEOF(decoder); err != nil {
		return zero, err
	}
	if envelope.WeightEligible == nil || *envelope.WeightEligible ||
		envelope.Schema != dispatchResponseSchema ||
		envelope.CodingContractVersion != codingcontract.ContractVersion ||
		envelope.Sequence == 0 || len(envelope.Settlement) == 0 || bytes.Equal(envelope.Settlement, []byte("null")) {
		return zero, errors.New("coding Platform response authority is invalid")
	}
	settlement, err := codingcontract.ParseInferenceProviderSettlement(envelope.Settlement, policy)
	if err != nil {
		return zero, err
	}
	normalized, err := decodeNullableProjection(envelope.NormalizedResponseBase64, policy.MaxResponseBytes)
	if err != nil {
		return zero, err
	}
	failure, err := decodeNullableProjection(envelope.FailureProjectionBase64, policy.MaxResponseBytes)
	if err != nil {
		zeroBytes(normalized)
		return zero, err
	}
	return dispatchResponse{
		Schema: envelope.Schema, CodingContractVersion: envelope.CodingContractVersion,
		WeightEligible: *envelope.WeightEligible, Sequence: envelope.Sequence,
		Settlement: settlement.Clone(), NormalizedResponse: normalized,
		FailureResponseProjection: failure,
	}, nil
}

func decodeNullableProjection(raw json.RawMessage, maximum uint64) ([]byte, error) {
	if len(raw) == 0 {
		return nil, errors.New("coding Platform response projection is missing")
	}
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return nil, nil
	}
	var encoded string
	if err := json.Unmarshal(raw, &encoded); err != nil || encoded == "" {
		return nil, errors.New("coding Platform response projection is invalid")
	}
	maximumEncoded := ((maximum + 2) / 3) * 4
	if uint64(len(encoded)) > maximumEncoded {
		return nil, errors.New("coding Platform response projection exceeds its bound")
	}
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || len(decoded) == 0 || uint64(len(decoded)) > maximum {
		zeroBytes(decoded)
		return nil, errors.New("coding Platform response projection is invalid")
	}
	return decoded, nil
}

func requireEOF(decoder *json.Decoder) error {
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("coding Platform response has trailing JSON")
		}
		return err
	}
	return nil
}
