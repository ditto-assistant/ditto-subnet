package codingcontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"unicode/utf8"
)

type canonicalModel interface {
	RunManifest | SeedRequest | RunRequest
}

type parsedModel interface {
	RunManifest | SeedRequest | RunRequest | TaskEvidence | RunEvidence
}

// CanonicalJSON validates and emits the sorted known-field projection with one
// trailing newline. Unknown input fields never survive into this projection.
func CanonicalJSON[T canonicalModel](value T) ([]byte, error) {
	if err := validateCanonical(value); err != nil {
		return nil, err
	}
	return canonicalJSONUnchecked(value)
}

// Digest validates and hashes the exact canonical known-field projection.
func Digest[T canonicalModel](value T) (string, error) {
	body, err := CanonicalJSON(value)
	if err != nil {
		return "", err
	}
	return digestBytes(body), nil
}

// IssueDigest binds the exact model-visible issue projection used by coding selection.
func IssueDigest(issue Issue) (string, error) {
	if err := issue.Validate(); err != nil {
		return "", err
	}
	return digestUnchecked(issue)
}

// RuntimePolicyDigest binds the exact model-visible path and command projection.
func RuntimePolicyDigest(policy RuntimePolicy) (string, error) {
	if err := policy.Validate(); err != nil {
		return "", err
	}
	return digestUnchecked(policy)
}

// BudgetsDigest binds the exact model, workspace-tool, and wall-time budgets.
func BudgetsDigest(budgets Budgets) (string, error) {
	if err := budgets.Validate(); err != nil {
		return "", err
	}
	return digestUnchecked(budgets)
}

// TaskEvidenceDigest is the only signing-capable task-evidence digest path.
func TaskEvidenceDigest(
	manifest RunManifest,
	validatorTicketID string,
	evidence TaskEvidence,
) (string, error) {
	if err := evidence.ValidateAgainst(manifest, validatorTicketID); err != nil {
		return "", err
	}
	return digestUnchecked(evidence)
}

// RunEvidenceDigest is the only signing-capable run-evidence digest path.
func RunEvidenceDigest(
	manifest RunManifest,
	validatorTicketID string,
	evidence RunEvidence,
	tasks []TaskEvidence,
) (string, error) {
	if err := evidence.ValidateAgainst(manifest, validatorTicketID, tasks); err != nil {
		return "", err
	}
	return digestUnchecked(evidence)
}

func digestUnchecked(value any) (string, error) {
	body, err := canonicalJSONUnchecked(value)
	if err != nil {
		return "", err
	}
	return digestBytes(body), nil
}

func canonicalJSONUnchecked(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projected any
	if err := decoder.Decode(&projected); err != nil {
		return nil, err
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projected); err != nil {
		return nil, err
	}
	canonical := output.Bytes()
	if len(canonical) > MaxCanonicalJSONBytes {
		return nil, errors.New("canonical coding JSON exceeds 4 MiB")
	}
	return canonical, nil
}

func digestBytes(body []byte) string {
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}

func validateCanonical[T parsedModel](value T) error {
	switch typed := any(value).(type) {
	case RunManifest:
		return typed.Validate()
	case SeedRequest:
		return typed.Validate()
	case RunRequest:
		return typed.Validate()
	case TaskEvidence:
		return typed.Validate()
	case RunEvidence:
		return typed.Validate()
	default:
		return errors.New("unsupported canonical coding model")
	}
}

func ParseRunManifest(body []byte) (RunManifest, error) {
	return parseCanonical[RunManifest](body, validateRunManifestShape)
}

func ParseSeedRequest(body []byte) (SeedRequest, error) {
	return parseCanonical[SeedRequest](body, validateSeedRequestShape)
}

func ParseRunRequest(body []byte) (RunRequest, error) {
	return parseCanonical[RunRequest](body, validateRunRequestShape)
}

func ParseTaskEvidence(body []byte) (TaskEvidence, error) {
	return parseCanonical[TaskEvidence](body, validateTaskEvidenceShape)
}

