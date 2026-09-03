package codingcontract

import (
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
)

const ExecutorControlSchema = "dittobench-coding-executor-control-v1"

var (
	executorHotkey     = regexp.MustCompile(`^[1-9A-HJ-NP-Za-km-z]{47,48}$`)
	executorSignature  = regexp.MustCompile(`^[0-9a-fA-F]{128}$`)
	executorOperations = map[string]struct{}{
		"supervisor.prepare": {}, "supervisor.author": {}, "supervisor.grade": {},
		"supervisor.abort-authoring": {}, "supervisor.abort-grading": {}, "supervisor.recover": {},
		"publications.prepare": {}, "publications.acknowledge": {}, "publications.pending": {},
		"publications.open": {}, "publications.lookup": {},
	}
)

type ExecutorControlEnvelope struct {
	Schema                string    `json:"schema"`
	CodingContractVersion int       `json:"coding_contract_version"`
	WeightEligible        bool      `json:"weight_eligible"`
	ValidatorHotkey       string    `json:"validator_hotkey"`
	AgentID               string    `json:"agent_id"`
	AgentArtifactSHA256   string    `json:"agent_artifact_sha256"`
	CodingRunID           string    `json:"coding_run_id"`
	TicketID              string    `json:"ticket_id"`
	Operation             string    `json:"operation"`
	Method                string    `json:"method"`
	RequestBodySHA256     string    `json:"request_body_sha256"`
	Nonce                 string    `json:"nonce"`
	IssuedAt              time.Time `json:"issued_at"`
	ExpiresAt             time.Time `json:"expires_at"`
	Signature             string    `json:"signature"`
}

func (value ExecutorControlEnvelope) Validate() error {
	_, operationOK := executorOperations[value.Operation]
	if value.Schema != ExecutorControlSchema || value.CodingContractVersion != ContractVersion || value.WeightEligible ||
		!executorHotkey.MatchString(value.ValidatorHotkey) || uuid.Validate(value.AgentID) != nil ||
		uuid.Validate(value.TicketID) != nil || uuid.Validate(value.Nonce) != nil ||
		value.AgentID == uuid.Nil.String() || value.TicketID == uuid.Nil.String() || value.Nonce == uuid.Nil.String() ||
		!validSHA256(value.AgentArtifactSHA256) || !validSHA256(value.RequestBodySHA256) ||
		!validIdentifier(value.CodingRunID, 256) || !operationOK || value.Method != "POST" ||
		!executorSignature.MatchString(value.Signature) || value.IssuedAt.Nanosecond() != 0 || value.ExpiresAt.Nanosecond() != 0 ||
		!value.IssuedAt.Before(value.ExpiresAt) || value.ExpiresAt.Sub(value.IssuedAt) > 2*time.Minute {
		return errors.New("coding executor control authority is invalid")
	}
	return nil
}

func ExecutorControlSigningMessage(value ExecutorControlEnvelope) ([]byte, error) {
	if err := value.Validate(); err != nil {
		return nil, err
	}
	fields := []string{
		"dittobench-coding-executor-control:v1", value.ValidatorHotkey, value.AgentID,
		value.AgentArtifactSHA256, value.CodingRunID, value.TicketID, value.Operation,
		value.Method, value.RequestBodySHA256, value.Nonce,
		value.IssuedAt.UTC().Format("2006-01-02T15:04:05.000000-07:00"),
		value.ExpiresAt.UTC().Format("2006-01-02T15:04:05.000000-07:00"),
	}
	return []byte(strings.Join(fields, "\x00")), nil
}
