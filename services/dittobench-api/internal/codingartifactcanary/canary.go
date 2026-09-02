// Package codingartifactcanary performs one ticket-bound S3 transport proof
// without constructing a coding host or executing candidate code.
package codingartifactcanary

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
)

const maximumCapabilityBytes = 32 << 10

var (
	ErrInvalid = errors.New("coding artifact canary configuration is invalid")
	ErrRun     = errors.New("coding artifact canary failed")
)

type ArtifactSource interface {
	Open(context.Context, codingartifacts.Capability) (io.ReadCloser, error)
}

type Config struct {
	Artifacts      ArtifactSource
	CapabilityPath string
	ReceiptPath    string
	Now            func() time.Time
}

type Canary struct {
	artifacts      ArtifactSource
	capabilityPath string
	receiptPath    string
	now            func() time.Time
}

type receipt struct {
	Schema                string `json:"schema"`
	CodingContractVersion int    `json:"coding_contract_version"`
	WeightEligible        bool   `json:"weight_eligible"`
	Authority             string `json:"authority"`
	Status                string `json:"status"`
	TicketIDSHA256        string `json:"ticket_id_sha256"`
	ArtifactKind          string `json:"artifact_kind"`
	ArtifactSHA256        string `json:"artifact_sha256"`
	ArtifactSizeBytes     int64  `json:"artifact_size_bytes"`
	CapabilityExpiresAt   string `json:"capability_expires_at"`
	TicketAuthorityUsed   bool   `json:"ticket_authority_used"`
	PlatformContacted     bool   `json:"platform_contacted"`
	S3Accessed            bool   `json:"s3_accessed"`
	CandidateExecuted     bool   `json:"candidate_executed"`
	StartedAt             string `json:"started_at"`
	CompletedAt           string `json:"completed_at"`
}

