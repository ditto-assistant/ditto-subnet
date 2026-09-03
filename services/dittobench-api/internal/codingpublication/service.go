// Package codingpublication exposes the durable coding outbox publication
// transitions to the trusted Python validator over a private authenticated
// local HTTP contract. It never signs, contacts Platform, or executes a miner.
package codingpublication

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/google/uuid"
)

const (
	commandSchema        = "dittobench-coding-publication-command-v1"
	responseSchema       = "dittobench-coding-publication-result-v1"
	maximumCommandBytes  = 6 << 20
	maximumResponseBytes = 32 << 20
)

var (
	ErrInvalid = errors.New("coding publication command is invalid")
	ErrClosed  = errors.New("coding publication service is closed")
)

type Config struct {
	Store        *codingoutbox.Store
	ControlToken string
}

type Service struct {
	mu sync.Mutex

	store  *codingoutbox.Store
	token  [sha256.Size]byte
	active int
	closed bool
}

type prepareCommand struct {
	Schema     string                            `json:"schema"`
	TicketID   string                            `json:"ticket_id"`
	Stage      codingoutbox.PublicationStage     `json:"stage"`
	Authority  codingoutbox.PublicationAuthority `json:"authority"`
	BodyBase64 string                            `json:"body_base64"`
}

type acknowledgeCommand struct {
	Schema        string                        `json:"schema"`
	TicketID      string                        `json:"ticket_id"`
	Stage         codingoutbox.PublicationStage `json:"stage"`
	RequestSHA256 string                        `json:"request_sha256"`
	BodyBase64    string                        `json:"body_base64"`
}

type pendingCommand struct {
	Schema              string `json:"schema"`
	Limit               int    `json:"limit"`
	AgentID             string `json:"agent_id"`
	AgentArtifactSHA256 string `json:"agent_artifact_sha256"`
	TicketID            string `json:"ticket_id"`
	CodingRunID         string `json:"coding_run_id"`
}

type openCommand struct {
	Schema              string                        `json:"schema"`
	RecordID            string                        `json:"record_id"`
	Stage               codingoutbox.PublicationStage `json:"stage"`
	Acknowledgement     bool                          `json:"acknowledgement"`
	AgentID             string                        `json:"agent_id"`
	AgentArtifactSHA256 string                        `json:"agent_artifact_sha256"`
	TicketID            string                        `json:"ticket_id"`
	CodingRunID         string                        `json:"coding_run_id"`
}

type lookupCommand struct {
	Schema   string                        `json:"schema"`
	TicketID string                        `json:"ticket_id"`
	Stage    codingoutbox.PublicationStage `json:"stage"`
}

type pendingResult struct {
	RecordID  string                            `json:"record_id"`
	TicketID  string                            `json:"ticket_id"`
	Stage     codingoutbox.PublicationStage     `json:"stage"`
	Authority codingoutbox.PublicationAuthority `json:"authority"`
	Request   codingoutbox.PublicationArtifact  `json:"request"`
}

type publicationResult struct {
	RecordID        string                            `json:"record_id"`
	TicketID        string                            `json:"ticket_id"`
	Stage           codingoutbox.PublicationStage     `json:"stage"`
	Authority       codingoutbox.PublicationAuthority `json:"authority"`
	Request         codingoutbox.PublicationArtifact  `json:"request"`
	Acknowledgement *codingoutbox.PublicationArtifact `json:"acknowledgement,omitempty"`
}

type result struct {
	Schema                string                            `json:"schema"`
	CodingContractVersion int                               `json:"coding_contract_version"`
	WeightEligible        bool                              `json:"weight_eligible"`
	Operation             string                            `json:"operation"`
	RecordID              string                            `json:"record_id,omitempty"`
	Artifact              *codingoutbox.PublicationArtifact `json:"artifact,omitempty"`
	Pending               []pendingResult                   `json:"pending"`
	Publication           *publicationResult                `json:"publication,omitempty"`
	BodyBase64            string                            `json:"body_base64,omitempty"`
}

func New(config Config) (*Service, error) {
	if config.Store == nil || !validControlToken(config.ControlToken) {
		return nil, ErrInvalid
	}
	return &Service{
		store: config.Store,
		token: sha256.Sum256([]byte(config.ControlToken)),
	}, nil
}