func ParseRunEvidence(body []byte) (RunEvidence, error) {
	return parseCanonical[RunEvidence](body, validateRunEvidenceShape)
}

// ValidateJSONDocument applies the shared bounded Unicode, duplicate-field,
// nesting, and trailing-content rules to a non-canonical transport document.
func ValidateJSONDocument(body []byte, maximumBytes int) error {
	if maximumBytes <= 0 || len(body) == 0 || len(body) > maximumBytes {
		return errors.New("coding JSON size is outside its transport bound")
	}
	if err := ValidateRawJSONUnicode(body); err != nil {
		return err
	}
	if err := rejectDuplicateJSONFields(body); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var decoded any
	if err := decoder.Decode(&decoded); err != nil {
		return err
	}
	return requireEOF(decoder)
}

func parseCanonical[T parsedModel](
	body []byte,
	validateShape func(map[string]any) error,
) (T, error) {
	var zero T
	if len(body) == 0 || len(body) > MaxCanonicalJSONBytes {
		return zero, errors.New("coding JSON size is outside the canonical bound")
	}
	if err := ValidateRawJSONUnicode(body); err != nil {
		return zero, err
	}
	if err := rejectDuplicateJSONFields(body); err != nil {
		return zero, err
	}
	shapeDecoder := json.NewDecoder(bytes.NewReader(body))
	shapeDecoder.UseNumber()
	var shape map[string]any
	if err := shapeDecoder.Decode(&shape); err != nil {
		return zero, err
	}
	if err := requireEOF(shapeDecoder); err != nil {
		return zero, err
	}
	if err := validateShape(shape); err != nil {
		return zero, err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	var decoded T
	if err := decoder.Decode(&decoded); err != nil {
		return zero, err
	}
	if err := requireEOF(decoder); err != nil {
		return zero, err
	}
	if err := validateCanonical(decoded); err != nil {
		return zero, err
	}
	return decoded, nil
}

// ValidateRawJSONUnicode rejects byte sequences that encoding/json would
// otherwise coerce to U+FFFD. It is shared by coding-contract and runner wire
// decoders so their Unicode acceptance boundary cannot drift.
func ValidateRawJSONUnicode(body []byte) error {
	if !utf8.Valid(body) {
		return errors.New("coding JSON is not valid UTF-8")
	}
	for index := 0; index < len(body); {
		if body[index] != '\\' || index+1 >= len(body) {
			index++
			continue
		}
		if body[index+1] != 'u' {
			index += 2
			continue
		}
		codepoint, ok := escapedCodepoint(body, index)
		if !ok {
			index += 2
			continue
		}
		switch {
		case codepoint >= 0xD800 && codepoint <= 0xDBFF:
			paired, valid := escapedCodepoint(body, index+6)
			if !valid || paired < 0xDC00 || paired > 0xDFFF {
				return errors.New("coding JSON contains an unpaired high surrogate")
			}
			index += 12
		case codepoint >= 0xDC00 && codepoint <= 0xDFFF:
			return errors.New("coding JSON contains an unpaired low surrogate")
		default:
			index += 6
		}
	}
	return nil
}

func escapedCodepoint(body []byte, index int) (uint16, bool) {
	if index < 0 || index+6 > len(body) || body[index] != '\\' || body[index+1] != 'u' {
		return 0, false
	}
	decoded, err := hex.DecodeString(string(body[index+2 : index+6]))
	if err != nil || len(decoded) != 2 {
		return 0, false
	}
	return uint16(decoded[0])<<8 | uint16(decoded[1]), true
}

func requireEOF(decoder *json.Decoder) error {
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return errors.New("coding JSON contains trailing content")
		}
		return err
	}
	return nil
}

func requireFields(object map[string]any, path string, names ...string) error {
	for _, name := range names {
		if _, present := object[name]; !present {
			return fmt.Errorf("%s is missing required field %q", path, name)
		}
	}
	return nil
}

func objectField(object map[string]any, path, name string) (map[string]any, error) {
	value, ok := object[name].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s.%s must be an object", path, name)
	}
	return value, nil
}

