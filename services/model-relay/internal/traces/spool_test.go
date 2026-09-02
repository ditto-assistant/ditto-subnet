package traces

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
)

// fakeS3 is a minimal presigned-URL S3: HEAD/PUT bucket, PUT object. It
// records objects and can be told to fail.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string][]byte
	fail    bool
	puts    int
	buckets map[string]bool
	server  *httptest.Server
}

func newFakeS3(t *testing.T) *fakeS3 {
	t.Helper()
	f := &fakeS3{objects: map[string][]byte{}, buckets: map[string]bool{}}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if q.Get("X-Amz-Signature") == "" || q.Get("X-Amz-Credential") == "" || q.Get("X-Amz-SignedHeaders") != "host" {
			http.Error(w, "not presigned", http.StatusForbidden)
			return
		}
		parts := strings.SplitN(strings.TrimPrefix(r.URL.Path, "/"), "/", 2)
		bucket := parts[0]
		f.mu.Lock()
		defer f.mu.Unlock()
		if len(parts) == 1 || parts[1] == "" {
			switch r.Method {
			case http.MethodHead:
				if f.buckets[bucket] {
					w.WriteHeader(http.StatusOK)
				} else {
					w.WriteHeader(http.StatusNotFound)
				}
			case http.MethodPut:
				f.buckets[bucket] = true
				w.WriteHeader(http.StatusOK)
			default:
				w.WriteHeader(http.StatusMethodNotAllowed)
			}
			return
		}
		if r.Method == http.MethodGet {
			if f.fail {
				http.Error(w, "<Error>ServiceUnavailable</Error>", http.StatusServiceUnavailable)
				return
			}
			body, ok := f.objects[bucket+"/"+parts[1]]
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_, _ = w.Write(body)
			return
		}
		if r.Method != http.MethodPut {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		f.puts++
		if f.fail {
			http.Error(w, "<Error>ServiceUnavailable</Error>", http.StatusServiceUnavailable)
			return
		}
		body, _ := io.ReadAll(r.Body)
		if int64(len(body)) != r.ContentLength {
			http.Error(w, "length mismatch", http.StatusBadRequest)
			return
		}
		f.objects[bucket+"/"+parts[1]] = body
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeS3) sink(t *testing.T, name string, required bool) *S3Sink {
	t.Helper()
	s, err := NewS3Sink(S3Config{
		Name: name, Endpoint: f.server.URL, Region: "decentralized", Bucket: "ditto-subnet-traces",
		AccessKeyID: "hip_test", SecretAccessKey: "secret", Required: required, PathStyle: true,
	}, f.server.Client())
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func (f *fakeS3) setFail(v bool) { f.mu.Lock(); f.fail = v; f.mu.Unlock() }

func (f *fakeS3) keys() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for k := range f.objects {
		out = append(out, k)
	}
	return out
}

func (f *fakeS3) decode(t *testing.T, key string) []Record {
	t.Helper()
	f.mu.Lock()
	body := f.objects[key]
	f.mu.Unlock()
	dec, err := zstd.NewReader(strings.NewReader(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	defer dec.Close()
	raw, err := io.ReadAll(dec)
	if err != nil {
		t.Fatal(err)
	}
	var out []Record
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var r Record
		if err := json.Unmarshal([]byte(line), &r); err != nil {
			t.Fatalf("bad line %q: %v", line, err)
		}
		out = append(out, r)
	}
	return out
}

func sampleRecord(kind string, i int) *Record {
	return &Record{
		Event:   EventSettled,
		Request: Request{Lane: LaneInference, Kind: kind, GrantID: "g", Nonce: "n", Body: json.RawMessage(`{"i":` + itoa(i) + `}`)},
	}
}

func itoa(i int) string { return strings.TrimSpace(strings.Repeat(" ", 0) + string(rune('0'+i%10))) }

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestSpoolRotatesAndUploadsToEverySink(t *testing.T) {
	dir := t.TempDir()
	s3a := newFakeS3(t)
	s3b := newFakeS3(t)
	spool, err := NewSpooler(SpoolOptions{Dir: dir, RotateBytes: 1, RotateInterval: time.Hour, Instance: "relay-1:8010"})
	if err != nil {
		t.Fatal(err)
	}
	primary := s3a.sink(t, "hippius", true)
	mirror := s3b.sink(t, "backblaze", false)
	for _, s := range []Sink{primary, mirror} {
		if err := s.Ensure(context.Background()); err != nil {
			t.Fatalf("ensure: %v", err)
		}
	}
	up, err := NewUploader(spool, UploaderOptions{Sinks: []Sink{primary, mirror}, PollInterval: 50 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	up.Start(ctx)

	spool.Record(sampleRecord(KindChat, 1))
	spool.Record(sampleRecord(KindEmbedding, 2))
	waitFor(t, "two objects in both sinks", func() bool { return len(s3a.keys()) == 2 && len(s3b.keys()) == 2 })
	for _, k := range s3a.keys() {
		if !strings.HasPrefix(k, "ditto-subnet-traces/traces/v1/lane=inference/kind=") || !strings.Contains(k, "/dt=") || !strings.Contains(k, "/hour=") || !strings.HasSuffix(k, ".jsonl.zst") {
			t.Fatalf("unexpected key layout: %s", k)
		}
		if !strings.Contains(k, "relay_1_8010-") {
			t.Fatalf("instance missing from key: %s", k)
		}
		recs := s3a.decode(t, k)
		if len(recs) != 1 || recs[0].Schema != SchemaVersion || recs[0].Relay.Instance != "relay-1:8010" || recs[0].Relay.Source != "relay" {
			t.Fatalf("record content: %+v", recs)
		}
	}
	waitFor(t, "local files released", func() bool {
		entries, _ := os.ReadDir(filepath.Join(dir, readyDirName))
		return len(entries) == 0
	})
	cancel()
	if err := spool.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if spool.Dropped() != 0 {
		t.Fatalf("dropped %d", spool.Dropped())
	}
}

func TestRequiredSinkOutageKeepsFileAndResumes(t *testing.T) {
	dir := t.TempDir()
	s3a := newFakeS3(t)
	s3b := newFakeS3(t)
	spool, err := NewSpooler(SpoolOptions{Dir: dir, RotateBytes: 1, RotateInterval: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	primary := s3a.sink(t, "hippius", true)
	mirror := s3b.sink(t, "backblaze", true)
	s3a.setFail(true)
	up, err := NewUploader(spool, UploaderOptions{Sinks: []Sink{primary, mirror}, PollInterval: 30 * time.Millisecond, RetryBackoff: 40 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	up.Start(ctx)
	spool.Record(sampleRecord(KindChat, 1))
	waitFor(t, "mirror got the object", func() bool { return len(s3b.keys()) == 1 })
	// Primary is down: the file must stay, with the sidecar recording the mirror.
	time.Sleep(150 * time.Millisecond)
	entries, _ := os.ReadDir(filepath.Join(dir, readyDirName))
	var side *sidecar
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".sinks.json") {
			side, _ = readSidecar(filepath.Join(dir, readyDirName, e.Name()))
		}
	}
	if side == nil || side.Completed["backblaze"] == "" || side.Completed["hippius"] != "" {
		t.Fatalf("sidecar: %+v (entries %d)", side, len(entries))
	}
	if len(s3a.keys()) != 0 {
		t.Fatalf("primary should have nothing yet")
	}
	mirrorPuts := s3b.puts
	s3a.setFail(false)
	waitFor(t, "primary caught up and file released", func() bool {
		entries, _ := os.ReadDir(filepath.Join(dir, readyDirName))
		return len(s3a.keys()) == 1 && len(entries) == 0
	})
	if s3b.puts != mirrorPuts {
		t.Fatalf("mirror was re-sent an object it already had")
	}
	if s3a.keys()[0] != s3b.keys()[0] {
		t.Fatalf("sinks disagree on key: %s vs %s", s3a.keys()[0], s3b.keys()[0])
	}
}

func TestRecoveryShipsTornLeftoverFromPreviousProcess(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, openDirName), 0o750); err != nil {
		t.Fatal(err)
	}
	good1, _ := json.Marshal(&Record{Schema: SchemaVersion, Event: EventSettled, RecordedAt: time.Date(2026, 8, 21, 17, 0, 0, 0, time.UTC), Request: Request{Lane: LaneInference, Kind: KindChat}})
	good2, _ := json.Marshal(&Record{Schema: SchemaVersion, Event: EventSettled, RecordedAt: time.Date(2026, 8, 21, 17, 4, 0, 0, time.UTC), Request: Request{Lane: LaneInference, Kind: KindChat}})
	content := string(good1) + "\n" + string(good2) + "\n" + `{"schema":"ditto.inference.trace.v1","event":"inference.settled","request":{"lane":"inference","ki`
	// A restart of the same slot keeps its instance identity, so the leftover
	// this process must adopt is one carrying its own name.
	leftover := openName("inference-chat", "relay-2", time.Date(2026, 8, 21, 17, 0, 0, 0, time.UTC))
	if err := os.WriteFile(filepath.Join(dir, openDirName, leftover), []byte(content), 0o640); err != nil {
		t.Fatal(err)
	}
	s3 := newFakeS3(t)
	spool, err := NewSpooler(SpoolOptions{Dir: dir, Instance: "relay-2"})
	if err != nil {
		t.Fatal(err)
	}
	up, err := NewUploader(spool, UploaderOptions{Sinks: []Sink{s3.sink(t, "hippius", true)}, PollInterval: 30 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	up.Start(ctx)
	waitFor(t, "leftover uploaded", func() bool { return len(s3.keys()) == 1 })
	key := s3.keys()[0]
	if !strings.Contains(key, "/dt=2026-08-21/hour=17/") {
		t.Fatalf("partition should come from the records' own time: %s", key)
	}
	recs := s3.decode(t, key)
	if len(recs) != 2 {
		t.Fatalf("torn tail must be dropped, complete lines kept: got %d records", len(recs))
	}
}

func TestSpoolBudgetDropsInsteadOfBlocking(t *testing.T) {
	dir := t.TempDir()
	spool, err := NewSpooler(SpoolOptions{Dir: dir, RotateBytes: 1 << 20, MaxSpoolBytes: 200, QueueSize: 8})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 50; i++ {
		spool.Record(sampleRecord(KindChat, i))
		time.Sleep(time.Millisecond)
	}
	if err := spool.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if spool.Dropped() == 0 {
		t.Fatalf("expected drops once the spool budget was exceeded")
	}
	if spool.Dropped() >= 50 {
		t.Fatalf("expected some records to land before the budget bit; dropped=%d", spool.Dropped())
	}
}

func TestCloseRotatesOpenFileIntoReady(t *testing.T) {
	dir := t.TempDir()
	spool, err := NewSpooler(SpoolOptions{Dir: dir, RotateBytes: 1 << 30, RotateInterval: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	spool.Record(sampleRecord(KindChat, 1))
	if err := spool.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	ready, _ := os.ReadDir(filepath.Join(dir, readyDirName))
	open, _ := os.ReadDir(filepath.Join(dir, openDirName))
	if len(ready) != 1 || len(open) != 0 {
		t.Fatalf("ready=%d open=%d", len(ready), len(open))
	}
	rf, ok := parseReadyName(filepath.Join(dir, readyDirName, ready[0].Name()))
	if !ok || rf.lane != LaneInference || rf.kind != KindChat {
		t.Fatalf("ready name not parseable: %s", ready[0].Name())
	}
	// A record after Close is dropped, never panics.
	spool.Record(sampleRecord(KindChat, 2))
	if spool.Dropped() != 1 {
		t.Fatalf("dropped=%d", spool.Dropped())
	}
}

func TestNilSpoolerIsANoOpRecorder(t *testing.T) {
	var s *Spooler
	var r Recorder = s
	r.Record(sampleRecord(KindChat, 1))
	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// A relay sharing a spool directory with its siblings must never adopt a file
// another live instance still has open: truncateToCompleteLines opens O_RDWR and
// truncates, which tears the owner's stream and leaves a NUL hole at its next
// buffered flush. Observed in production as objects named for one relay slot
// holding only other slots' records.
func TestRecoveryLeavesLiveSiblingOpenFileAlone(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, openDirName), 0o750); err != nil {
		t.Fatal(err)
	}
	rec, _ := json.Marshal(&Record{Schema: SchemaVersion, Event: EventSettled, RecordedAt: time.Date(2026, 8, 21, 17, 0, 0, 0, time.UTC), Request: Request{Lane: LaneInference, Kind: KindChat}})
	sibling := openName("inference-chat", "relay-8011", time.Date(2026, 8, 21, 17, 0, 0, 0, time.UTC))
	path := filepath.Join(dir, openDirName, sibling)
	if err := os.WriteFile(path, append(rec, '\n'), 0o640); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	spool, err := NewSpooler(SpoolOptions{Dir: dir, Instance: "relay-8010"})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.Close(context.Background())

	if _, err := os.Stat(path); err != nil {
		t.Fatalf("sibling's open file must stay in open/: %v", err)
	}
	after, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if after.Size() != before.Size() {
		t.Fatalf("sibling's file was truncated: %d -> %d bytes", before.Size(), after.Size())
	}
	entries, err := os.ReadDir(filepath.Join(dir, readyDirName))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("sibling's file must not be renamed into ready/: %v", entries[0].Name())
	}
}

// Once a sibling has gone quiet for long enough that it cannot still be
// writing, its leftover is adopted rather than stranded forever.
func TestRecoveryAdoptsAbandonedSiblingFile(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, openDirName), 0o750); err != nil {
		t.Fatal(err)
	}
	rec, _ := json.Marshal(&Record{Schema: SchemaVersion, Event: EventSettled, RecordedAt: time.Date(2026, 8, 21, 17, 0, 0, 0, time.UTC), Request: Request{Lane: LaneInference, Kind: KindChat}})
	for _, name := range []string{
		openName("inference-chat", "relay-8011", time.Date(2026, 8, 21, 17, 0, 0, 0, time.UTC)),
		"inference-chat-20260821T170000Z-legacy.jsonl", // pre-ownership name
	} {
		path := filepath.Join(dir, openDirName, name)
		if err := os.WriteFile(path, append(rec, '\n'), 0o640); err != nil {
			t.Fatal(err)
		}
		stale := time.Now().Add(-2 * time.Hour)
		if err := os.Chtimes(path, stale, stale); err != nil {
			t.Fatal(err)
		}
	}
	spool, err := NewSpooler(SpoolOptions{Dir: dir, Instance: "relay-8010"})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.Close(context.Background())

	open, err := os.ReadDir(filepath.Join(dir, openDirName))
	if err != nil {
		t.Fatal(err)
	}
	if len(open) != 0 {
		t.Fatalf("abandoned files should have left open/: %d remain", len(open))
	}
	ready, err := os.ReadDir(filepath.Join(dir, readyDirName))
	if err != nil {
		t.Fatal(err)
	}
	if len(ready) != 2 {
		t.Fatalf("both abandoned files should be in ready/: got %d", len(ready))
	}
	for _, e := range ready {
		if !strings.Contains(e.Name(), "inference-chat-relay_8010-") {
			t.Fatalf("adopted file should be renamed under the adopting instance: %s", e.Name())
		}
	}
}

func TestParseOpenNameRoundTrip(t *testing.T) {
	name := openName("inference-chat", "ditto-platform-prod:8010", time.Date(2026, 8, 31, 8, 51, 21, 0, time.UTC))
	stream, instance, ok := parseOpenName(name)
	if !ok {
		t.Fatalf("openName output must parse: %s", name)
	}
	if stream != "inference-chat" {
		t.Fatalf("stream = %q", stream)
	}
	if instance != "ditto_platform_prod_8010" {
		t.Fatalf("instance = %q", instance)
	}
	if _, _, ok := parseOpenName("inference-chat-20260821T170000Z-abc.jsonl"); ok {
		t.Fatal("a pre-ownership name must not parse as owned")
	}
}
