package codingrunner

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func parseToolRequest(body []byte) (ToolRequest, error) {
	if len(body) == 0 || int64(len(body)) > MaxToolRequestBytes {
		return ToolRequest{}, errors.New("workspace tool request is outside the body limit")
	}
	if err := codingcontract.ValidateRawJSONUnicode(body); err != nil {
		return ToolRequest{}, err
	}
	if err := rejectDuplicateFields(body); err != nil {
		return ToolRequest{}, err
	}
	shapeDecoder := json.NewDecoder(bytes.NewReader(body))
	shapeDecoder.UseNumber()
	var shape map[string]any
	if err := shapeDecoder.Decode(&shape); err != nil {
		return ToolRequest{}, err
	}
	if err := requireJSONEOF(shapeDecoder); err != nil {
		return ToolRequest{}, err
	}
	for _, field := range []string{
		"coding_contract_version", "case_id", "profile_capability_id", "call_id", "name", "arguments",
	} {
		if _, present := shape[field]; !present {
			return ToolRequest{}, fmt.Errorf("workspace tool request is missing %q", field)
		}
	}
	if _, ok := shape["arguments"].(map[string]any); !ok {
		return ToolRequest{}, errors.New("workspace tool arguments must be an object")
	}
	var request ToolRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return ToolRequest{}, err
	}
	if err := request.validate(); err != nil {
		return ToolRequest{}, err
	}
	return request, nil
}

func (request ToolRequest) validate() error {
	if request.CodingContractVersion != ContractVersion || !validIdentifier(request.CaseID, 256) ||
		!validIdentifier(request.ProfileCapabilityID, 256) || !validIdentifier(request.CallID, 128) ||
		!validIdentifier(request.Name, 80) || len(request.Arguments) == 0 ||
		int64(len(request.Arguments)) > MaxToolRequestBytes {
		return errors.New("workspace tool request identity is invalid")
	}
	if err := codingcontract.ValidateRawJSONUnicode(request.Arguments); err != nil {
		return err
	}
	if err := rejectDuplicateFields(request.Arguments); err != nil {
		return err
	}
	var arguments map[string]any
	if err := json.Unmarshal(request.Arguments, &arguments); err != nil || arguments == nil {
		return errors.New("workspace tool arguments must be an object")
	}
	return nil
}

func rejectDuplicateFields(body []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	if err := scanJSONValue(decoder, 0); err != nil {
		return err
	}
	return requireJSONEOF(decoder)
}

func scanJSONValue(decoder *json.Decoder, depth int) error {
	if depth > 16 {
		return errors.New("workspace tool JSON nesting exceeds 16 levels")
	}
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, composite := token.(json.Delim)
	if !composite {
		return nil
	}
	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return errors.New("workspace tool object key is not a string")
			}
			if _, duplicate := seen[key]; duplicate {
				return fmt.Errorf("workspace tool JSON contains duplicate field %q", key)
			}
			seen[key] = struct{}{}
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return errors.New("workspace tool object is not closed")
		}
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errors.New("workspace tool array is not closed")
		}
	default:
		return errors.New("workspace tool JSON begins with an unexpected delimiter")
	}
	return nil
}

func requireJSONEOF(decoder *json.Decoder) error {
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("workspace tool JSON contains trailing content")
		}
		return err
	}
	return nil
}

func decodeArguments[T any](raw json.RawMessage) (T, error) {
	var zero T
	if err := rejectDuplicateFields(raw); err != nil {
		return zero, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var result T
	if err := decoder.Decode(&result); err != nil {
		return zero, err
	}
	if err := requireJSONEOF(decoder); err != nil {
		return zero, err
	}
	return result, nil
}