func arrayField(object map[string]any, path, name string) ([]any, error) {
	value, ok := object[name].([]any)
	if !ok {
		return nil, fmt.Errorf("%s.%s must be an array", path, name)
	}
	return value, nil
}

func arrayObjects(object map[string]any, path, name string, validate func(map[string]any, string) error) error {
	values, err := arrayField(object, path, name)
	if err != nil {
		return err
	}
	for index, raw := range values {
		value, ok := raw.(map[string]any)
		if !ok {
			return fmt.Errorf("%s.%s[%d] must be an object", path, name, index)
		}
		if err := validate(value, fmt.Sprintf("%s.%s[%d]", path, name, index)); err != nil {
			return err
		}
	}
	return nil
}

func validateManifestTaskShape(object map[string]any, path string) error {
	return requireFields(object, path,
		"case_id", "variant_id", "profile_capability_id", "visible_bundle_sha256", "base_tree_sha256",
		"memory_bundle_sha256", "environment_image_digest", "environment_platform", "resource_profile_sha256",
		"grader_bundle_sha256", "grader_image_digest", "grader_platform", "test_manifest_sha256", "grader_plan_sha256",
	)
}

func validateRunManifestShape(object map[string]any) error {
	if err := requireFields(object, "$", "schema", "coding_contract_version", "bench_family", "weight_eligible", "coding_run_id",
		"agent_id", "agent_artifact_sha256", "corpus_release_id", "catalog_merkle_root",
		"selection_derivation_id", "selection_chain_genesis_hash", "selection_block_number",
		"selection_block_hash", "inference_grant_sha256", "grader_contract_sha256", "task_set_id",
		"task_set_manifest_sha256", "tasks"); err != nil {
		return err
	}
	return arrayObjects(object, "$", "tasks", validateManifestTaskShape)
}

func validateMemoryShape(object map[string]any, path string) error {
	return requireFields(object, path, "memory_id", "repository_capability_id", "fact_group_id", "scope", "type",
		"content", "valid_from_epoch", "valid_until_epoch", "supersedes", "confidence_micros")
}

func validateSeedRequestShape(object map[string]any) error {
	if err := requireFields(object, "$", "coding_contract_version", "ticket_id", "case_id",
		"profile_capability_id", "memory_bundle_sha256", "memories"); err != nil {
		return err
	}
	return arrayObjects(object, "$", "memories", validateMemoryShape)
}

func validateRunRequestShape(object map[string]any) error {
	if err := requireFields(object, "$", "coding_contract_version", "ticket_id", "case_id",
		"profile_capability_id", "repository_epoch", "visible_bundle_sha256", "issue", "runtime_policy",
		"workspace_capability_url", "inference_base_url", "budgets"); err != nil {
		return err
	}
	issue, err := objectField(object, "$", "issue")
	if err != nil {
		return err
	}
	if err := requireFields(issue, "$.issue", "title", "description", "constraints"); err != nil {
		return err
	}
	policy, err := objectField(object, "$", "runtime_policy")
	if err != nil {
		return err
	}
	if err := requireFields(policy, "$.runtime_policy", "editable_paths", "test_command_ids", "build_command_ids"); err != nil {
		return err
	}
	budgets, err := objectField(object, "$", "budgets")
	if err != nil {
		return err
	}
	return requireFields(budgets, "$.budgets", "model_input_tokens", "model_output_tokens", "workspace_tool_calls", "wall_time_seconds")
}

func validateModelShape(object map[string]any, path string) error {
	return requireFields(object, path, "model", "provider", "provider_route_profile", "reasoning_effort",
		"inference_grant_sha256", "prompt_sha256", "tool_schema_sha256", "usage_status", "fallback_used",
		"cost_source", "currency", "provider_receipt_set_sha256", "requests", "prompt_tokens",
		"completion_tokens", "total_tokens", "cost_usd_micros", "retry_count")
}