func (service *Service) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.Header().Set("Content-Type", "application/json")
		response.Header().Set("X-Content-Type-Options", "nosniff")
		operation := strings.TrimPrefix(request.URL.Path, "/v1/coding/publications/")
		if request.Method != http.MethodPost || request.URL.RawQuery != "" ||
			!validOperation(operation) {
			writeError(response, http.StatusNotFound, "not_found")
			return
		}
		if !service.authorized(request) {
			writeError(response, http.StatusUnauthorized, "unauthorized")
			return
		}
		defer service.release()
		mediaType, _, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/json" || request.Header.Get("Content-Encoding") != "" {
			writeError(response, http.StatusUnsupportedMediaType, "unsupported_media_type")
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, maximumCommandBytes))
		if err != nil || codingcontract.ValidateJSONDocument(body, maximumCommandBytes) != nil {
			writeError(response, http.StatusBadRequest, "invalid_command")
			return
		}
		value, err := service.execute(request.Context(), operation, body)
		if err != nil {
			writeError(response, statusForError(err), codeForError(err))
			return
		}
		encoded, err := json.Marshal(value)
		if err != nil {
			writeError(response, http.StatusBadGateway, "response_invalid")
			return
		}
		for value.Operation == "pending" && len(encoded)+1 > maximumResponseBytes && len(value.Pending) > 1 {
			value.Pending = value.Pending[:len(value.Pending)/2]
			encoded, err = json.Marshal(value)
			if err != nil {
				writeError(response, http.StatusBadGateway, "response_invalid")
				return
			}
		}
		if len(encoded)+1 > maximumResponseBytes {
			writeError(response, http.StatusBadGateway, "response_invalid")
			return
		}
		encoded = append(encoded, '\n')
		response.Header().Set("Content-Length", strconv.Itoa(len(encoded)))
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(encoded)
	})
}

