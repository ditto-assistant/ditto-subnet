package codingsupervisor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

var operationPaths = map[string]Operation{
	"/v1/coding/supervisor/prepare":         OperationPrepare,
	"/v1/coding/supervisor/author":          OperationAuthor,
	"/v1/coding/supervisor/grade":           OperationGrade,
	"/v1/coding/supervisor/abort-authoring": OperationAbortAuthoring,
	"/v1/coding/supervisor/abort-grading":   OperationAbortGrading,
	"/v1/coding/supervisor/recover":         OperationRecover,
}

func New(config Config) (*Service, error) {
	if nilLike(config.Backend) || !validControlToken(config.ControlToken) ||
		config.OperationTimeout < 0 || config.OperationTimeout > maximumTimeout {
		return nil, ErrInvalidConfig
	}
	if config.OperationTimeout == 0 {
		config.OperationTimeout = defaultTimeout
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	now := config.Now().UTC()
	if now.IsZero() {
		return nil, ErrInvalidConfig
	}
	return &Service{
		backend: config.Backend, now: config.Now, timeout: config.OperationTimeout,
		token: sha256.Sum256([]byte(config.ControlToken)), lastNow: now,
		active: make(map[string]struct{}),
	}, nil
}

func (service *Service) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		setPrivateHeaders(response)
		if service == nil {
			writeError(response, http.StatusServiceUnavailable, "unavailable")
			return
		}
		operation, pathOK := operationPaths[request.URL.Path]
		if !pathOK || request.URL.RawQuery != "" {
			writeError(response, http.StatusNotFound, "not_found")
			return
		}
		if request.Method != http.MethodPost {
			response.Header().Set("Allow", http.MethodPost)
			writeError(response, http.StatusMethodNotAllowed, "method_not_allowed")
			return
		}
		if !service.authorized(request) {
			writeError(response, http.StatusUnauthorized, "unauthorized")
			return
		}
		mediaType, _, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/json" || request.Header.Get("Content-Encoding") != "" {
			writeError(response, http.StatusUnsupportedMediaType, "unsupported_media_type")
			return
		}
		if request.ContentLength > maximumRequestBytes {
			writeError(response, http.StatusRequestEntityTooLarge, "request_too_large")
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, maximumRequestBytes))
		if err != nil {
			writeError(response, http.StatusRequestEntityTooLarge, "request_too_large")
			return
		}
		now, err := service.trustedNow()
		if err != nil {
			writeError(response, http.StatusServiceUnavailable, "clock")
			return
		}
		value, err := parseRequest(body, operation, now)
		if err != nil {
			writeError(response, statusForError(err), errorCode(err))
			return
		}
		key := value.TicketID + ":" + value.CodingRunID
		backend, beginErr := service.begin(key)
		if beginErr != nil {
			writeError(response, statusForError(beginErr), errorCode(beginErr))
			return
		}
		defer service.release(key)
		operationContext, cancel := service.operationContext(request.Context(), value, now)
		if callErr := operationContext.Err(); callErr != nil {
			cancel()
			writeError(response, statusForError(callErr), errorCode(callErr))
			return
		}
		result, backendErr := backend.Execute(operationContext, cloneRequest(value))
		callErr := operationContext.Err()
		cancel()
		if backendErr != nil || callErr != nil {
			if callErr != nil {
				backendErr = callErr
			}
			writeError(response, statusForError(backendErr), errorCode(backendErr))
			return
		}
		result = cloneResponse(result)
		if err := validateResponse(value, result); err != nil {
			writeError(response, http.StatusBadGateway, "backend_invalid")
			return
		}
		encoded, err := json.Marshal(result)
		if err != nil || len(encoded)+1 > maximumResponseBytes {
			writeError(response, http.StatusBadGateway, "backend_invalid")
			return
		}
		encoded = append(encoded, '\n')
		response.Header().Set("Content-Length", strconv.Itoa(len(encoded)))
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(encoded)
	})
}

