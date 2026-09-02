package traces

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/klauspost/compress/zstd"
)

// UploaderOptions tunes shipping.
type UploaderOptions struct {
	Sinks        []Sink
	PollInterval time.Duration // how often ready/ is rescanned when idle (default 5s)
	RetryBackoff time.Duration // base delay before a failed file is retried (default 30s, capped x8)
	KeyPrefix    string        // object key root (default "traces/v1")
	Logger       *slog.Logger
	Metrics      *Metrics
	Now          func() time.Time
}

func (o *UploaderOptions) withDefaults() {
	if o.PollInterval <= 0 {
		o.PollInterval = 5 * time.Second
	}
	if o.RetryBackoff <= 0 {
		o.RetryBackoff = 30 * time.Second
	}
	if o.KeyPrefix == "" {
		o.KeyPrefix = "traces/v1"
	}
	o.KeyPrefix = strings.Trim(o.KeyPrefix, "/")
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.Metrics == nil {
		o.Metrics = NopMetrics()
	}
	if o.Now == nil {
		o.Now = time.Now
	}
}

// Uploader ships ready/ files to every sink and deletes them once every
// required sink holds them. One file is processed at a time, oldest first:
// the upload volume (low-GB/day) never needs parallelism, and ordering keeps
// the oldest data the first to become durable.
type Uploader struct {
	spool *Spooler
	opts  UploaderOptions
	mu    sync.Mutex
	retry map[string]retryState // ready file path -> backoff state
	done  chan struct{}
	wake  chan struct{}
}

type retryState struct {
	attempts int
	nextAt   time.Time
}

// sidecar records which sinks already hold a file, so a restart or a
// half-failed multi-sink upload never re-sends to a sink that succeeded.
type sidecar struct {
	Key       string            `json:"key"`
	SHA256    string            `json:"sha256"`
	Bytes     int64             `json:"bytes"`
	Completed map[string]string `json:"completed"` // sink name -> RFC3339 time
}

// NewUploader creates an uploader over spool. Start runs it.
func NewUploader(spool *Spooler, opts UploaderOptions) (*Uploader, error) {
	opts.withDefaults()
	if spool == nil {
		return nil, errors.New("traces: uploader needs a spooler")
	}
	if len(opts.Sinks) == 0 {
		return nil, errors.New("traces: uploader needs at least one sink")
	}
	seen := map[string]bool{}
	for _, s := range opts.Sinks {
		if seen[s.Name()] {
			return nil, fmt.Errorf("traces: duplicate sink name %q", s.Name())
		}
		seen[s.Name()] = true
	}
	return &Uploader{spool: spool, opts: opts, retry: map[string]retryState{}, done: make(chan struct{}), wake: make(chan struct{}, 1)}, nil
}

// Start runs the shipping loop until ctx is cancelled, then returns after a
// final pass bounded by the context the caller passes to Drain.
func (u *Uploader) Start(ctx context.Context) {
	go func() {
		defer close(u.done)
		ticker := time.NewTicker(u.opts.PollInterval)
		defer ticker.Stop()
		for {
			u.pass(ctx)
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			case <-u.wake:
			}
		}
	}()
}

// Drain waits for the loop to stop, then makes one last attempt to ship
// whatever is ready, bounded by ctx. Files that do not make it stay on disk
// for the next process.
func (u *Uploader) Drain(ctx context.Context) {
	select {
	case <-u.done:
	case <-ctx.Done():
		return
	}
	u.pass(ctx)
}

// Kick asks the loop to scan now (tests, and the spooler after rotation).
func (u *Uploader) Kick() {
	select {
	case u.wake <- struct{}{}:
	default:
	}
}

// pass ships every ready file that is not in backoff.
func (u *Uploader) pass(ctx context.Context) {
	for _, rf := range u.spool.readyFiles() {
		if ctx.Err() != nil {
			return
		}
		if !u.due(rf.path) {
			continue
		}
		if err := u.ship(ctx, rf); err != nil {
			u.backoff(rf.path)
			u.opts.Logger.Warn("trace upload incomplete; will retry",
				slog.String("file", filepath.Base(rf.path)), slog.String("error", err.Error()))
			continue
		}
		u.clearRetry(rf.path)
	}
}

func (u *Uploader) due(path string) bool {
	u.mu.Lock()
	defer u.mu.Unlock()
	st, ok := u.retry[path]
	return !ok || !u.opts.Now().Before(st.nextAt)
}

func (u *Uploader) backoff(path string) {
	u.mu.Lock()
	defer u.mu.Unlock()
	st := u.retry[path]
	st.attempts++
	shift := st.attempts - 1
	if shift > 3 {
		shift = 3
	}
	st.nextAt = u.opts.Now().Add(u.opts.RetryBackoff << shift)
	u.retry[path] = st
}

func (u *Uploader) clearRetry(path string) {
	u.mu.Lock()
	delete(u.retry, path)
	u.mu.Unlock()
}