func validateAuthoringShape(object map[string]any, path string) error {
	if err := requireFields(object, path, "model", "authoring_event_root", "authoring_transcript_sha256",
		"frozen_patch_sha256", "changed_path_root", "final_tree_sha256", "changed_path_count", "changed_bytes",
		"protected_paths_intact"); err != nil {
		return err
	}
	model, err := objectField(object, path, "model")
	if err != nil {
		return err
	}
	return validateModelShape(model, path+".model")
}

func validateBuildShape(object map[string]any, path string) error {
	return requireFields(object, path, "command_id", "required", "passed")
}

func validateTestGroupShape(object map[string]any, path string) error {
	return requireFields(object, path, "group", "passed", "total")
}

func validateGraderShape(object map[string]any, path string) error {
	if err := requireFields(object, path, "grader_contract_sha256", "grader_bundle_sha256",
		"grader_image_digest", "grader_platform", "test_manifest_sha256", "grader_plan_sha256", "resource_profile_sha256",
		"execution_receipt_root_sha256", "execution_receipt_count",
		"grader_integrity_before_sha256", "grader_integrity_after_sha256", "build", "test_groups"); err != nil {
		return err
	}
	build, err := objectField(object, path, "build")
	if err != nil {
		return err
	}
	if err := validateBuildShape(build, path+".build"); err != nil {
		return err
	}
	return arrayObjects(object, path, "test_groups", validateTestGroupShape)
}

func validateTaskEvidenceShape(object map[string]any) error {
	if err := requireFields(object, "$", "schema", "coding_contract_version", "weight_eligible", "coding_run_id",
		"validator_ticket_id",
		"agent_id", "agent_artifact_sha256", "corpus_release_id", "task_set_id", "task_set_manifest_sha256",
		"task", "authoring", "grader", "terminal_domain", "failure_code", "repair_score_micros"); err != nil {
		return err
	}
	task, err := objectField(object, "$", "task")
	if err != nil {
		return err
	}
	if err := validateManifestTaskShape(task, "$.task"); err != nil {
		return err
	}
	if object["authoring"] != nil {
		authoring, err := objectField(object, "$", "authoring")
		if err != nil {
			return err
		}
		if err := validateAuthoringShape(authoring, "$.authoring"); err != nil {
			return err
		}
	}
	if object["grader"] != nil {
		grader, err := objectField(object, "$", "grader")
		if err != nil {
			return err
		}
		if err := validateGraderShape(grader, "$.grader"); err != nil {
			return err
		}
	}
	return nil
}

func validateTaskResultShape(object map[string]any, path string) error {
	return requireFields(object, path, "case_id", "variant_id", "task_evidence_sha256", "terminal_domain", "repair_score_micros")
}

func validateRunEvidenceShape(object map[string]any) error {
	if err := requireFields(object, "$", "schema", "coding_contract_version", "weight_eligible",
		"coding_run_id", "validator_ticket_id",
		"run_manifest_sha256", "task_set_manifest_sha256", "tasks", "resolved_count", "repair_failure_count",
		"infrastructure_count", "invalid_count", "candidate_integrity_count", "control_plane_integrity_count",
		"scoreable_task_count",
		"repair_mean_micros"); err != nil {
		return err
	}
	return arrayObjects(object, "$", "tasks", validateTaskResultShape)
}

func rejectDuplicateJSONFields(body []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	if err := scanJSONValue(decoder, 0); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return errors.New("coding JSON contains trailing content")
		}
		return err
	}
	return nil
}

func scanJSONValue(decoder *json.Decoder, depth int) error {
	if depth > 32 {
		return errors.New("coding JSON nesting exceeds 32 levels")
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
				return errors.New("coding JSON object has a non-string field name")
			}
			if _, duplicate := seen[key]; duplicate {
				return fmt.Errorf("coding JSON contains duplicate field %q", key)
			}
			seen[key] = struct{}{}
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil {
			return err
		}
		if closing != json.Delim('}') {
			return errors.New("coding JSON object is not closed")
		}
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil {
			return err
		}
		if closing != json.Delim(']') {
			return errors.New("coding JSON array is not closed")
		}
	default:
		return errors.New("coding JSON begins with an unexpected delimiter")
	}
	return nil
}
