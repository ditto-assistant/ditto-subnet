package traces

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Recorder is what the inference handlers call. A nil *Spooler is a valid,
// no-op Recorder so the hooks need no feature check.
type Recorder interface {
	Record(rec *Record)
}

// SpoolOptions tunes the on-disk spool.
type SpoolOptions struct {
	Dir            string        // REQUIRED: local directory; open/ and ready/ live under it
	RotateBytes    int64         // rotate a stream file once it reaches this size (default 64 MiB)
	RotateInterval time.Duration // rotate a stream file once its first record is this old (default 5m)
	MaxSpoolBytes  int64         // stop accepting records once open+ready exceed this (default 8 GiB)
	QueueSize      int           // in-memory queue depth before records are dropped (default 4096)
	FlushInterval  time.Duration // bufio flush + fsync cadence (default 2s)
	Instance       string        // relay identity for object keys and Record.Relay
	Commit         string
	Source         string // Record.Relay.Source default ("relay"; the backfill sets "postgres-backfill")
	// RotateOnDayChange also rotates when a record's partition day differs
	// from the open file's first record, so an export that walks history
	// lands each UTC day in its own objects.
	RotateOnDayChange bool
	Logger            *slog.Logger
	Metrics           *Metrics
	Now               func() time.Time
}

func (o *SpoolOptions) withDefaults() {
	if o.RotateBytes <= 0 {
		o.RotateBytes = 64 << 20
	}
	if o.RotateInterval <= 0 {
		o.RotateInterval = 5 * time.Minute
	}
	if o.MaxSpoolBytes <= 0 {
		o.MaxSpoolBytes = 8 << 30
	}
	if o.QueueSize <= 0 {
		o.QueueSize = 4096
	}
	if o.FlushInterval <= 0 {
		o.FlushInterval = 2 * time.Second
	}
	if o.Instance == "" {
		host, _ := os.Hostname()
		if host == "" {
			host = "relay"
		}
		o.Instance = host
	}
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

const (
	openDirName  = "open"
	readyDirName = "ready"
	spoolExt     = ".jsonl"
)

// Spooler is the Recorder: a bounded queue drained by one writer goroutine
// into per-stream JSONL files under Dir/open, rotated into Dir/ready for the
// Uploader. It never blocks its callers.
type Spooler struct {
	opts   SpoolOptions
	queue  chan *Record
	done   chan struct{}
	closed atomic.Bool
	mu     sync.Mutex
	files  map[string]*streamFile // stream key -> open file
	// diskBytes tracks open+ready bytes; refreshed by the writer on rotation
	// and by the uploader on deletion (see Uploader.releaseBytes).
	diskBytes atomic.Int64
	dropped   atomic.Int64
}

type streamFile struct {
	path     string
	f        *os.File
	w        *bufio.Writer
	bytes    int64
	firstAt  time.Time // earliest partition time in the file
	lastAt   time.Time // latest partition time in the file
	openedAt time.Time // wall clock; drives age rotation
	records  int64
	instance string
}

// NewSpooler prepares Dir, recovers leftovers from a previous process, and
// starts the writer. Call Close to flush and rotate on shutdown.
func NewSpooler(opts SpoolOptions) (*Spooler, error) {
	opts.withDefaults()
	if opts.Dir == "" {
		return nil, errors.New("traces: spool dir is required")
	}
	for _, sub := range []string{openDirName, readyDirName} {
		if err := os.MkdirAll(filepath.Join(opts.Dir, sub), 0o750); err != nil {
			return nil, fmt.Errorf("traces: create spool dir: %w", err)
		}
	}
	s := &Spooler{
		opts:  opts,
		queue: make(chan *Record, opts.QueueSize),
		done:  make(chan struct{}),
		files: map[string]*streamFile{},
	}
	if err := s.recoverOpenFiles(); err != nil {
		return nil, err
	}
	s.diskBytes.Store(s.measureDisk())
	opts.Metrics.SpoolBytes(s.diskBytes.Load())
	go s.run()
	return s, nil
}

// Record enqueues without blocking. Drops (queue full, spool over budget,
// closed) are counted and logged at a low rate; they are never surfaced to
// the inference caller.
func (s *Spooler) Record(rec *Record) {
	if s == nil || rec == nil {
		return
	}
	if s.closed.Load() {
		s.drop("closed")
		return
	}
	if s.diskBytes.Load() > s.opts.MaxSpoolBytes {
		s.drop("spool_full")
		return
	}
	if rec.Schema == "" {
		rec.Schema = SchemaVersion
	}
	if rec.RecordedAt.IsZero() {
		rec.RecordedAt = s.opts.Now().UTC()
	}
	if rec.Relay.Instance == "" {
		rec.Relay.Instance = s.opts.Instance
	}
	if rec.Relay.Commit == "" {
		rec.Relay.Commit = s.opts.Commit
	}
	if rec.Relay.Source == "" {
		if s.opts.Source != "" {
			rec.Relay.Source = s.opts.Source
		} else {
			rec.Relay.Source = "relay"
		}
	}
	select {
	case s.queue <- rec:
	default:
		s.drop("queue_full")
	}
}

func (s *Spooler) drop(reason string) {
	n := s.dropped.Add(1)
	s.opts.Metrics.Dropped(reason)
	if n == 1 || n%1000 == 0 {
		s.opts.Logger.Warn("trace record dropped", slog.String("reason", reason), slog.Int64("dropped_total", n))
	}
}

// Dropped returns the number of records dropped since start (tests/ops).
func (s *Spooler) Dropped() int64 { return s.dropped.Load() }

// WaitIdle blocks until the writer has drained the queue (every record
// enqueued so far is in a file) or ctx ends. Used by the backfill to keep
// its cursor behind the disk.
func (s *Spooler) WaitIdle(ctx context.Context) error {
	if s == nil {
		return nil
	}
	for {
		if len(s.queue) == 0 {
			// Give the writer a beat to finish the record it holds.
			time.Sleep(10 * time.Millisecond)
			if len(s.queue) == 0 {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(20 * time.Millisecond):
		}
	}
}

// Pending is the number of ready/ files not yet released by the uploader.
func (s *Spooler) Pending() int {
	if s == nil {
		return 0
	}
	return len(s.readyFiles())
}

// Close stops accepting records, drains the queue, flushes and rotates every
// open file into ready/ so the uploader (or the next process) ships them.
func (s *Spooler) Close(ctx context.Context) error {
	if s == nil {
		return nil
	}
	if s.closed.Swap(true) {
		return nil
	}
	close(s.queue)
	select {
	case <-s.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Spooler) run() {
	defer close(s.done)
	ticker := time.NewTicker(s.opts.FlushInterval)
	defer ticker.Stop()
	for {
		select {
		case rec, ok := <-s.queue:
			if !ok {
				s.rotateAll("close")
				return
			}
			s.write(rec)
		case <-ticker.C:
			s.flushAndRotateStale()
		}
	}
}

func (s *Spooler) write(rec *Record) {
	line, err := marshalLine(rec)
	if err != nil {
		s.drop("marshal")
		return
	}
	key := rec.StreamKey()
	at := rec.PartitionTime()
	s.mu.Lock()
	defer s.mu.Unlock()
	sf := s.files[key]
	if sf != nil && s.opts.RotateOnDayChange && sf.records > 0 {
		if y1, m1, d1 := sf.firstAt.Date(); true {
			if y2, m2, d2 := at.Date(); y1 != y2 || m1 != m2 || d1 != d2 {
				s.rotateLocked(key, sf, "day")
				sf = nil
			}
		}
	}
	if sf == nil {
		opened, err := s.openStream(key)
		if err != nil {
			s.opts.Logger.Error("trace spool open failed", slog.String("error", err.Error()))
			s.drop("open_failed")
			return
		}
		sf = opened
		s.files[key] = sf
	}
	if _, err := sf.w.Write(line); err != nil {
		s.opts.Logger.Error("trace spool write failed", slog.String("error", err.Error()))
		s.drop("write_failed")
		return
	}
	n := int64(len(line))
	sf.bytes += n
	sf.records++
	if sf.firstAt.IsZero() || at.Before(sf.firstAt) {
		sf.firstAt = at
	}
	if at.After(sf.lastAt) {
		sf.lastAt = at
	}
	s.diskBytes.Add(n)
	s.opts.Metrics.Recorded(rec.Event, rec.Request.Lane, rec.Request.Kind, n)
	if sf.bytes >= s.opts.RotateBytes {
		s.rotateLocked(key, sf, "size")
	}
}

func (s *Spooler) openStream(key string) (*streamFile, error) {
	now := s.opts.Now().UTC()
	name := fmt.Sprintf("%s-%s-%s%s", key, now.Format("20060102T150405Z"), randomSuffix(), spoolExt)
	path := filepath.Join(s.opts.Dir, openDirName, name)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o640)
	if err != nil {
		return nil, err
	}
	return &streamFile{path: path, f: f, w: bufio.NewWriterSize(f, 256<<10), instance: s.opts.Instance, openedAt: now}, nil
}

func (s *Spooler) flushAndRotateStale() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.opts.Now().UTC()
	for key, sf := range s.files {
		if sf.records == 0 {
			continue
		}
		if now.Sub(sf.openedAt) >= s.opts.RotateInterval {
			s.rotateLocked(key, sf, "age")
			continue
		}
		if err := sf.w.Flush(); err != nil {
			s.opts.Logger.Error("trace spool flush failed", slog.String("error", err.Error()))
			continue
		}
		_ = sf.f.Sync()
	}
}

func (s *Spooler) rotateAll(reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, sf := range s.files {
		s.rotateLocked(key, sf, reason)
	}
}

// rotateLocked flushes, fsyncs and moves the stream file into ready/ under a
// name that carries the stream key and the first/last record times (the
// uploader derives the object key from it, so the file is self-describing
// without parsing its contents).
func (s *Spooler) rotateLocked(key string, sf *streamFile, reason string) {
	delete(s.files, key)
	if err := sf.w.Flush(); err != nil {
		s.opts.Logger.Error("trace spool flush failed", slog.String("error", err.Error()))
	}
	_ = sf.f.Sync()
	_ = sf.f.Close()
	if sf.records == 0 {
		_ = os.Remove(sf.path)
		return
	}
	target := filepath.Join(s.opts.Dir, readyDirName, readyName(key, sf.instance, sf.firstAt, sf.lastAt))
	if err := os.Rename(sf.path, target); err != nil {
		s.opts.Logger.Error("trace spool rotate failed", slog.String("error", err.Error()))
		return
	}
	s.opts.Metrics.Rotated(reason, sf.records, sf.bytes)
}

// marshalLine encodes one record as a single JSON line with a trailing
// newline and WITHOUT HTML escaping: prompts and completions are stored as
// the bytes the model saw, not as \u003c-mangled text.
func marshalLine(rec *Record) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(rec); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// readyName is <stream>-<instance>-<firstTS>-<lastTS>-<rand>.jsonl.
func readyName(stream, instance string, first, last time.Time) string {
	return fmt.Sprintf("%s-%s-%s-%s-%s%s", stream, sanitizeInstance(instance),
		first.UTC().Format("20060102T150405Z"), last.UTC().Format("20060102T150405Z"), randomSuffix(), spoolExt)
}

// readyFile is a parsed ready/ file name.
type readyFile struct {
	path     string
	stream   string // lane-kind
	lane     string
	kind     string
	instance string
	firstAt  time.Time
	lastAt   time.Time
	suffix   string
}

func parseReadyName(path string) (*readyFile, bool) {
	base := strings.TrimSuffix(filepath.Base(path), spoolExt)
	parts := strings.Split(base, "-")
	// lane-kind-instance-first-last-rand : instance may itself contain no '-'
	// (sanitizeInstance guarantees it).
	if len(parts) != 6 {
		return nil, false
	}
	first, err1 := time.Parse("20060102T150405Z", parts[3])
	last, err2 := time.Parse("20060102T150405Z", parts[4])
	if err1 != nil || err2 != nil {
		return nil, false
	}
	return &readyFile{
		path: path, stream: parts[0] + "-" + parts[1], lane: parts[0], kind: parts[1],
		instance: parts[2], firstAt: first, lastAt: last, suffix: parts[5],
	}, true
}

// sanitizeInstance keeps the instance name safe for file and object names:
// [a-z0-9_.] only; everything else (including '-' and ':') becomes '_'.
func sanitizeInstance(s string) string {
	var b strings.Builder
	for _, c := range strings.ToLower(s) {
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '_', c == '.':
			b.WriteRune(c)
		default:
			b.WriteByte('_')
		}
	}
	if b.Len() == 0 {
		return "relay"
	}
	return b.String()
}

