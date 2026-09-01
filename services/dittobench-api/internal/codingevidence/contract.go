// Package codingevidence validates ticket-scoped sealed-evidence upload wires.
package codingevidence

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/google/uuid"
)

const (
	capabilitySchema   = "dittobench-coding-sealed-evidence-upload-capability-v1"
	finalizationSchema = "dittobench-coding-sealed-evidence-finalized-v1"
	contractVersion    = 1
	maximumWireBytes   = 32 << 10
	maximumURLBytes    = 16 << 10
	maximumURLSeconds  = 300
)

var (
	sha256Pattern       = regexp.MustCompile(`^[0-9a-f]{64}$`)
	rfc3339Microseconds = regexp.MustCompile(
		`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$`,
	)
)

// Kind identifies an immutable sealed-evidence body.
type Kind string

const (
	KindAuthoringTranscript                 Kind = "authoring-transcript"
	KindFrozenSubmission                    Kind = "frozen-submission"
	KindAuthoringPublicationRequest         Kind = "authoring-publication-request"
	KindAuthoringPublicationAcknowledgement Kind = "authoring-publication-acknowledgement"
	KindTerminalPublicationRequest          Kind = "terminal-publication-request"
	KindTerminalPublicationAcknowledgement  Kind = "terminal-publication-acknowledgement"
)

var maximumSize = map[Kind]int64{
	KindAuthoringTranscript:                 512 << 20,
	KindFrozenSubmission:                    128 << 20,
	KindAuthoringPublicationRequest:         4 << 20,
	KindAuthoringPublicationAcknowledgement: 1 << 20,
	KindTerminalPublicationRequest:          4 << 20,
	KindTerminalPublicationAcknowledgement:  1 << 20,
}

// MaximumSize returns the hard byte bound for one known sealed-evidence kind.
func MaximumSize(kind Kind) (int64, bool) {
	value, ok := maximumSize[kind]
	return value, ok
}

// WireUploadCapability may cross the validator-to-executor boundary. Its URL
// is deliberately absent from String, GoString, and structured logs.
type WireUploadCapability struct {
	Schema                string    `json:"schema"`
	CodingContractVersion int       `json:"coding_contract_version"`
	WeightEligible        bool      `json:"weight_eligible"`
	TicketID              string    `json:"ticket_id"`
	ClaimGeneration       int       `json:"claim_generation"`
	TicketDeadline        time.Time `json:"ticket_deadline"`
	UploadID              string    `json:"upload_id"`
	EvidenceKind          Kind      `json:"evidence_kind"`
	SHA256                string    `json:"sha256"`
	SizeBytes             int64     `json:"size_bytes"`
	ContentType           string    `json:"content_type"`
	ChecksumSHA256B64     string    `json:"checksum_sha256_b64"`
	URL                   string    `json:"url"`
	ExpiresAt             time.Time `json:"expires_at"`
}

func (value WireUploadCapability) String() string {
	return fmt.Sprintf(
		"CodingEvidenceUploadCapability{ticket=%q claim_generation=%d upload=%q kind=%q size_bytes=%d expires_at=%q}",
		value.TicketID, value.ClaimGeneration, value.UploadID, value.EvidenceKind,
		value.SizeBytes, value.ExpiresAt.UTC().Format(time.RFC3339),
	)
}

func (value WireUploadCapability) GoString() string { return value.String() }

func (value WireUploadCapability) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("ticket", value.TicketID),
		slog.Int("claim_generation", value.ClaimGeneration),
		slog.String("upload", value.UploadID),
		slog.String("kind", string(value.EvidenceKind)),
		slog.Int64("size_bytes", value.SizeBytes),
		slog.Time("expires_at", value.ExpiresAt.UTC()),
	)
}

// UploadCapability is deliberately non-serializable so a bearer URL cannot
// accidentally enter logs or another wire document after validation.
type UploadCapability struct {
	TicketID          string
	ClaimGeneration   int
	TicketDeadline    time.Time
	UploadID          string
	EvidenceKind      Kind
	SHA256            string
	SizeBytes         int64
	ContentType       string
	ChecksumSHA256B64 string
	URL               string
	ExpiresAt         time.Time
}

func (UploadCapability) MarshalJSON() ([]byte, error) {
	return nil, errors.New("sealed evidence capability must not serialize")
}

// WireFinalization is the redacted Platform acknowledgement after it verifies
// the expected uploaded object. It contains no storage key, URL, or version.
type WireFinalization struct {
	Schema                string    `json:"schema"`
	CodingContractVersion int       `json:"coding_contract_version"`
	WeightEligible        bool      `json:"weight_eligible"`
	TicketID              string    `json:"ticket_id"`
	ClaimGeneration       int       `json:"claim_generation"`
	UploadID              string    `json:"upload_id"`
	EvidenceKind          Kind      `json:"evidence_kind"`
	SHA256                string    `json:"sha256"`
	SizeBytes             int64     `json:"size_bytes"`
	FinalizedAt           time.Time `json:"finalized_at"`
	Accepted              bool      `json:"accepted"`
	Idempotent            bool      `json:"idempotent"`
}