// ship compresses (once), uploads to every sink that has not confirmed, and
// deletes the local files once every required sink has. Returns an error if
// any required sink is still missing.
func (u *Uploader) ship(ctx context.Context, rf *readyFile) error {
	zstPath := rf.path + ".zst"
	sidePath := rf.path + ".sinks.json"
	side, err := readSidecar(sidePath)
	if err != nil {
		return err
	}
	if side == nil || !fileExists(zstPath) {
		sum, size, err := compressFile(rf.path, zstPath)
		if err != nil {
			return fmt.Errorf("compress: %w", err)
		}
		u.spool.addBytes(size)
		side = &sidecar{Key: objectKey(u.opts.KeyPrefix, rf), SHA256: sum, Bytes: size, Completed: map[string]string{}}
		if err := writeSidecar(sidePath, side); err != nil {
			return err
		}
	}
	var missingRequired []string
	var firstErr error
	for _, sink := range u.opts.Sinks {
		if _, done := side.Completed[sink.Name()]; done {
			continue
		}
		err := sink.Put(ctx, side.Key, side.Bytes, "application/zstd", side.SHA256, fileOpener(zstPath))
		if err != nil {
			u.opts.Metrics.UploadFail(sink.Name())
			if firstErr == nil {
				firstErr = fmt.Errorf("%s: %w", sink.Name(), err)
			}
			if sink.Required() {
				missingRequired = append(missingRequired, sink.Name())
			}
			continue
		}
		side.Completed[sink.Name()] = u.opts.Now().UTC().Format(time.RFC3339)
		u.opts.Metrics.Uploaded(sink.Name(), side.Bytes)
		if err := writeSidecar(sidePath, side); err != nil {
			return err
		}
	}
	if len(missingRequired) > 0 {
		return fmt.Errorf("required sinks pending %v: %w", missingRequired, firstErr)
	}
	// Every required sink holds it. Optional sinks that failed are given up
	// on here — they are mirrors, and keeping the file would let one dead
	// mirror fill the disk.
	var freed int64
	for _, p := range []string{rf.path, zstPath, sidePath} {
		if info, err := os.Stat(p); err == nil {
			freed += info.Size()
		}
		_ = os.Remove(p)
	}
	u.spool.releaseBytes(freed)
	u.opts.Metrics.Released(1)
	if firstErr != nil {
		u.opts.Logger.Warn("trace file released with an optional sink missing", slog.String("file", filepath.Base(rf.path)), slog.String("error", firstErr.Error()))
	}
	return nil
}

// objectKey lays the object out for partition-pruning readers:
//
//	<prefix>/lane=<lane>/kind=<kind>/dt=<YYYY-MM-DD>/hour=<HH>/<instance>-<first>-<last>-<rand>.jsonl.zst
//
// dt/hour come from the FIRST record in the file, so a file never straddles
// the partition its name claims by more than one rotation interval.
func objectKey(prefix string, rf *readyFile) string {
	first := rf.firstAt.UTC()
	name := fmt.Sprintf("%s-%s-%s-%s.jsonl.zst", rf.instance,
		first.Format("20060102T150405Z"), rf.lastAt.UTC().Format("20060102T150405Z"), rf.suffix)
	return fmt.Sprintf("%s/lane=%s/kind=%s/dt=%s/hour=%s/%s", prefix, rf.lane, rf.kind,
		first.Format("2006-01-02"), first.Format("15"), name)
}

// compressFile writes src as zstd to dst (atomically via a temp name) and
// returns the sha256 and size of dst.
func compressFile(src, dst string) (string, int64, error) {
	in, err := os.Open(src)
	if err != nil {
		return "", 0, err
	}
	defer in.Close()
	tmp := dst + ".tmp"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o640)
	if err != nil {
		return "", 0, err
	}
	hasher := sha256.New()
	enc, err := zstd.NewWriter(io.MultiWriter(out, hasher), zstd.WithEncoderLevel(zstd.SpeedBetterCompression))
	if err != nil {
		_ = out.Close()
		return "", 0, err
	}
	if _, err := io.Copy(enc, in); err != nil {
		_ = enc.Close()
		_ = out.Close()
		_ = os.Remove(tmp)
		return "", 0, err
	}
	if err := enc.Close(); err != nil {
		_ = out.Close()
		_ = os.Remove(tmp)
		return "", 0, err
	}
	if err := out.Sync(); err != nil {
		_ = out.Close()
		return "", 0, err
	}
	if err := out.Close(); err != nil {
		return "", 0, err
	}
	info, err := os.Stat(tmp)
	if err != nil {
		return "", 0, err
	}
	if err := os.Rename(tmp, dst); err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(hasher.Sum(nil)), info.Size(), nil
}

func readSidecar(path string) (*sidecar, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var s sidecar
	if err := json.Unmarshal(b, &s); err != nil {
		// A torn sidecar is rebuilt from scratch: worst case a sink gets the
		// same object PUT twice, which is idempotent.
		return nil, nil
	}
	if s.Completed == nil {
		s.Completed = map[string]string{}
	}
	return &s, nil
}

func writeSidecar(path string, s *sidecar) error {
	b, err := json.Marshal(s)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o640); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