func (service *Service) execute(ctx context.Context, operation string, body []byte) (result, error) {
	base := result{
		Schema: responseSchema, CodingContractVersion: codingcontract.ContractVersion,
		WeightEligible: false, Operation: operation,
	}
	switch operation {
	case "prepare":
		var command prepareCommand
		if decodeRequired(body, &command, "schema", "ticket_id", "stage", "authority", "body_base64") != nil ||
			command.Schema != commandSchema || !validTicketID(command.TicketID) || !validStage(command.Stage) {
			return result{}, ErrInvalid
		}
		raw, err := decodeBody(command.BodyBase64, 4<<20)
		if err != nil {
			return result{}, err
		}
		attempt, _, err := service.store.Lookup(ctx, codingoutbox.PurposeShadowAttempt, command.TicketID)
		if err != nil {
			return result{}, err
		}
		var artifact codingoutbox.PublicationArtifact
		if command.Stage == codingoutbox.PublicationAuthoringFreeze {
			artifact, err = attempt.PrepareAuthoringPublication(ctx, command.Authority, raw)
		} else {
			artifact, err = attempt.PrepareTerminalPublication(ctx, command.Authority, raw)
		}
		if err != nil {
			return result{}, err
		}
		base.RecordID = attempt.ID()
		base.Artifact = &artifact
		return base, nil
	case "acknowledge":
		var command acknowledgeCommand
		if decodeRequired(body, &command, "schema", "ticket_id", "stage", "request_sha256", "body_base64") != nil ||
			command.Schema != commandSchema || !validTicketID(command.TicketID) || !validStage(command.Stage) {
			return result{}, ErrInvalid
		}
		raw, err := decodeBody(command.BodyBase64, 1<<20)
		if err != nil {
			return result{}, err
		}
		attempt, record, err := service.store.Lookup(ctx, codingoutbox.PurposeShadowAttempt, command.TicketID)
		if err != nil {
			return result{}, err
		}
		var artifact codingoutbox.PublicationArtifact
		if command.Stage == codingoutbox.PublicationAuthoringFreeze {
			artifact, err = attempt.AcknowledgeAuthoringPublication(ctx, command.RequestSHA256, raw)
		} else {
			artifact, err = attempt.AcknowledgeTerminalPublication(ctx, command.RequestSHA256, raw)
			if err == nil {
				if record.TerminalPublication == nil {
					err = codingoutbox.ErrState
				} else {
					err = service.store.Release(
						ctx, attempt.ID(), record.TerminalPublication.Authority.EvidenceSHA256,
					)
				}
			}
		}
		if err != nil {
			return result{}, err
		}
		base.RecordID = attempt.ID()
		base.Artifact = &artifact
		return base, nil
	case "pending":
		var command pendingCommand
		if decodeRequired(body, &command, "schema", "limit") != nil || command.Schema != commandSchema {
			return result{}, ErrInvalid
		}
		// A remote validator uses pending only as a pre-start transport probe.
		// Its signed ticket identity must not grant a global journal listing.
		if command.AgentID != "" || command.AgentArtifactSHA256 != "" || command.TicketID != "" || command.CodingRunID != "" {
			if !validTicketID(command.AgentID) || !validSHA256(command.AgentArtifactSHA256) ||
				!validTicketID(command.TicketID) || !validIdentifier(command.CodingRunID, 256) {
				return result{}, ErrInvalid
			}
			base.Pending = []pendingResult{}
			return base, nil
		}
		pending, err := service.store.PendingPublications(ctx, command.Limit)
		if err != nil {
			return result{}, err
		}
		base.Pending = make([]pendingResult, len(pending))
		for index, item := range pending {
			base.Pending[index] = pendingResult{
				RecordID: item.RecordID, TicketID: item.Binding.TicketID, Stage: item.Stage,
				Authority: item.Authority, Request: item.Request,
			}
		}
		return base, nil
	case "open":
		var command openCommand
		if decodeRequired(body, &command, "schema", "record_id", "stage", "acknowledgement") != nil ||
			command.Schema != commandSchema || !validStage(command.Stage) {
			return result{}, ErrInvalid
		}
		if command.AgentID != "" || command.AgentArtifactSHA256 != "" || command.TicketID != "" || command.CodingRunID != "" {
			if !validTicketID(command.AgentID) || !validSHA256(command.AgentArtifactSHA256) ||
				!validTicketID(command.TicketID) || !validIdentifier(command.CodingRunID, 256) {
				return result{}, ErrInvalid
			}
			attempt, record, err := service.store.Lookup(ctx, codingoutbox.PurposeShadowAttempt, command.TicketID)
			if err != nil || attempt.ID() != command.RecordID {
				return result{}, errors.Join(codingoutbox.ErrState, err)
			}
			publication := record.AuthoringPublication
			if command.Stage == codingoutbox.PublicationTerminalResult {
				publication = record.TerminalPublication
			}
			if publication == nil || publication.Stage != command.Stage ||
				publication.Authority.AgentID != command.AgentID ||
				publication.Authority.CodingRunID != command.CodingRunID ||
				record.Binding.AgentArtifactSHA256 != command.AgentArtifactSHA256 {
				return result{}, codingoutbox.ErrState
			}
		}
		var reader io.ReadCloser
		var err error
		if command.Acknowledgement {
			reader, err = service.store.OpenPublicationAcknowledgement(ctx, command.RecordID, command.Stage)
		} else {
			reader, err = service.store.OpenPublication(ctx, command.RecordID, command.Stage)
		}
		if err != nil {
			return result{}, err
		}
		raw, readErr := io.ReadAll(io.LimitReader(reader, (4<<20)+1))
		closeErr := reader.Close()
		if readErr != nil || closeErr != nil || len(raw) == 0 || len(raw) > 4<<20 {
			return result{}, errors.Join(ErrInvalid, readErr, closeErr)
		}
		base.RecordID = command.RecordID
		base.BodyBase64 = base64.StdEncoding.EncodeToString(raw)
		return base, nil
	case "lookup":
		var command lookupCommand
		if decodeRequired(body, &command, "schema", "ticket_id", "stage") != nil ||
			command.Schema != commandSchema || !validTicketID(command.TicketID) || !validStage(command.Stage) {
			return result{}, ErrInvalid
		}
		attempt, record, err := service.store.Lookup(ctx, codingoutbox.PurposeShadowAttempt, command.TicketID)
		if err != nil {
			return result{}, err
		}
		publication := record.AuthoringPublication
		if command.Stage == codingoutbox.PublicationTerminalResult {
			publication = record.TerminalPublication
		}
		if publication == nil || publication.Stage != command.Stage {
			return result{}, codingoutbox.ErrState
		}
		base.RecordID = attempt.ID()
		base.Publication = &publicationResult{
			RecordID: attempt.ID(), TicketID: record.Binding.TicketID, Stage: publication.Stage,
			Authority: publication.Authority, Request: publication.Request,
			Acknowledgement: publication.Acknowledgement,
		}
		return base, nil
	default:
		return result{}, ErrInvalid
	}
}