// DecodeWireUploadCapability rejects duplicate, missing, malformed, or
// incoherent known fields while ignoring unknown fields for rolling upgrades.
func DecodeWireUploadCapability(body []byte) (WireUploadCapability, error) {
	var zero WireUploadCapability
	if err := codingcontract.ValidateJSONDocument(body, maximumWireBytes); err != nil {
		return zero, err
	}
	var shape map[string]json.RawMessage
	if err := json.Unmarshal(body, &shape); err != nil {
		return zero, err
	}
	for _, field := range capabilityFields {
		if _, present := shape[field]; !present {
			return zero, fmt.Errorf("sealed evidence capability is missing field %q", field)
		}
	}
	if err := validateTimestamp(shape["ticket_deadline"]); err != nil {
		return zero, err
	}
	if err := validateTimestamp(shape["expires_at"]); err != nil {
		return zero, err
	}
	var decoded WireUploadCapability
	if err := json.Unmarshal(body, &decoded); err != nil {
		return zero, err
	}
	if _, err := decoded.ToCapability(); err != nil {
		return zero, err
	}
	return decoded, nil
}

// ToCapability validates known fields and converts a serializable wire object
// into a non-serializable bearer capability.
func (value WireUploadCapability) ToCapability() (UploadCapability, error) {
	capability := UploadCapability{
		TicketID: value.TicketID, ClaimGeneration: value.ClaimGeneration,
		TicketDeadline: value.TicketDeadline, UploadID: value.UploadID,
		EvidenceKind: value.EvidenceKind, SHA256: value.SHA256,
		SizeBytes: value.SizeBytes, ContentType: value.ContentType,
		ChecksumSHA256B64: value.ChecksumSHA256B64, URL: value.URL,
		ExpiresAt: value.ExpiresAt,
	}
	if value.Schema != capabilitySchema || value.CodingContractVersion != contractVersion || value.WeightEligible {
		return UploadCapability{}, errors.New("sealed evidence capability authority is invalid")
	}
	if err := validateCapability(capability); err != nil {
		return UploadCapability{}, err
	}
	return capability, nil
}

// DecodeWireFinalization validates a Platform finalization acknowledgement.
func DecodeWireFinalization(body []byte) (WireFinalization, error) {
	var zero WireFinalization
	if err := codingcontract.ValidateJSONDocument(body, maximumWireBytes); err != nil {
		return zero, err
	}
	var shape map[string]json.RawMessage
	if err := json.Unmarshal(body, &shape); err != nil {
		return zero, err
	}
	for _, field := range finalizationFields {
		if _, present := shape[field]; !present {
			return zero, fmt.Errorf("sealed evidence finalization is missing field %q", field)
		}
	}
	if err := validateTimestamp(shape["finalized_at"]); err != nil {
		return zero, err
	}
	var decoded WireFinalization
	if err := json.Unmarshal(body, &decoded); err != nil {
		return zero, err
	}
	if decoded.Schema != finalizationSchema || decoded.CodingContractVersion != contractVersion ||
		decoded.WeightEligible || !decoded.Accepted || !validUUID(decoded.TicketID) ||
		!validUUID(decoded.UploadID) || decoded.ClaimGeneration < 1 || !validSHA256(decoded.SHA256) ||
		decoded.SizeBytes < 1 || decoded.SizeBytes > maximumSize[decoded.EvidenceKind] ||
		decoded.FinalizedAt.IsZero() {
		return zero, errors.New("sealed evidence finalization authority is invalid")
	}
	return decoded, nil
}

var capabilityFields = []string{
	"schema", "coding_contract_version", "weight_eligible", "ticket_id", "claim_generation",
	"ticket_deadline", "upload_id", "evidence_kind", "sha256", "size_bytes", "content_type",
	"checksum_sha256_b64", "url", "expires_at",
}

var finalizationFields = []string{
	"schema", "coding_contract_version", "weight_eligible", "ticket_id", "claim_generation",
	"upload_id", "evidence_kind", "sha256", "size_bytes", "finalized_at", "accepted", "idempotent",
}