func randomSuffix() string {
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%012x", time.Now().UnixNano()&0xffffffffffff)
	}
	return hex.EncodeToString(b[:])
}

// recoverOpenFiles moves files a previous process left in open/ (a crash or
// SIGKILL before rotation) into ready/, truncating a torn trailing line.
// Their first/last times come from the records themselves.
func (s *Spooler) recoverOpenFiles() error {
	entries, err := os.ReadDir(filepath.Join(s.opts.Dir, openDirName))
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), spoolExt) {
			continue
		}
		path := filepath.Join(s.opts.Dir, openDirName, e.Name())
		stream, first, last, records, err := truncateToCompleteLines(path)
		if err != nil {
			s.opts.Logger.Error("trace spool recovery failed; leaving file in place",
				slog.String("path", path), slog.String("error", err.Error()))
			continue
		}
		if records == 0 {
			_ = os.Remove(path)
			continue
		}
		if stream == "" {
			stream = strings.SplitN(e.Name(), "-2", 2)[0] // best effort: "<lane>-<kind>"
		}
		target := filepath.Join(s.opts.Dir, readyDirName, readyName(stream, s.opts.Instance, first, last))
		if err := os.Rename(path, target); err != nil {
			return fmt.Errorf("traces: recover %s: %w", path, err)
		}
		s.opts.Logger.Info("trace spool recovered leftover file", slog.String("file", filepath.Base(target)), slog.Int64("records", records))
	}
	return nil
}

