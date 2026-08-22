package codingrelayjournal

import (
	"errors"
	"log/slog"
	"os"
	"strconv"
	"sync"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

const (
	stateSchema             = "dittobench-coding-relay-journal-state-v1"
	entrySchema             = "dittobench-coding-relay-journal-entry-v1"
	maximumStateBytes       = 64 << 10
	maximumEntryBytes       = 48 << 20
	maximumRootBytes  int64 = 1 << 40
	maximumEntries          = 4_096
)

var (
	ErrInvalid  = errors.New("coding relay journal input is invalid")
	ErrLocked   = errors.New("coding relay journal is already open")
	ErrCapacity = errors.New("coding relay journal capacity is exhausted")
	ErrConflict = errors.New("coding relay journal identity conflicts")
	ErrCorrupt  = errors.New("coding relay journal is corrupt")
	ErrState    = errors.New("coding relay journal state transition is invalid")
	ErrClosed   = errors.New("coding relay journal is closed")
)

// Config fixes the private root, locked inference policy, and physical bounds.
// Root must already exist as an euid-owned directory with mode 0700.
type Config struct {
	Root          string
	Policy        codingcontract.InferencePolicy
	MaxTotalBytes int64
	MaxEntries    int
}

// Store implements codingrelay.Journal for one immutable relay binding.
type Store struct {
	mu sync.Mutex

	config   Config
	root     string
	dirs     directorySet
	rootLeaf string
	rootDev  uint64
	rootIno  uint64

	state         *stateRecord
	entries       []entryRecord
	stateBytes    int64
	entryBytes    []int64
	physicalBytes int64
	syncPending   bool
	fsync         func(int) error
	closed        bool
	closeErr      error
}

type stateRecord struct {
	Schema         string               `json:"schema"`
	Generation     uint64               `json:"generation"`
	Binding        *codingrelay.Binding `json:"binding"`
	Revoked        bool                 `json:"revoked"`
	ChecksumSHA256 string               `json:"checksum_sha256"`
}

type entryRecord struct {
	Schema         string                   `json:"schema"`
	Generation     uint64                   `json:"generation"`
	Sequence       uint32                   `json:"sequence"`
	Entry          codingrelay.JournalEntry `json:"entry"`
	ChecksumSHA256 string                   `json:"checksum_sha256"`
}

type directorySet struct {
	parent  *os.File
	root    *os.File
	staging *os.File
	entries *os.File
}

var _ codingrelay.Journal = (*Store)(nil)

func (*Store) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding relay journal stores cannot be serialized")
}

func (store *Store) String() string {
	if store == nil {
		return "CodingRelayJournal{nil=true}"
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	return "CodingRelayJournal{entries=" + strconv.Itoa(len(store.entries)) +
		" closed=" + strconv.FormatBool(store.closed) + "}"
}

func (store *Store) GoString() string { return store.String() }

func (store *Store) LogValue() slog.Value {
	if store == nil {
		return slog.GroupValue(slog.Bool("nil", true))
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	return slog.GroupValue(
		slog.Int("entries", len(store.entries)),
		slog.Bool("closed", store.closed),
	)
}
