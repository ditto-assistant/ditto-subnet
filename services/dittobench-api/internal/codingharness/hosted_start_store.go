package codingharness

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

// HostedStartCommandConfig is trusted Platform-only process configuration.
// PythonExecutable must be the approved installed Platform virtualenv interpreter.
// The environment is explicitly injected, never inherited or sent to a harness.
type HostedStartCommandConfig struct {
	PythonExecutable    string
	PostgresEnvironment []string
	WorkerID            string
}

type HostedStartCommand struct{ config HostedStartCommandConfig }

func NewHostedStartCommand(config HostedStartCommandConfig) (*HostedStartCommand, error) {
	if !filepath.IsAbs(config.PythonExecutable) || filepath.Clean(config.PythonExecutable) != config.PythonExecutable || !canonicalUUID(config.WorkerID) {
		return nil, ErrInvalidConfig
	}
	allowed := map[string]bool{"POSTGRES_HOST": true, "POSTGRES_PORT": true, "POSTGRES_USER": true, "POSTGRES_PASSWORD": true, "POSTGRES_DB": true, "POSTGRES_COMMAND_TIMEOUT": true, "POSTGRES_POOL_MIN_SIZE": true, "POSTGRES_POOL_MAX_SIZE": true}
	seen := map[string]bool{}
	for _, entry := range config.PostgresEnvironment {
		key, value, ok := strings.Cut(entry, "=")
		if !ok || !allowed[key] || seen[key] || value == "" || len(entry) > 16384 || strings.ContainsRune(entry, 0) {
			return nil, ErrInvalidConfig
		}
		seen[key] = true
	}
	for _, key := range []string{"POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} {
		if !seen[key] {
			return nil, ErrInvalidConfig
		}
	}
	config.PostgresEnvironment = append([]string(nil), config.PostgresEnvironment...)
	return &HostedStartCommand{config: config}, nil
}

func hostedStartProjection(binding HostedBinding, instance string) (map[string]any, error) {
	if !canonicalUUID(binding.EvaluationID) || !canonicalUUID(binding.AttemptID) || !canonicalUUID(binding.WorkerID) || !canonicalUUID(binding.AgentID) ||
		!lowerSHA256(binding.AssignmentSHA256) || !lowerSHA256(binding.AgentArtifactSHA256) || !lowerSHA256(binding.ScreenedImageSHA256) ||
		binding.ProfileCapabilityID != "hosted-"+binding.AttemptID || binding.Deadline.Nanosecond() != 0 || !binding.Deadline.After(time.Now()) ||
		binding.Deadline.After(time.Now().Add(time.Hour)) || !validIdentifier(instance, 256) ||
		binding.ImageURL != "" || !binding.ImageExpiresAt.IsZero() {
		return nil, ErrInvalid
	}
	return map[string]any{
		"schema": "dittobench-coding-hosted-start-v2", "evaluation_id": binding.EvaluationID, "attempt_id": binding.AttemptID, "worker_id": binding.WorkerID,
		"agent_id": binding.AgentID, "assignment_sha256": binding.AssignmentSHA256, "artifact_sha256": binding.AgentArtifactSHA256,
		"screened_image_sha256": binding.ScreenedImageSHA256, "screened_image_id": binding.ScreenedImageID, "screened_image_ref": binding.ScreenedImageRef,
		"screened_image_size_bytes": binding.ScreenedImageSize, "screening_policy_version": binding.ScreeningPolicyVersion,
		"profile_capability_id": binding.ProfileCapabilityID, "harness_instance_id": instance, "deadline_unix": binding.Deadline.Unix(),
	}, nil
}

func (store *HostedStartCommand) CommitStart(ctx context.Context, binding HostedBinding, instance string) (bool, error) {
	if store == nil || ctx == nil || ctx.Err() != nil || binding.WorkerID != store.config.WorkerID {
		return false, ErrInvalid
	}
	projection, err := hostedStartProjection(binding, instance)
	if err != nil {
		return false, err
	}
	body, err := json.Marshal(projection)
	if err != nil || len(body) > 16383 {
		return false, ErrInvalid
	}
	body = append(body, '\n')
	digest := fmt.Sprintf("%x", sha256.Sum256(body))
	call, cancel := context.WithDeadline(ctx, binding.Deadline)
	defer cancel()
	call, cancelTimeout := context.WithTimeout(call, 30*time.Second)
	defer cancelTimeout()
	command := exec.CommandContext(call, store.config.PythonExecutable, "-I", "-m", "ditto.coding_hosted_start_worker", "--worker-id", store.config.WorkerID)
	command.Env = append([]string(nil), store.config.PostgresEnvironment...)
	command.Stdin = bytes.NewReader(body)
	output := &boundedStartOutput{}
	command.Stdout = output
	command.Stderr = io.Discard
	command.WaitDelay = time.Second
	if err := command.Run(); err != nil || call.Err() != nil || !binding.Deadline.After(time.Now()) {
		return false, ErrLifecycle
	}
	return parseHostedStartResult(output.Bytes(), digest)
}

func parseHostedStartResult(body []byte, digest string) (bool, error) {
	if codingcontract.ValidateJSONDocument(body, 1024) != nil {
		return false, ErrLifecycle
	}
	var response struct {
		Schema        string `json:"schema"`
		RequestSHA256 string `json:"request_sha256"`
		NewlyStarted  *bool  `json:"newly_started"`
	}
	if json.Unmarshal(body, &response) != nil || response.Schema != "dittobench-coding-hosted-start-result-v2" || response.RequestSHA256 != digest || response.NewlyStarted == nil {
		return false, ErrLifecycle
	}
	return *response.NewlyStarted, nil
}

type boundedStartOutput struct{ buffer bytes.Buffer }

func (output *boundedStartOutput) Bytes() []byte { return output.buffer.Bytes() }

func (output *boundedStartOutput) Write(body []byte) (int, error) {
	if output.buffer.Len()+len(body) > 1024 {
		return 0, ErrLifecycle
	}
	return output.buffer.Write(body)
}

func (HostedStartCommandConfig) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }
func (HostedStartCommandConfig) String() string               { return "HostedStartCommandConfig{private}" }
func (v HostedStartCommandConfig) GoString() string           { return v.String() }
func (v HostedStartCommandConfig) LogValue() slog.Value       { return slog.StringValue(v.String()) }
func (*HostedStartCommand) MarshalJSON() ([]byte, error)      { return nil, ErrInvalid }
func (*HostedStartCommand) String() string                    { return "HostedStartCommand{private}" }
func (v *HostedStartCommand) GoString() string                { return v.String() }
func (v *HostedStartCommand) LogValue() slog.Value            { return slog.StringValue(v.String()) }

var _ HostedStartStore = (*HostedStartCommand)(nil)
