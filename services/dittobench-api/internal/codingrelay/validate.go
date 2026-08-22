package codingrelay

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"reflect"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func validateConfig(config Config, now time.Time) (Config, error) {
	if nilLike(config.Upstream) || nilLike(config.Journal) {
		return Config{}, ErrInvalidConfig
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	if config.NewRequestID == nil {
		config.NewRequestID = uuid.NewString
	}
	if config.OperationTimeout == 0 {
		config.OperationTimeout = 5 * time.Second
	}
	if config.OperationTimeout <= 0 || config.OperationTimeout > maximumOperationTimeout {
		return Config{}, ErrInvalidConfig
	}
	config.Binding = cloneBinding(config.Binding)
	if err := config.Policy.Validate(); err != nil {
		return Config{}, ErrInvalidConfig
	}
	if err := validateBinding(config.Policy, config.Binding, now); err != nil {
		return Config{}, err
	}
	return config, nil
}

func validateBinding(policy codingcontract.InferencePolicy, binding Binding, now time.Time) error {
	grantSHA256, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil {
		return ErrInvalidConfig
	}
	issuedAt := binding.IssuedAt.UTC()
	deadline := binding.Deadline.UTC()
	if now.IsZero() {
		return ErrInvalidConfig
	}
	if !validIdentifier(binding.AttemptID, 256) || !validSHA256(binding.AgentArtifactSHA256) ||
		!validIdentifier(binding.HarnessInstanceID, 256) || !canonicalUUID(binding.TicketID) ||
		!validIdentifier(binding.CaseID, 256) || !validIdentifier(binding.ProfileCapabilityID, 256) ||
		!canonicalUUID(binding.GrantID) || binding.Generation == 0 || binding.Generation > 1<<31-1 ||
		binding.InferenceGrantSHA256 != grantSHA256 || issuedAt.IsZero() || issuedAt.After(now) ||
		deadline.IsZero() || !deadline.After(issuedAt) || deadline.After(issuedAt.Add(maximumBindingLifetime)) ||
		binding.RequestBudget == 0 ||
		binding.RequestBudget > policy.MaxRequests || binding.PromptTokenBudget == 0 ||
		binding.PromptTokenBudget > policy.MaxPromptTokens || binding.CompletionTokenBudget == 0 ||
		binding.CompletionTokenBudget > policy.MaxCompletionTokens {
		return ErrInvalidConfig
	}
	return nil
}

func evidenceBindingMatches(binding Binding, observed EvidenceBinding) bool {
	return observed.AttemptID == binding.AttemptID &&
		observed.AgentArtifactSHA256 == binding.AgentArtifactSHA256 &&
		observed.HarnessInstanceID == binding.HarnessInstanceID &&
		observed.TicketID == binding.TicketID && observed.CaseID == binding.CaseID &&
		observed.ProfileCapabilityID == binding.ProfileCapabilityID &&
		observed.InferenceGrantSHA256 == binding.InferenceGrantSHA256 &&
		observed.Deadline.Equal(binding.Deadline) &&
		observed.RequestBudget == binding.RequestBudget &&
		observed.PromptTokenBudget == binding.PromptTokenBudget &&
		observed.CompletionTokenBudget == binding.CompletionTokenBudget
}

func bindingMatches(left, right Binding) bool {
	return left.AttemptID == right.AttemptID && left.AgentArtifactSHA256 == right.AgentArtifactSHA256 &&
		left.HarnessInstanceID == right.HarnessInstanceID && left.TicketID == right.TicketID &&
		left.CaseID == right.CaseID && left.ProfileCapabilityID == right.ProfileCapabilityID &&
		left.GrantID == right.GrantID && left.Generation == right.Generation &&
		left.InferenceGrantSHA256 == right.InferenceGrantSHA256 && left.IssuedAt.Equal(right.IssuedAt) &&
		left.Deadline.Equal(right.Deadline) && left.RequestBudget == right.RequestBudget &&
		left.PromptTokenBudget == right.PromptTokenBudget &&
		left.CompletionTokenBudget == right.CompletionTokenBudget
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
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

func errorCode(err error) string {
	switch {
	case errors.Is(err, ErrInvalidRequest):
		return "invalid_request"
	case errors.Is(err, ErrCapabilityRevoked):
		return "capability_revoked"
	case errors.Is(err, ErrCapabilityExpired):
		return "capability_expired"
	case errors.Is(err, ErrConcurrentRequest):
		return "concurrent_request"
	case errors.Is(err, ErrBudgetExhausted):
		return "budget_exhausted"
	case errors.Is(err, ErrProviderFailure):
		return "provider_failure"
	default:
		return "relay_unavailable"
	}
}

func safeErrorText(err error) string {
	return strings.ReplaceAll(errorCode(err), "_", " ")
}