// truncateToCompleteLines drops a torn final line and returns the stream
// key, first/last recorded_at and record count of what remains.
func truncateToCompleteLines(path string) (stream string, first, last time.Time, records int64, err error) {
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return "", first, last, 0, err
	}
	defer f.Close()
	reader := bufio.NewReaderSize(f, 1<<20)
	var offset int64
	for {
		line, rerr := reader.ReadBytes('\n')
		if rerr != nil && !errors.Is(rerr, io.EOF) {
			return "", first, last, 0, rerr
		}
		if len(line) > 0 && line[len(line)-1] == '\n' {
			var probe struct {
				RecordedAt time.Time `json:"recorded_at"`
				Request    struct {
					Lane string `json:"lane"`
					Kind string `json:"kind"`
				} `json:"request"`
			}
			if json.Unmarshal(line, &probe) == nil {
				if stream == "" && probe.Request.Lane != "" {
					stream = probe.Request.Lane + "-" + probe.Request.Kind
				}
				if first.IsZero() || probe.RecordedAt.Before(first) {
					first = probe.RecordedAt
				}
				if probe.RecordedAt.After(last) {
					last = probe.RecordedAt
				}
				records++
				offset += int64(len(line))
				continue
			}
		}
		// Torn or unparseable tail: cut here.
		break
	}
	if err := f.Truncate(offset); err != nil {
		return "", first, last, 0, err
	}
	if first.IsZero() {
		first = time.Now().UTC()
	}
	if last.IsZero() {
		last = first
	}
	return stream, first.UTC(), last.UTC(), records, nil
}

