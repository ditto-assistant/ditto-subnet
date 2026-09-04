package traces

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func plantReadyFile(t *testing.T, dir string, recs int) *readyFile {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, readyDirName), 0o750); err != nil {
		t.Fatal(err)
	}
	first := time.Date(2026, 9, 3, 2, 44, 33, 0, time.UTC)
	last := first.Add(5 * time.Minute)
	name := readyName("inference-embedding", "relay-8010", first, last)
	path := filepath.Join(dir, readyDirName, name)
	var b strings.Builder
	for i := 0; i < recs; i++ {
		line, _ := json.Marshal(&Record{
			Schema: SchemaVersion, Event: EventSettled, RecordedAt: first,
			Request: Request{Lane: LaneInference, Kind: KindEmbedding},
		})
		b.Write(line)
		b.WriteString("\n")
	}
	if err := os.WriteFile(path, []byte(b.String()), 0o640); err != nil {
		t.Fatal(err)
	}
	rf, ok := parseReadyName(path)
	if !ok {
		t.Fatalf("planted file does not parse: %s", name)
	}
	return rf
}

func newTestUploader(t *testing.T, dir string, s3 *fakeS3) *Uploader {
	t.Helper()
	spool, err := NewSpooler(SpoolOptions{Dir: dir, Instance: "relay-8010"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = spool.Close(context.Background()) })
	up, err := NewUploader(spool, UploaderOptions{
		Sinks: []Sink{s3.sink(t, "hippius", true)}, PollInterval: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	return up
}

func TestShipRebuildsAnUnusableArtifactFromSource(t *testing.T) {
	dir := t.TempDir()
	s3 := newFakeS3(t)
	up := newTestUploader(t, dir, s3)
	rf := plantReadyFile(t, dir, 40)

	zstPath := rf.path + ".zst"
	sidePath := rf.path + ".sinks.json"
	if err := os.WriteFile(zstPath, []byte("husk!\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeSidecar(sidePath, &sidecar{
		Key: objectKey("traces/v1", rf), SHA256: "stale", Bytes: 6,
		Completed: map[string]string{},
	}); err != nil {
		t.Fatal(err)
	}

	if err := up.ship(context.Background(), rf); err != nil {
		t.Fatalf("ship should rebuild and succeed: %v", err)
	}
	keys := s3.keys()
	if len(keys) != 1 {
		t.Fatalf("expected one stored object, got %v", keys)
	}
	if recs := s3.decode(t, keys[0]); len(recs) != 40 {
		t.Fatalf("rebuilt object should carry every source record, got %d", len(recs))
	}
}

func TestShipRefusesToUploadAnUnusableArtifactItCannotRebuild(t *testing.T) {
	dir := t.TempDir()
	s3 := newFakeS3(t)
	up := newTestUploader(t, dir, s3)
	rf := plantReadyFile(t, dir, 12)

	zstPath := rf.path + ".zst"
	sidePath := rf.path + ".sinks.json"
	if err := os.WriteFile(zstPath, []byte("husk!\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeSidecar(sidePath, &sidecar{
		Key: objectKey("traces/v1", rf), SHA256: "stale", Bytes: 6,
		Completed: map[string]string{},
	}); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(rf.path); err != nil {
		t.Fatal(err)
	}

	err := up.ship(context.Background(), rf)
	if err == nil {
		t.Fatal("ship must fail rather than store an unusable artifact")
	}
	if got := s3.keys(); len(got) != 0 {
		t.Fatalf("nothing should have been stored, got %v", got)
	}
}

func TestValidCompressedAcceptsRealOutputAndRejectsHusks(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.jsonl")
	if err := os.WriteFile(src, []byte("{\"a\":1}\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "out.zst")
	sum, size, err := compressFile(src, dst)
	if err != nil {
		t.Fatal(err)
	}
	good := &sidecar{SHA256: sum, Bytes: size}
	if err := validCompressed(dst, good); err != nil {
		t.Fatalf("real compressor output must validate: %v", err)
	}

	for name, body := range map[string][]byte{
		"empty":       {},
		"six-bytes":   []byte("husk!\n"),
		"wrong-magic": []byte("not a zstd frame at all"),
	} {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, body, 0o640); err != nil {
			t.Fatal(err)
		}
		if err := validCompressed(p, &sidecar{Bytes: int64(len(body))}); err == nil {
			t.Fatalf("%s must be rejected", name)
		}
	}

	if err := validCompressed(dst, &sidecar{SHA256: sum, Bytes: size + 1}); err == nil {
		t.Fatal("size disagreeing with the sidecar must be rejected")
	}
}
