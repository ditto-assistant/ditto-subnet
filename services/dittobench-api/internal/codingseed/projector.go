package codingseed

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/google/uuid"
)

type memoryArtifact struct {
	Memories []codingcontract.VisibleMemory `json:"memories"`
}

func New(config Config) (*Projector, error) {
	if config.MaxBundleBytes <= 0 || config.MaxBundleBytes > maximumBundleBytes ||
		config.SeedTimeout < time.Second || config.SeedTimeout > 2*time.Minute ||
		config.SeedTimeout%time.Millisecond != 0 {
		return nil, errors.New("coding seed projector configuration is invalid")
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &Projector{maximum: config.MaxBundleBytes, timeout: config.SeedTimeout, now: config.Now}, nil
}

func (projector *Projector) Project(reader io.Reader, binding Binding) (Projection, error) {
	if projector == nil || nilLike(reader) {
		return Projection{}, errors.New("coding seed projection dependencies are incomplete")
	}
	now := projector.now().UTC()
	binding.Deadline = binding.Deadline.UTC()
	if err := validateBinding(binding, now); err != nil {
		return Projection{}, err
	}
	body, err := io.ReadAll(io.LimitReader(reader, projector.maximum+1))
	if err != nil || len(body) == 0 || int64(len(body)) > projector.maximum {
		return Projection{}, errors.New("coding memory artifact size is invalid")
	}
	digest := sha256.Sum256(body)
	if hex.EncodeToString(digest[:]) != binding.MemoryBundleSHA256 {
		return Projection{}, errors.New("coding memory artifact digest is invalid")
	}
	if err := codingcontract.ValidateJSONDocument(body, int(projector.maximum)); err != nil {
		return Projection{}, errors.New("coding memory artifact JSON is invalid")
	}
	if err := validateArtifactShape(body); err != nil {
		return Projection{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var artifact memoryArtifact
	if err := decoder.Decode(&artifact); err != nil || artifact.Memories == nil {
		return Projection{}, errors.New("coding memory artifact schema is invalid")
	}
	request := codingcontract.SeedRequest{
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              binding.TicketID, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID,
		MemoryBundleSHA256:  binding.MemoryBundleSHA256,
		Memories:            cloneMemories(artifact.Memories),
	}
	if err := request.Validate(); err != nil {
		return Projection{}, fmt.Errorf("coding memory artifact violates the seed contract: %w", err)
	}
	seedBytes, err := codingcontract.CanonicalJSON(request)
	if err != nil || len(seedBytes) > codingcontract.MaxCanonicalJSONBytes {
		return Projection{}, errors.New("projected coding seed exceeds the wire contract")
	}
	return Projection{request: request, deadline: binding.Deadline}, nil
}

func validateArtifactShape(body []byte) error {
	var root map[string]json.RawMessage
	if err := json.Unmarshal(body, &root); err != nil || len(root) != 1 {
		return errors.New("coding memory artifact root is invalid")
	}
	rawMemories, present := root["memories"]
	if !present || bytes.Equal(bytes.TrimSpace(rawMemories), []byte("null")) {
		return errors.New("coding memory artifact memories are missing")
	}
	var memories []json.RawMessage
	if err := json.Unmarshal(rawMemories, &memories); err != nil {
		return errors.New("coding memory artifact memories are invalid")
	}
	required := []string{
		"memory_id", "repository_capability_id", "fact_group_id", "scope", "type", "content",
		"valid_from_epoch", "valid_until_epoch", "supersedes", "confidence_micros",
	}
	for _, raw := range memories {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(raw, &fields); err != nil || len(fields) != len(required) {
			return errors.New("coding memory artifact record shape is invalid")
		}
		for _, field := range required {
			if _, ok := fields[field]; !ok {
				return errors.New("coding memory artifact record lacks a required field")
			}
		}
	}
	return nil
}

func validateBinding(binding Binding, now time.Time) error {
	ticket, ticketErr := uuid.Parse(binding.TicketID)
	if ticketErr != nil || ticket == uuid.Nil || ticket.String() != binding.TicketID ||
		!validIdentifier(binding.CaseID, 256) || !validIdentifier(binding.ProfileCapabilityID, 256) ||
		!lowerSHA256(binding.MemoryBundleSHA256) || binding.Deadline.IsZero() ||
		!binding.Deadline.After(now) || binding.Deadline.After(now.Add(2*time.Hour)) {
		return errors.New("coding seed projection binding is invalid")
	}
	return nil
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

func lowerSHA256(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func cloneMemories(values []codingcontract.VisibleMemory) []codingcontract.VisibleMemory {
	if values == nil {
		return nil
	}
	result := make([]codingcontract.VisibleMemory, len(values))
	for index, value := range values {
		result[index] = value
		result[index].RepositoryCapabilityID = cloneString(value.RepositoryCapabilityID)
		result[index].FactGroupID = cloneString(value.FactGroupID)
		result[index].ValidFromEpoch = cloneString(value.ValidFromEpoch)
		result[index].ValidUntilEpoch = cloneString(value.ValidUntilEpoch)
		result[index].Supersedes = cloneStrings(value.Supersedes)
	}
	return result
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneStrings(values []string) []string {
	if values == nil {
		return nil
	}
	result := make([]string, len(values))
	copy(result, values)
	return result
}
