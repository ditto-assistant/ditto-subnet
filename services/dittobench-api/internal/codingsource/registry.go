// Package codingsource binds private coding capability routes to one active
// harness container source. It owns no listener, Docker runtime, task bytes,
// provider credential, score, or weight path.
package codingsource

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/netip"
	"reflect"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"
)

var (
	ErrInvalid  = errors.New("coding source binding is invalid")
	ErrConflict = errors.New("coding source binding conflicts")
	ErrClosed   = errors.New("coding source binding is closed")
)

// HarnessBinding is validator-owned identity for one active container. The
// source address is deliberately separate because it is observed from Docker,
// never accepted from the harness or a Platform response.
type HarnessBinding struct {
	HarnessInstanceID   string
	AgentArtifactSHA256 string
	TicketID            string
	CaseID              string
	ProfileCapabilityID string
	Deadline            time.Time
}

type sourceRecord struct {
	binding HarnessBinding
	address netip.Addr
}

// Registry owns the one-to-one active instance/source mapping shared by the
// dormant harness controller and capability router.
type Registry struct {
	mu sync.Mutex

	now        func() time.Time
	lastNow    time.Time
	byInstance map[string]*sourceRecord
	byAddress  map[netip.Addr]*sourceRecord
}

// Lease is the exact registration owner. Close cannot remove a replacement
// record that reused the same textual identity after this lease ended.
type Lease struct {
	mu sync.Mutex

	registry *Registry
	record   *sourceRecord
	closed   bool
}

func NewRegistry(now func() time.Time) *Registry {
	if now == nil {
		now = time.Now
	}
	return &Registry{
		now: now, lastNow: now().UTC(),
		byInstance: make(map[string]*sourceRecord), byAddress: make(map[netip.Addr]*sourceRecord),
	}
}

func (registry *Registry) Register(binding HarnessBinding, sourceIP string) (*Lease, error) {
	if registry == nil {
		return nil, ErrInvalid
	}
	binding.Deadline = binding.Deadline.UTC()
	address, err := netip.ParseAddr(strings.TrimSpace(sourceIP))
	if err != nil || !validSourceAddress(address) {
		return nil, ErrInvalid
	}
	record := &sourceRecord{binding: binding, address: address.Unmap()}
	registry.mu.Lock()
	defer registry.mu.Unlock()
	now := registry.now().UTC()
	if !validBinding(binding, now) {
		return nil, ErrInvalid
	}
	if now.Before(registry.lastNow) {
		return nil, ErrClosed
	}
	registry.lastNow = now
	if registry.byInstance == nil || registry.byAddress == nil {
		return nil, ErrClosed
	}
	if len(registry.byInstance) >= 256 {
		return nil, ErrConflict
	}
	if registry.byInstance[binding.HarnessInstanceID] != nil || registry.byAddress[record.address] != nil {
		return nil, ErrConflict
	}
	registry.byInstance[binding.HarnessInstanceID] = record
	registry.byAddress[record.address] = record
	return &Lease{registry: registry, record: record}, nil
}

func (lease *Lease) Close() error {
	if lease == nil {
		return nil
	}
	lease.mu.Lock()
	defer lease.mu.Unlock()
	if lease.closed {
		return nil
	}
	if lease.registry == nil || lease.record == nil {
		return ErrInvalid
	}
	lease.registry.mu.Lock()
	defer lease.registry.mu.Unlock()
	currentInstance := lease.registry.byInstance[lease.record.binding.HarnessInstanceID]
	currentAddress := lease.registry.byAddress[lease.record.address]
	if currentInstance != lease.record || currentAddress != lease.record {
		return ErrConflict
	}
	delete(lease.registry.byInstance, lease.record.binding.HarnessInstanceID)
	delete(lease.registry.byAddress, lease.record.address)
	lease.closed = true
	return nil
}

func (registry *Registry) resolve(
	instanceID string,
	agentArtifactSHA256 string,
	ticketID string,
	caseID string,
	profileCapabilityID string,
	deadline *time.Time,
) (*sourceRecord, bool) {
	if registry == nil {
		return nil, false
	}
	registry.mu.Lock()
	defer registry.mu.Unlock()
	now := registry.now().UTC()
	if now.Before(registry.lastNow) {
		return nil, false
	}
	registry.lastNow = now
	record := registry.byInstance[instanceID]
	if record == nil || !record.binding.Deadline.After(now) ||
		record.binding.AgentArtifactSHA256 != agentArtifactSHA256 || record.binding.TicketID != ticketID ||
		record.binding.CaseID != caseID || record.binding.ProfileCapabilityID != profileCapabilityID {
		return nil, false
	}
	if deadline != nil && !record.binding.Deadline.Equal(deadline.UTC()) {
		return nil, false
	}
	return record, true
}

func (registry *Registry) matches(record *sourceRecord, remote netip.Addr) bool {
	if registry == nil || record == nil || !remote.IsValid() {
		return false
	}
	registry.mu.Lock()
	defer registry.mu.Unlock()
	now := registry.now().UTC()
	if now.Before(registry.lastNow) {
		return false
	}
	registry.lastNow = now
	return registry.byInstance[record.binding.HarnessInstanceID] == record &&
		registry.byAddress[record.address] == record && record.address == remote.Unmap() &&
		record.binding.Deadline.After(now)
}

func validBinding(binding HarnessBinding, now time.Time) bool {
	return validIdentifier(binding.HarnessInstanceID, 256) && lowerSHA256(binding.AgentArtifactSHA256) &&
		canonicalUUID(binding.TicketID) && validIdentifier(binding.CaseID, 256) &&
		validIdentifier(binding.ProfileCapabilityID, 256) && !now.IsZero() && binding.Deadline.After(now) &&
		!binding.Deadline.After(now.Add(2*time.Hour))
}

func validSourceAddress(address netip.Addr) bool {
	address = address.Unmap()
	return address.IsValid() && address.IsPrivate() && !address.IsLoopback() &&
		!address.IsUnspecified() && !address.IsMulticast() && !address.IsLinkLocalUnicast()
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func lowerSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
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

func (HarnessBinding) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }
func (*Registry) MarshalJSON() ([]byte, error)      { return nil, ErrInvalid }
func (*Lease) MarshalJSON() ([]byte, error)         { return nil, ErrInvalid }

func (binding HarnessBinding) String() string   { return "CodingSourceHarnessBinding{private}" }
func (binding HarnessBinding) GoString() string { return binding.String() }
func (binding HarnessBinding) LogValue() slog.Value {
	return slog.StringValue("coding-source-harness-binding")
}

func (registry *Registry) String() string {
	if registry == nil {
		return "CodingSourceRegistry{nil=true}"
	}
	registry.mu.Lock()
	defer registry.mu.Unlock()
	return "CodingSourceRegistry{active=" + slog.IntValue(len(registry.byInstance)).String() + "}"
}

func (registry *Registry) GoString() string { return registry.String() }
func (registry *Registry) LogValue() slog.Value {
	if registry == nil {
		return slog.GroupValue(slog.Bool("nil", true))
	}
	registry.mu.Lock()
	defer registry.mu.Unlock()
	return slog.GroupValue(slog.Int("active", len(registry.byInstance)))
}

func (lease *Lease) String() string       { return "CodingSourceLease{private}" }
func (lease *Lease) GoString() string     { return lease.String() }
func (lease *Lease) LogValue() slog.Value { return slog.StringValue("coding-source-lease") }

var _ json.Marshaler = HarnessBinding{}
var _ json.Marshaler = (*Registry)(nil)
var _ json.Marshaler = (*Lease)(nil)