func (service *Service) Close() error {
	if service == nil {
		return nil
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	if len(service.active) != 0 {
		return ErrConcurrent
	}
	service.closed = true
	service.backend = nil
	clear(service.active)
	service.token = [sha256.Size]byte{}
	return nil
}

func (service *Service) authorized(request *http.Request) bool {
	values := request.Header.Values("Authorization")
	if len(values) != 1 || !strings.HasPrefix(values[0], "Bearer ") {
		return false
	}
	token := strings.TrimPrefix(values[0], "Bearer ")
	digest := sha256.Sum256([]byte(token))
	service.mu.Lock()
	expected := service.token
	closed := service.closed
	service.mu.Unlock()
	return !closed && subtle.ConstantTimeCompare(digest[:], expected[:]) == 1
}

func (service *Service) begin(key string) (Backend, error) {
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.closed {
		return nil, ErrClosed
	}
	if _, exists := service.active[key]; exists {
		return nil, ErrConcurrent
	}
	service.active[key] = struct{}{}
	return service.backend, nil
}

func (service *Service) release(key string) {
	service.mu.Lock()
	delete(service.active, key)
	service.mu.Unlock()
}

func (service *Service) operationContext(
	parent context.Context,
	request Request,
	now time.Time,
) (context.Context, context.CancelFunc) {
	duration := service.timeout
	if request.Operation == OperationPrepare || request.Operation == OperationAuthor || request.Operation == OperationGrade {
		remaining := request.Deadline.Sub(now)
		if remaining < duration {
			duration = remaining
		}
	}
	return context.WithTimeout(parent, duration)
}

func (service *Service) trustedNow() (time.Time, error) {
	now := service.now().UTC()
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.closed {
		return time.Time{}, ErrClosed
	}
	if now.IsZero() || now.Before(service.lastNow) {
		return time.Time{}, ErrDeadline
	}
	service.lastNow = now
	return now, nil
}

func parseRequest(body []byte, operation Operation, now time.Time) (Request, error) {
	var zero Request
	if len(body) == 0 || len(body) > maximumRequestBytes ||
		codingcontract.ValidateJSONDocument(body, maximumRequestBytes) != nil {
		return zero, ErrInvalid
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	var shape map[string]json.RawMessage
	if err := decoder.Decode(&shape); err != nil {
		return zero, ErrInvalid
	}
	for _, field := range []string{"schema", "operation", "operation_id", "ticket_id", "coding_run_id", "deadline", "lease", "authoring", "grant", "harness"} {
		if _, ok := shape[field]; !ok {
			return zero, ErrInvalid
		}
	}
	var request Request
	if err := json.Unmarshal(body, &request); err != nil || request.Schema != RequestSchema ||
		request.Operation != operation || !canonicalUUID(request.OperationID) ||
		!canonicalUUID(request.TicketID) || !validIdentifier(request.CodingRunID, 256) ||
		request.Deadline.IsZero() {
		return zero, ErrInvalid
	}
	if (operation == OperationPrepare || operation == OperationAuthor || operation == OperationGrade) &&
		!request.Deadline.After(now) {
		return zero, ErrDeadline
	}
	if (operation == OperationPrepare || operation == OperationAuthor || operation == OperationGrade) &&
		request.Deadline.After(now.Add(maximumBindingLifetime)) {
		return zero, ErrInvalid
	}
	leaseObject := rawObject(request.Lease, maximumLeaseBytes)
	authoringObject := rawObject(request.Authoring, maximumOutcomeBytes)
	grantObject := rawObject(request.Grant, maximumLeaseBytes)
	harnessObject := rawObject(request.Harness, maximumLeaseBytes)
	switch operation {
	case OperationPrepare:
		if !leaseObject || authoringObject || grantObject || harnessObject || !rawNull(request.Authoring) ||
			!rawNull(request.Grant) || !rawNull(request.Harness) {
			return zero, ErrInvalid
		}
	case OperationAuthor:
		if !leaseObject || authoringObject || !grantObject || !harnessObject || !rawNull(request.Authoring) ||
			validateGrant(request.Grant, request, now) != nil || validateHarness(request.Harness, request, now) != nil {
			return zero, ErrInvalid
		}
	case OperationAbortAuthoring, OperationAbortGrading:
		if !leaseObject || authoringObject || grantObject || harnessObject || !rawNull(request.Authoring) ||
			!rawNull(request.Grant) || !rawNull(request.Harness) {
			return zero, ErrInvalid
		}
	case OperationGrade:
		if !leaseObject || !authoringObject || grantObject || harnessObject || !rawNull(request.Grant) ||
			!rawNull(request.Harness) {
			return zero, ErrInvalid
		}
	case OperationRecover:
		if !rawNull(request.Lease) || !rawNull(request.Authoring) || !rawNull(request.Grant) ||
			!rawNull(request.Harness) {
			return zero, ErrInvalid
		}
	default:
		return zero, ErrInvalid
	}
	request.Lease = append(json.RawMessage(nil), request.Lease...)
	request.Authoring = append(json.RawMessage(nil), request.Authoring...)
	request.Grant = append(json.RawMessage(nil), request.Grant...)
	request.Harness = append(json.RawMessage(nil), request.Harness...)
	return request, nil
}

func validateResponse(request Request, response Response) error {
	if response.Schema != ResponseSchema || response.Operation != request.Operation ||
		response.OperationID != request.OperationID || response.TicketID != request.TicketID ||
		response.CodingRunID != request.CodingRunID {
		return ErrConflict
	}
	switch request.Operation {
	case OperationPrepare:
		if response.Preparation == nil || response.Authoring != nil || response.Grading != nil ||
			response.Recovery != nil || response.Aborted || validatePreparation(*response.Preparation) != nil {
			return ErrConflict
		}
	case OperationAuthor:
		if response.Preparation != nil || response.Authoring == nil || response.Grading != nil || response.Recovery != nil || response.Aborted ||
			validateAuthoring(*response.Authoring) != nil {
			return ErrConflict
		}
	case OperationGrade:
		if response.Preparation != nil || response.Authoring != nil || response.Grading == nil || response.Recovery != nil || response.Aborted ||
			validateGrading(*response.Grading, request.CodingRunID, request.TicketID) != nil {
			return ErrConflict
		}
	case OperationAbortAuthoring, OperationAbortGrading:
		if response.Preparation != nil || response.Authoring != nil || response.Grading != nil || response.Recovery != nil || !response.Aborted {
			return ErrConflict
		}
	case OperationRecover:
		if response.Preparation != nil || response.Authoring != nil || response.Grading != nil || response.Recovery == nil || response.Aborted ||
			validateRecovery(*response.Recovery) != nil {
			return ErrConflict
		}
	default:
		return ErrConflict
	}
	return nil
}

func validatePreparation(outcome PreparationOutcome) error {
	if !canonicalUUID(outcome.SessionID) || !validBrokerKey(outcome.BrokerPublicKey) {
		return ErrConflict
	}
	return nil
}

func validateAuthoring(outcome AuthoringOutcome) error {
	if !rawObject(outcome.Evidence, maximumOutcomeBytes) ||
		!contentAddressedKey(outcome.AuthoringTranscriptObjectKey) || outcome.AuthoringTranscriptBytes <= 0 ||
		outcome.AuthoringTranscriptBytes > 512<<20 || outcome.AuthoringEventCount == 0 ||
		outcome.AuthoringEventCount > 1_000 || !contentAddressedKey(outcome.FrozenSubmissionObjectKey) ||
		!outcome.CapabilitiesRevoked || !outcome.AuthoringEnvironmentDestroyed {
		return ErrConflict
	}
	var evidence codingcontract.AuthoringEvidence
	if err := json.Unmarshal(outcome.Evidence, &evidence); err != nil || evidence.Validate() != nil {
		return ErrConflict
	}
	if outcome.AuthoringTranscriptObjectKey != "sha256/"+evidence.AuthoringTranscriptSHA256 ||
		outcome.FrozenSubmissionObjectKey != "sha256/"+evidence.FrozenPatchSHA256 {
		return ErrConflict
	}
	return nil
}

func validateGrading(outcome GradingOutcome, codingRunID, ticketID string) error {
	if len(outcome.TaskEvidence) == 0 || len(outcome.TaskEvidence) > 100 || !outcome.GradingEnvironmentDestroyed {
		return ErrConflict
	}
	previous := ""
	for _, evidence := range outcome.TaskEvidence {
		if !rawObject(evidence, maximumOutcomeBytes) {
			return ErrConflict
		}
		var value codingcontract.TaskEvidence
		if err := json.Unmarshal(evidence, &value); err != nil || value.Validate() != nil ||
			value.CodingRunID != codingRunID || value.ValidatorTicketID != ticketID {
			return ErrConflict
		}
		identity := value.Task.CaseID + "\x00" + value.Task.VariantID
		if previous != "" && identity <= previous {
			return ErrConflict
		}
		previous = identity
	}
	return nil
}

func validateRecovery(outcome RecoveryOutcome) error {
	validState := outcome.State == "none" || outcome.State == "authoring_pending" ||
		outcome.State == "terminal_pending" || outcome.State == "released" ||
		outcome.State == "ambiguous" || outcome.State == "expired"
	if !validState {
		return ErrConflict
	}
	pending := outcome.State == "authoring_pending" || outcome.State == "terminal_pending"
	if pending {
		if outcome.PublicationStage == nil || outcome.RequestSHA256 == nil ||
			(outcome.State == "authoring_pending" && *outcome.PublicationStage != "authoring_freeze") ||
			(outcome.State == "terminal_pending" && *outcome.PublicationStage != "terminal_result") ||
			!lowerSHA256(*outcome.RequestSHA256) {
			return ErrConflict
		}
		return nil
	}
	if outcome.PublicationStage != nil || outcome.RequestSHA256 != nil {
		return ErrConflict
	}
	return nil
}

func cloneRequest(request Request) Request {
	request.Lease = append(json.RawMessage(nil), request.Lease...)
	request.Authoring = append(json.RawMessage(nil), request.Authoring...)
	request.Grant = append(json.RawMessage(nil), request.Grant...)
	request.Harness = append(json.RawMessage(nil), request.Harness...)
	return request
}

func cloneResponse(response Response) Response {
	if response.Preparation != nil {
		value := *response.Preparation
		response.Preparation = &value
	}
	if response.Authoring != nil {
		value := *response.Authoring
		value.Evidence = append(json.RawMessage(nil), value.Evidence...)
		response.Authoring = &value
	}
	if response.Grading != nil {
		value := *response.Grading
		value.TaskEvidence = append([]json.RawMessage(nil), value.TaskEvidence...)
		for index := range value.TaskEvidence {
			value.TaskEvidence[index] = append(json.RawMessage(nil), value.TaskEvidence[index]...)
		}
		response.Grading = &value
	}
	if response.Recovery != nil {
		value := *response.Recovery
		if value.PublicationStage != nil {
			stage := *value.PublicationStage
			value.PublicationStage = &stage
		}
		if value.RequestSHA256 != nil {
			digest := *value.RequestSHA256
			value.RequestSHA256 = &digest
		}
		response.Recovery = &value
	}
	return response
}

type exchangedGrant struct {
	Schema                string    `json:"schema"`
	CodingContractVersion int       `json:"coding_contract_version"`
	WeightEligible        *bool     `json:"weight_eligible"`
	Status                string    `json:"status"`
	GrantID               string    `json:"grant_id"`
	TicketID              string    `json:"ticket_id"`
	CaseID                string    `json:"case_id"`
	ProfileCapabilityID   string    `json:"profile_capability_id"`
	InferenceGrantSHA256  string    `json:"inference_grant_sha256"`
	Generation            uint32    `json:"generation"`
	RequestBudget         uint32    `json:"request_budget"`
	PromptTokenBudget     uint64    `json:"prompt_token_budget"`
	CompletionTokenBudget uint64    `json:"completion_token_budget"`
	Bearer                string    `json:"bearer"`
	ProxyURL              string    `json:"proxy_url"`
	RevokeBearer          string    `json:"revoke_bearer"`
	RevokeURL             string    `json:"revoke_url"`
	ExpiresAt             time.Time `json:"expires_at"`
}

type leaseGrantAuthority struct {
	RunManifest struct {
		AgentID              string `json:"agent_id"`
		AgentArtifactSHA256  string `json:"agent_artifact_sha256"`
		InferenceGrantSHA256 string `json:"inference_grant_sha256"`
		Tasks                []struct {
			CaseID              string `json:"case_id"`
			ProfileCapabilityID string `json:"profile_capability_id"`
		} `json:"tasks"`
	} `json:"run_manifest"`
	Budgets struct {
		ModelInputTokens   uint64 `json:"model_input_tokens"`
		ModelOutputTokens  uint64 `json:"model_output_tokens"`
		WorkspaceToolCalls uint32 `json:"workspace_tool_calls"`
	} `json:"budgets"`
}

type screenedHarness struct {
	Schema                 string    `json:"schema"`
	CodingContractVersion  int       `json:"coding_contract_version"`
	WeightEligible         *bool     `json:"weight_eligible"`
	AgentID                string    `json:"agent_id"`
	RunRowID               string    `json:"run_row_id"`
	TicketID               string    `json:"ticket_id"`
	TicketDeadline         time.Time `json:"ticket_deadline"`
	BenchVersion           int       `json:"bench_version"`
	AgentArtifactSHA256    string    `json:"agent_artifact_sha256"`
	ScreenedImageSHA256    string    `json:"screened_image_sha256"`
	ScreenedImageSizeBytes int64     `json:"screened_image_size_bytes"`
	ScreenedImageID        string    `json:"screened_image_id"`
	ScreenedImageRef       string    `json:"screened_image_ref"`
	ScreeningPolicyVersion int       `json:"screening_policy_version"`
	ImageURL               string    `json:"image_url"`
	ExpiresAt              time.Time `json:"expires_at"`
}

func validateHarness(body json.RawMessage, request Request, now time.Time) error {
	var harness screenedHarness
	var lease leaseGrantAuthority
	if err := json.Unmarshal(body, &harness); err != nil || json.Unmarshal(request.Lease, &lease) != nil {
		return ErrInvalid
	}
	if harness.Schema != "dittobench-coding-harness-launch-v1" || harness.CodingContractVersion != 1 ||
		harness.WeightEligible == nil || *harness.WeightEligible || !canonicalUUID(harness.AgentID) ||
		!canonicalUUID(harness.RunRowID) || harness.AgentID != lease.RunManifest.AgentID ||
		harness.AgentArtifactSHA256 != lease.RunManifest.AgentArtifactSHA256 ||
		harness.TicketID != request.TicketID || !harness.TicketDeadline.Equal(request.Deadline) ||
		harness.BenchVersion < 7 || harness.BenchVersion > 1_000_000 ||
		!lowerSHA256(harness.ScreenedImageSHA256) || harness.ScreenedImageSizeBytes <= 0 ||
		harness.ScreenedImageSizeBytes > 8<<30 || !strings.HasPrefix(harness.ScreenedImageID, "sha256:") ||
		!lowerSHA256(strings.TrimPrefix(harness.ScreenedImageID, "sha256:")) ||
		!validIdentifier(harness.ScreenedImageRef, 512) || harness.ScreeningPolicyVersion < 9 ||
		harness.ScreeningPolicyVersion > 1_000_000 || !validImageURL(harness.ImageURL) ||
		harness.ExpiresAt.IsZero() || !harness.ExpiresAt.After(now) || harness.ExpiresAt.After(request.Deadline) {
		return ErrInvalid
	}
	return nil
}

func validateGrant(body json.RawMessage, request Request, now time.Time) error {
	var grant exchangedGrant
	var lease leaseGrantAuthority
	if err := json.Unmarshal(request.Lease, &lease); err != nil || len(lease.RunManifest.Tasks) != 1 {
		return ErrInvalid
	}
	task := lease.RunManifest.Tasks[0]
	requestBudget := lease.Budgets.WorkspaceToolCalls + 16
	if requestBudget > 256 || requestBudget < lease.Budgets.WorkspaceToolCalls {
		requestBudget = 256
	}
	if err := json.Unmarshal(body, &grant); err != nil ||
		grant.Schema != "dittobench-coding-inference-exchange-v1" || grant.CodingContractVersion != 1 ||
		grant.WeightEligible == nil || *grant.WeightEligible || grant.Status != "active" ||
		!canonicalUUID(grant.GrantID) ||
		grant.TicketID != request.TicketID || grant.CaseID != task.CaseID ||
		grant.ProfileCapabilityID != task.ProfileCapabilityID ||
		grant.InferenceGrantSHA256 != lease.RunManifest.InferenceGrantSHA256 ||
		grant.Generation == 0 || grant.Generation > 1<<31-1 ||
		grant.RequestBudget == 0 || grant.RequestBudget > requestBudget ||
		grant.PromptTokenBudget == 0 || grant.PromptTokenBudget > lease.Budgets.ModelInputTokens ||
		grant.CompletionTokenBudget == 0 || grant.CompletionTokenBudget > lease.Budgets.ModelOutputTokens ||
		!validBearer(grant.Bearer) || !validProxyURL(grant.ProxyURL) ||
		!validBearer(grant.RevokeBearer) || grant.RevokeBearer == grant.Bearer || !validRevokeURL(grant.RevokeURL) ||
		grant.ExpiresAt.IsZero() || !grant.ExpiresAt.After(now) || grant.ExpiresAt.After(request.Deadline) {
		return ErrInvalid
	}
	return nil
}

func validBrokerKey(value string) bool {
	decoded, err := base64.RawURLEncoding.DecodeString(strings.TrimSuffix(value, "="))
	return err == nil && len(decoded) == 32 && len(value) >= 43 && len(value) <= 44
}

func validBearer(value string) bool {
	if len(value) < 32 || len(value) > 128 {
		return false
	}
	for _, character := range value {
		if !(character >= 'a' && character <= 'z') && !(character >= 'A' && character <= 'Z') &&
			!(character >= '0' && character <= '9') && character != '_' && character != '-' {
			return false
		}
	}
	return true
}

func validProxyURL(value string) bool {
	if len(value) == 0 || len(value) > 2_048 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil ||
		parsed.RawQuery != "" || parsed.Fragment != "" {
		return false
	}
	port := parsed.Port()
	return parsed.Hostname() != "" && (port == "" || port == "443") &&
		strings.HasSuffix(parsed.Path, "/api/v1/inference/coding/chat/completions")
}

func validRevokeURL(value string) bool {
	if len(value) == 0 || len(value) > 2_048 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil ||
		parsed.RawQuery != "" || parsed.Fragment != "" {
		return false
	}
	port := parsed.Port()
	return parsed.Hostname() != "" && (port == "" || port == "443") &&
		strings.HasSuffix(parsed.Path, "/api/v1/validator/coding-shadow/inference-revoke-capability")
}

func validImageURL(value string) bool {
	if len(value) == 0 || len(value) > 16<<10 {
		return false
	}
	for _, character := range value {
		if character < 32 || character > 126 {
			return false
		}
	}
	parsed, err := url.ParseRequestURI(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil ||
		parsed.RawQuery == "" || parsed.Fragment != "" {
		return false
	}
	port := parsed.Port()
	return parsed.Hostname() != "" && (port == "" || port == "443")
}

func rawObject(value json.RawMessage, maximum int) bool {
	trimmed := bytes.TrimSpace(value)
	return len(trimmed) >= 2 && len(trimmed) <= maximum && trimmed[0] == '{' && trimmed[len(trimmed)-1] == '}' &&
		codingcontract.ValidateJSONDocument(trimmed, maximum) == nil
}

func rawNull(value json.RawMessage) bool { return bytes.Equal(bytes.TrimSpace(value), []byte("null")) }

func setPrivateHeaders(response http.ResponseWriter) {
	response.Header().Set("Cache-Control", "no-store")
	response.Header().Set("Content-Type", "application/json")
	response.Header().Set("X-Content-Type-Options", "nosniff")
}

func writeError(response http.ResponseWriter, status int, code string) {
	body, _ := json.Marshal(map[string]any{"error": map[string]string{
		"code": code, "message": strings.ReplaceAll(code, "_", " "),
	}})
	body = append(body, '\n')
	response.Header().Set("Content-Length", strconv.Itoa(len(body)))
	response.WriteHeader(status)
	_, _ = response.Write(body)
}

func statusForError(err error) int {
	switch {
	case errors.Is(err, ErrInvalid):
		return http.StatusBadRequest
	case errors.Is(err, ErrUnauthorized):
		return http.StatusUnauthorized
	case errors.Is(err, ErrConcurrent), errors.Is(err, ErrConflict):
		return http.StatusConflict
	case errors.Is(err, ErrDeadline), errors.Is(err, context.DeadlineExceeded):
		return http.StatusGatewayTimeout
	case errors.Is(err, context.Canceled):
		return http.StatusRequestTimeout
	default:
		return http.StatusServiceUnavailable
	}
}

func errorCode(err error) string {
	switch {
	case errors.Is(err, ErrInvalid):
		return "invalid_request"
	case errors.Is(err, ErrUnauthorized):
		return "unauthorized"
	case errors.Is(err, ErrConcurrent):
		return "concurrent"
	case errors.Is(err, ErrConflict):
		return "conflict"
	case errors.Is(err, ErrDeadline), errors.Is(err, context.DeadlineExceeded):
		return "deadline"
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, ErrClosed):
		return "closed"
	default:
		return "unavailable"
	}
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func contentAddressedKey(value string) bool {
	return strings.HasPrefix(value, "sha256/") && lowerSHA256(strings.TrimPrefix(value, "sha256/"))
}

func lowerSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func validControlToken(value string) bool {
	return len(value) >= 32 && len(value) <= 256 && validIdentifier(value, 256)
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}