func validateCapability(value UploadCapability) error {
	if !validUUID(value.TicketID) || !validUUID(value.UploadID) || value.ClaimGeneration < 1 ||
		!validSHA256(value.SHA256) || value.SizeBytes < 1 || value.SizeBytes > maximumSize[value.EvidenceKind] ||
		value.ContentType != "application/octet-stream" || value.ExpiresAt.IsZero() ||
		value.TicketDeadline.IsZero() || value.ExpiresAt.Nanosecond() != 0 || value.ExpiresAt.After(value.TicketDeadline) {
		return errors.New("sealed evidence capability fields are invalid")
	}
	checksum, err := base64.StdEncoding.DecodeString(value.ChecksumSHA256B64)
	if err != nil || len(checksum) != 32 || fmt.Sprintf("%x", checksum) != value.SHA256 {
		return errors.New("sealed evidence checksum disagrees with SHA-256")
	}
	return validateCapabilityURL(value)
}

func validateCapabilityURL(value UploadCapability) error {
	if len(value.URL) == 0 || len(value.URL) > maximumURLBytes || !isASCII(value.URL) {
		return errors.New("sealed evidence URL is invalid")
	}
	parsed, err := url.Parse(value.URL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" || parsed.User != nil ||
		parsed.Fragment != "" || parsed.RawQuery == "" || strings.Contains(parsed.RawQuery, ";") ||
		strings.Contains(parsed.Path, "%") || strings.Contains(parsed.Path, "//") ||
		path.Clean(parsed.Path) != parsed.Path {
		return errors.New("sealed evidence URL is invalid")
	}
	if rawPort := parsed.Port(); rawPort != "" {
		port, err := decimal(rawPort)
		if err != nil || port < 1 || port > 65535 {
			return errors.New("sealed evidence URL port is invalid")
		}
	}
	if parsed.Scheme != "https" && !(parsed.Scheme == "http" && loopback(parsed.Hostname())) {
		return errors.New("sealed evidence URL requires HTTPS outside loopback")
	}
	expected := "/coding-evidence/v1/" + string(value.EvidenceKind) + "/sha256/" + value.SHA256
	if !strings.HasSuffix(parsed.Path, expected) {
		return errors.New("sealed evidence URL path disagrees with known fields")
	}
	query, err := url.ParseQuery(parsed.RawQuery)
	if err != nil || queryCount(query) > 64 {
		return errors.New("sealed evidence URL query is invalid")
	}
	expires, err := signedExpiry(query)
	if err != nil || !expires.Equal(value.ExpiresAt) {
		return errors.New("sealed evidence signed expiry disagrees with known fields")
	}
	return nil
}

func signedExpiry(query url.Values) (time.Time, error) {
	v4, v2 := query["X-Amz-Signature"], query["Signature"]
	if len(v4) == 0 {
		v4, v2 = query["x-amz-signature"], query["signature"]
	}
	if (len(v4) == 0) == (len(v2) == 0) {
		return time.Time{}, errors.New("sealed evidence signature fields are ambiguous")
	}
	if len(v4) != 0 {
		dates, durations := query["X-Amz-Date"], query["X-Amz-Expires"]
		if len(dates) == 0 {
			dates, durations = query["x-amz-date"], query["x-amz-expires"]
		}
		if len(v4) != 1 || len(dates) != 1 || len(durations) != 1 {
			return time.Time{}, errors.New("sealed evidence v4 signature fields are invalid")
		}
		signedAt, err := time.Parse("20060102T150405Z", dates[0])
		if err != nil {
			return time.Time{}, err
		}
		duration, err := decimal(durations[0])
		if err != nil || duration < 60 || duration > maximumURLSeconds {
			return time.Time{}, errors.New("sealed evidence v4 expiry is outside bounds")
		}
		return signedAt.UTC().Add(time.Duration(duration) * time.Second), nil
	}
	expires := query["Expires"]
	if len(expires) == 0 {
		expires = query["expires"]
	}
	if len(v2) != 1 || len(expires) != 1 {
		return time.Time{}, errors.New("sealed evidence v2 signature fields are invalid")
	}
	value, err := decimal(expires[0])
	if err != nil {
		return time.Time{}, err
	}
	return time.Unix(int64(value), 0).UTC(), nil
}

func validateTimestamp(raw json.RawMessage) error {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil || !rfc3339Microseconds.MatchString(value) {
		return errors.New("sealed evidence timestamp must be RFC3339 with at most microsecond precision")
	}
	return nil
}

func validUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil
}

func validSHA256(value string) bool { return sha256Pattern.MatchString(value) }

func loopback(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}

func isASCII(value string) bool {
	for _, character := range value {
		if character < 32 || character > 126 {
			return false
		}
	}
	return true
}

func queryCount(query url.Values) int {
	count := 0
	for _, values := range query {
		count += len(values)
	}
	return count
}

func decimal(value string) (int, error) {
	if value == "" {
		return 0, errors.New("empty decimal")
	}
	for _, character := range value {
		if character < '0' || character > '9' {
			return 0, errors.New("decimal is invalid")
		}
	}
	return strconv.Atoi(value)
}