func (service *Service) authorized(request *http.Request) bool {
	if service == nil {
		return false
	}
	values := request.Header.Values("Authorization")
	if len(values) != 1 || !strings.HasPrefix(values[0], "Bearer ") {
		return false
	}
	digest := sha256.Sum256([]byte(strings.TrimPrefix(values[0], "Bearer ")))
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.closed || subtle.ConstantTimeCompare(digest[:], service.token[:]) != 1 {
		return false
	}
	service.active++
	return true
}

func (service *Service) release() {
	service.mu.Lock()
	service.active--
	service.mu.Unlock()
}

func (service *Service) Close() error {
	if service == nil {
		return nil
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.active != 0 {
		return ErrClosed
	}
	service.closed = true
	service.token = [sha256.Size]byte{}
	service.store = nil
	return nil
}

func decodeRequired(body []byte, destination any, required ...string) error {
	var shape map[string]json.RawMessage
	if json.Unmarshal(body, &shape) != nil {
		return ErrInvalid
	}
	for _, field := range required {
		if _, ok := shape[field]; !ok {
			return ErrInvalid
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	if decoder.Decode(destination) != nil {
		return ErrInvalid
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return ErrInvalid
	}
	return nil
}

func decodeBody(value string, maximum int64) ([]byte, error) {
	if value == "" || int64(base64.StdEncoding.DecodedLen(len(value))) > maximum {
		return nil, ErrInvalid
	}
	body, err := base64.StdEncoding.Strict().DecodeString(value)
	if err != nil || len(body) == 0 || int64(len(body)) > maximum {
		return nil, ErrInvalid
	}
	return body, nil
}

func validTicketID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func validStage(stage codingoutbox.PublicationStage) bool {
	return stage == codingoutbox.PublicationAuthoringFreeze || stage == codingoutbox.PublicationTerminalResult
}

func validOperation(value string) bool {
	switch value {
	case "prepare", "acknowledge", "pending", "open", "lookup":
		return true
	default:
		return false
	}
}

func validControlToken(value string) bool {
	if len(value) < 32 || len(value) > 256 {
		return false
	}
	for _, character := range value {
		if (character < 'a' || character > 'z') && (character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') && character != '_' && character != '-' {
			return false
		}
	}
	return true
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

func statusForError(err error) int {
	switch {
	case errors.Is(err, codingoutbox.ErrInvalid), errors.Is(err, ErrInvalid):
		return http.StatusBadRequest
	case errors.Is(err, codingoutbox.ErrConflict), errors.Is(err, codingoutbox.ErrState):
		return http.StatusConflict
	case errors.Is(err, codingoutbox.ErrCapacity):
		return http.StatusInsufficientStorage
	case errors.Is(err, codingoutbox.ErrClosed), errors.Is(err, ErrClosed):
		return http.StatusServiceUnavailable
	default:
		return http.StatusBadGateway
	}
}

func codeForError(err error) string {
	switch statusForError(err) {
	case http.StatusBadRequest:
		return "invalid_command"
	case http.StatusConflict:
		return "publication_conflict"
	case http.StatusInsufficientStorage:
		return "publication_capacity"
	case http.StatusServiceUnavailable:
		return "service_closed"
	default:
		return "publication_failed"
	}
}

func writeError(response http.ResponseWriter, status int, code string) {
	body, _ := json.Marshal(map[string]any{
		"error": map[string]string{
			"code": code, "message": "coding publication request failed",
		},
	})
	body = append(body, '\n')
	response.Header().Set("Content-Length", strconv.Itoa(len(body)))
	response.WriteHeader(status)
	_, _ = response.Write(body)
}

func (service *Service) String() string       { return "CodingPublicationService{private}" }
func (service *Service) GoString() string     { return service.String() }
func (service *Service) LogValue() slog.Value { return slog.StringValue("coding-publication-service") }
func (*Service) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

var _ json.Marshaler = (*Service)(nil)