func New(config Config) (*Canary, error) {
	if nilLike(config.Artifacts) || !fixedFile(config.CapabilityPath, 0o400, true) ||
		!fixedReceipt(config.ReceiptPath) {
		return nil, ErrInvalid
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &Canary{
		artifacts: config.Artifacts, capabilityPath: config.CapabilityPath,
		receiptPath: config.ReceiptPath, now: config.Now,
	}, nil
}

func (canary *Canary) Run(ctx context.Context) error {
	if canary == nil || ctx == nil || ctx.Err() != nil {
		return ErrRun
	}
	started := canary.now().UTC()
	if !fixedFile(canary.capabilityPath, 0o400, true) {
		return ErrRun
	}
	body, err := os.ReadFile(canary.capabilityPath)
	if err != nil || len(body) == 0 || len(body) > maximumCapabilityBytes {
		return ErrRun
	}
	wire, err := codingartifacts.DecodeWireCapability(body)
	if err != nil || wire.DeliveryPhase != codingartifacts.PhaseAuthoring ||
		wire.ArtifactKind != codingartifacts.KindVisibleBundle ||
		wire.Audience != codingartifacts.AudienceWorkspaceMaterializer {
		return ErrRun
	}
	capability, err := wire.ToCapability()
	if err != nil {
		return ErrRun
	}
	if started.IsZero() || !started.Before(capability.ExpiresAt) ||
		!started.Before(capability.TicketDeadline) {
		return ErrRun
	}
	reader, err := canary.artifacts.Open(ctx, capability)
	invalidReader := nilLike(reader)
	if err != nil || invalidReader {
		if !invalidReader {
			_ = reader.Close()
		}
		return errors.Join(ErrRun, err)
	}
	written, copyErr := io.Copy(io.Discard, io.LimitReader(reader, capability.SizeBytes+1))
	closeErr := reader.Close()
	if copyErr != nil || closeErr != nil || written != capability.SizeBytes || ctx.Err() != nil {
		return errors.Join(ErrRun, copyErr, closeErr, ctx.Err())
	}
	completed := canary.now().UTC()
	if started.IsZero() || completed.Before(started) || !completed.Before(capability.ExpiresAt) ||
		!completed.Before(capability.TicketDeadline) {
		return ErrRun
	}
	ticketDigest := sha256.Sum256([]byte(capability.TicketID))
	value := receipt{
		Schema:                "dittobench-coding-artifact-connectivity-receipt-v1",
		CodingContractVersion: 1, WeightEligible: false,
		Authority: "operator-local-diagnostic", Status: "passed",
		TicketIDSHA256: hex.EncodeToString(ticketDigest[:]),
		ArtifactKind:   string(capability.Kind), ArtifactSHA256: capability.SHA256,
		ArtifactSizeBytes:   capability.SizeBytes,
		CapabilityExpiresAt: capability.ExpiresAt.UTC().Format(time.RFC3339Nano),
		TicketAuthorityUsed: true, PlatformContacted: false, S3Accessed: true, CandidateExecuted: false,
		StartedAt: started.Format(time.RFC3339Nano), CompletedAt: completed.Format(time.RFC3339Nano),
	}
	if err := writeReceipt(canary.receiptPath, value); err != nil {
		return ErrRun
	}
	return nil
}

func writeReceipt(path string, value receipt) error {
	parent := filepath.Dir(path)
	if !filepath.IsAbs(path) || filepath.Base(path) == "." || filepath.Base(path) == string(filepath.Separator) ||
		!safeDirectory(parent) {
		return ErrInvalid
	}
	if info, err := os.Lstat(path); err == nil {
		owner, ownerOK := fileOwner(info)
		if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o600 ||
			!ownerOK || owner != uint32(os.Geteuid()) {
			return ErrInvalid
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return ErrInvalid
	}
	body, err := json.Marshal(value)
	if err != nil {
		return ErrInvalid
	}
	body = append(body, '\n')
	handle, err := os.CreateTemp(parent, ".coding-artifact-canary-receipt-*")
	if err != nil {
		return ErrInvalid
	}
	temporary := handle.Name()
	cleanup := true
	defer func() {
		_ = handle.Close()
		if cleanup {
			_ = os.Remove(temporary)
		}
	}()
	if err := handle.Chmod(0o600); err != nil {
		return ErrInvalid
	}
	if _, err := handle.Write(body); err != nil {
		return ErrInvalid
	}
	if err := handle.Sync(); err != nil {
		return ErrInvalid
	}
	if err := handle.Close(); err != nil {
		return ErrInvalid
	}
	if err := os.Rename(temporary, path); err != nil {
		return ErrInvalid
	}
	cleanup = false
	directory, err := os.Open(parent)
	if err != nil {
		return ErrInvalid
	}
	syncErr := directory.Sync()
	closeErr := directory.Close()
	return errors.Join(syncErr, closeErr)
}

func fixedFile(path string, mode os.FileMode, bounded bool) bool {
	if !filepath.IsAbs(path) {
		return false
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 ||
		info.Mode().Perm() != mode || (bounded && (info.Size() <= 0 || info.Size() > maximumCapabilityBytes)) {
		return false
	}
	owner, ok := fileOwner(info)
	return ok && owner == uint32(os.Geteuid())
}

func fixedReceipt(path string) bool {
	return filepath.IsAbs(path) && safeDirectory(filepath.Dir(path))
}

func safeDirectory(path string) bool {
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o700 {
		return false
	}
	owner, ok := fileOwner(info)
	return ok && owner == uint32(os.Geteuid())
}

func (config Config) String() string        { return "CodingArtifactCanaryConfig{private}" }
func (config Config) GoString() string      { return config.String() }
func (Config) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }
func (config Config) LogValue() slog.Value  { return slog.StringValue(config.String()) }
func (canary *Canary) String() string {
	if canary == nil {
		return "CodingArtifactCanary{nil=true}"
	}
	return "CodingArtifactCanary{private=true}"
}
func (canary *Canary) GoString() string      { return canary.String() }
func (canary *Canary) LogValue() slog.Value  { return slog.StringValue(canary.String()) }
func (*Canary) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

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

func (value receipt) String() string {
	return "CodingArtifactCanaryReceipt{status=" + strconv.Quote(value.Status) + "}"
}

func (value receipt) GoString() string { return value.String() }