// measureDisk sums open/ and ready/ (including .zst and sidecars).
func (s *Spooler) measureDisk() int64 {
	var total int64
	for _, sub := range []string{openDirName, readyDirName} {
		entries, err := os.ReadDir(filepath.Join(s.opts.Dir, sub))
		if err != nil {
			continue
		}
		for _, e := range entries {
			if info, err := e.Info(); err == nil && !e.IsDir() {
				total += info.Size()
			}
		}
	}
	return total
}

// releaseBytes is called by the uploader after deleting files.
func (s *Spooler) releaseBytes(n int64) {
	v := s.diskBytes.Add(-n)
	if v < 0 {
		s.diskBytes.Store(0)
		v = 0
	}
	s.opts.Metrics.SpoolBytes(v)
}

func (s *Spooler) addBytes(n int64) {
	s.opts.Metrics.SpoolBytes(s.diskBytes.Add(n))
}

// readyFiles lists ready/ *.jsonl oldest first.
func (s *Spooler) readyFiles() []*readyFile {
	entries, err := os.ReadDir(filepath.Join(s.opts.Dir, readyDirName))
	if err != nil {
		return nil
	}
	var out []*readyFile
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), spoolExt) {
			continue
		}
		if rf, ok := parseReadyName(filepath.Join(s.opts.Dir, readyDirName, e.Name())); ok {
			out = append(out, rf)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if !out[i].firstAt.Equal(out[j].firstAt) {
			return out[i].firstAt.Before(out[j].firstAt)
		}
		return out[i].path < out[j].path
	})
	return out
}
