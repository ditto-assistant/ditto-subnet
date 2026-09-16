package codinglaunchjournal

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"syscall"
	"testing"
)

type fakeObject struct {
	id     string
	labels map[string]string
}

// fakeDocker models only what the reconciler and sentinel use. It records
// every call so tests can assert what was never touched.
type fakeDocker struct {
	mu         sync.Mutex
	containers map[string]fakeObject
	networks   map[string]fakeObject
	calls      [][]string
	failRemove bool
	inspectErr bool
}

func newFakeDocker() *fakeDocker {
	return &fakeDocker{containers: map[string]fakeObject{}, networks: map[string]fakeObject{}}
}

func objectID(name string) string {
	sum := sha256.Sum256([]byte(name))
	return hex.EncodeToString(sum[:])
}

func (d *fakeDocker) add(kind, name string, labels map[string]string) {
	target := d.containers
	if kind == "network" {
		target = d.networks
	}
	target[name] = fakeObject{id: objectID(name), labels: labels}
}

func (d *fakeDocker) lookup(kind, reference string) (string, fakeObject, bool) {
	target := d.containers
	if kind == "network" {
		target = d.networks
	}
	for name, object := range target {
		if name == reference || object.id == reference {
			return name, object, true
		}
	}
	return "", fakeObject{}, false
}

func (d *fakeDocker) Output(_ context.Context, args ...string) ([]byte, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, slices.Clone(args))
	kind := args[0]
	switch {
	case len(args) == 5 && args[1] == "inspect":
		if d.inspectErr {
			return []byte("Cannot connect to the Docker daemon"), errors.New("exit 1")
		}
		reference := args[4]
		_, object, ok := d.lookup(kind, reference)
		if !ok {
			if kind == "network" {
				return []byte("Error response from daemon: network " + reference + " not found\n"), errors.New("exit 1")
			}
			return []byte("Error: No such container: " + reference + "\n"), errors.New("exit 1")
		}
		labels, _ := json.Marshal(object.labels)
		return []byte(object.id + " " + string(labels) + "\n"), nil
	case kind == "container" && args[1] == "rm" || kind == "network" && args[1] == "rm":
		if d.failRemove {
			return []byte("busy"), errors.New("exit 1")
		}
		reference := args[len(args)-1]
		name, _, ok := d.lookup(kind, reference)
		if ok {
			if kind == "network" {
				delete(d.networks, name)
			} else {
				delete(d.containers, name)
			}
		}
		return []byte(reference + "\n"), nil
	case kind == "network" && args[1] == "create":
		labels := map[string]string{}
		for index := 2; index < len(args)-1; index++ {
			if args[index] == "--label" {
				key, value, _ := strings.Cut(args[index+1], "=")
				labels[key] = value
			}
		}
		d.add("network", args[len(args)-1], labels)
		return []byte(objectID(args[len(args)-1]) + "\n"), nil
	}
	return nil, fmt.Errorf("unexpected docker call %v", args)
}

func (d *fakeDocker) touched(name string) bool {
	id := objectID(name)
	for _, call := range d.calls {
		if slices.Contains(call, name) || slices.Contains(call, id) {
			return true
		}
	}
	return false
}

func (d *fakeDocker) removed(name string) bool {
	id := objectID(name)
	for _, call := range d.calls {
		if call[1] == "rm" && (slices.Contains(call, name) || slices.Contains(call, id)) {
			return true
		}
	}
	return false
}

func privateDir(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "journal")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return real
}

func open(t *testing.T, dir string) *Journal {
	t.Helper()
	journal, err := Open(dir, "attempt-1", "worker-1")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = journal.Close() })
	return journal
}

func owned(run string) map[string]string { return map[string]string{OwnerLabel: run} }

func TestRecordSyncsFileThenDirectoryBeforeReturning(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	var order []string
	journal.sync = func(kind string, file *os.File) error {
		// The whole entry is already written when the file is synced.
		if body, err := os.ReadFile(filepath.Join(dir, FileName)); err != nil || !bytes.HasSuffix(body, []byte("]}\n")) {
			t.Errorf("sync before the entry was written: %q %v", body, err)
		}
		order = append(order, kind)
		return file.Sync()
	}
	if err := journal.Record("abc123", []string{"dittobench-abc123"}, []string{"ditto-job-abc123"}); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(order, []string{"file", "directory"}) {
		t.Fatalf("sync order = %v", order)
	}
	journal.sync = func(kind string, file *os.File) error {
		if kind == "directory" {
			return errors.New("sync failed")
		}
		return nil
	}
	if journal.Record("abc124", []string{"dittobench-abc124"}, nil) == nil {
		t.Fatal("a failed directory sync still let the launch proceed")
	}
}

func TestJournalFileIsOwnerOnlySingleLinkAndIDsOnly(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	if err := journal.Record("0123456789abcdef", []string{"dittobench-0123456789abcdef"}, []string{"ditto-job-0123456789abcdef"}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, FileName)
	info, err := os.Lstat(path)
	if err != nil || info.Mode().Perm() != 0o600 || !info.Mode().IsRegular() || info.Sys().(*syscall.Stat_t).Nlink != 1 {
		t.Fatalf("journal file mode %v %v", info.Mode(), err)
	}
	body, _ := os.ReadFile(path)
	want := `{"schema":"dittobench-coding-launch-journal-entry-v1","attempt":"attempt-1","worker":"worker-1","run":"0123456789abcdef","containers":["dittobench-0123456789abcdef"],"networks":["ditto-job-0123456789abcdef"]}` + "\n"
	if string(body) != want {
		t.Fatalf("journal body = %s", body)
	}
	var keys map[string]any
	if json.Unmarshal(bytes.TrimSpace(body), &keys) != nil || len(keys) != 6 {
		t.Fatal("entry keys are not the closed id set")
	}
	// Anything that is not an identifier is refused before it is written.
	for _, bad := range []struct {
		run                  string
		containers, networks []string
	}{
		{"ABC", []string{"dittobench-a"}, nil},
		{"abc", []string{"/home/secret"}, nil},
		{"abc", []string{"dittobench-a=OPENROUTER_API_KEY"}, nil},
		{"abc", nil, []string{"bridge"}},
		{"abc", nil, nil},
		{"abc", []string{"dittobench-a b"}, nil},
		{strings.Repeat("a", 129), []string{"dittobench-a"}, nil},
	} {
		if journal.Record(bad.run, bad.containers, bad.networks) == nil {
			t.Fatalf("recorded a non-identifier entry %v", bad)
		}
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(after, body) {
		t.Fatal("a refused entry changed the journal")
	}
}

func TestDecodeEntryRefusesExtraFieldsAndOtherSpellings(t *testing.T) {
	good := `{"schema":"dittobench-coding-launch-journal-entry-v1","attempt":"a","worker":"w","run":"r","containers":["dittobench-r"],"networks":[]}`
	if _, err := DecodeEntry([]byte(good)); err != nil {
		t.Fatal(err)
	}
	for _, line := range []string{
		strings.Replace(good, `"networks":[]`, `"networks":[],"env":["TOKEN=x"]`, 1),
		strings.Replace(good, `"run":"r"`, `"run":"r","run":"s"`, 1),
		strings.Replace(good, `{"schema"`, `{ "schema"`, 1),
		strings.Replace(good, `"networks":[]`, `"networks":null`, 1),
		strings.Replace(good, `dittobench-r`, `dittobench-\u0072`, 1),
		strings.Replace(good, `entry-v1`, `entry-v2`, 1),
	} {
		if line == good {
			t.Fatal("mutation did not apply")
		}
		if _, err := DecodeEntry([]byte(line)); err == nil {
			t.Fatalf("decoded %s", line)
		}
	}
}

func TestOpenRefusesSymlinksSharedDirectoriesAndLiveOwners(t *testing.T) {
	dir := privateDir(t)
	link := filepath.Join(filepath.Dir(dir), "link")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(link, "a", "w"); err == nil {
		t.Fatal("followed a symlinked journal directory")
	}
	if err := os.Chmod(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, "a", "w"); err == nil {
		t.Fatal("opened a group-readable journal directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, "Attempt", "w"); err == nil {
		t.Fatal("opened with a non-identifier attempt")
	}
	first := open(t, dir)
	if _, err := Open(dir, "a", "w"); !errors.Is(err, ErrLocked) {
		t.Fatalf("second owner opened a live journal: %v", err)
	}
	_ = first.Close()
	second, err := Open(dir, "a", "w")
	if err != nil {
		t.Fatalf("lock not released on close: %v", err)
	}
	_ = second.Close()
}

func TestJournalFileSymlinkAndHardLinkAreRefused(t *testing.T) {
	dir := privateDir(t)
	target := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.WriteFile(target, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(dir, FileName)); err != nil {
		t.Fatal(err)
	}
	journal := open(t, dir)
	if journal.Record("abc", []string{"dittobench-abc"}, nil) == nil {
		t.Fatal("appended through a symlink")
	}
	if _, err := journal.Reconcile(t.Context(), newFakeDocker()); err == nil {
		t.Fatal("reconciled through a symlink")
	}
	if err := os.Remove(filepath.Join(dir, FileName)); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(target, filepath.Join(dir, FileName)); err != nil {
		t.Fatal(err)
	}
	if journal.Record("abc", []string{"dittobench-abc"}, nil) == nil {
		t.Fatal("appended to a hard-linked journal")
	}
	body, _ := os.ReadFile(target)
	if len(body) != 0 {
		t.Fatal("wrote outside the journal directory")
	}
}

func TestJournalSizeAndEntriesAreBounded(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	journal.sync = func(string, *os.File) error { return nil }
	count := 0
	for ; count <= MaxEntries; count++ {
		if journal.Record(fmt.Sprintf("r%d", count), []string{fmt.Sprintf("dittobench-r%d", count)}, nil) != nil {
			break
		}
	}
	if count != MaxEntries {
		t.Fatalf("accepted %d entries", count)
	}
	info, _ := os.Stat(filepath.Join(dir, FileName))
	if info.Size() > MaxBytes {
		t.Fatalf("journal grew to %d bytes", info.Size())
	}
	// A file already at the byte bound refuses the next append.
	if err := os.Truncate(filepath.Join(dir, FileName), MaxBytes); err != nil {
		t.Fatal(err)
	}
	if journal.Record("x", []string{"dittobench-x"}, nil) == nil {
		t.Fatal("appended past the byte bound")
	}
	if _, err := journal.Reconcile(t.Context(), newFakeDocker()); err == nil {
		t.Fatal("reconciled a corrupt journal")
	}
}

func TestReconcileRemovesOnlyJournaledOwnedObjectsAndRotates(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	docker := newFakeDocker()
	if err := journal.Record("aaaa", []string{"dittobench-aaaa"}, []string{"ditto-job-aaaa"}); err != nil {
		t.Fatal(err)
	}
	sentinel, err := journal.Sentinel(t.Context(), docker)
	if err != nil {
		t.Fatal(err)
	}
	docker.add("container", "dittobench-aaaa", owned("aaaa"))
	docker.add("network", "ditto-job-aaaa", owned("aaaa"))
	// Labelled like the runtime but never journaled: untouched.
	docker.add("container", "dittobench-bbbb", owned("bbbb"))
	docker.add("network", "ditto-job-bbbb", owned("bbbb"))
	docker.add("container", "unrelated", map[string]string{})
	report, err := journal.Reconcile(t.Context(), docker)
	if err != nil {
		t.Fatal(err)
	}
	if report.RemovedContainers != 1 || report.RemovedNetworks != 2 || report.Entries != 2 {
		t.Fatalf("report = %+v", report)
	}
	if _, ok := docker.networks[sentinel]; ok {
		t.Fatal("sentinel network survived reconciliation")
	}
	for _, name := range []string{"dittobench-bbbb", "ditto-job-bbbb", "unrelated"} {
		if docker.touched(name) {
			t.Fatalf("reconciler touched unjournaled %s", name)
		}
	}
	if _, err := os.Lstat(filepath.Join(dir, FileName)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("journal was not rotated")
	}
	if _, err := os.Lstat(filepath.Join(dir, ReconciledName)); err != nil {
		t.Fatal("reconciled copy missing")
	}
	pending, err := journal.Pending()
	if err != nil || pending {
		t.Fatal("journal still pending after reconciliation")
	}
	// Idempotent: a second run inspects and removes nothing.
	calls := len(docker.calls)
	if report, err := journal.Reconcile(t.Context(), docker); err != nil || report != (Report{}) || len(docker.calls) != calls {
		t.Fatalf("second reconcile did work: %+v %v", report, err)
	}
}

func TestReconcileRefusesUnlabelledJournaledNamesAndRemovesNothing(t *testing.T) {
	for name, labels := range map[string]map[string]string{
		"unlabelled":  {},
		"other run":   owned("zzzz"),
		"other label": {"io.heyditto.dittobench.coding-executor": "aaaa"},
	} {
		t.Run(name, func(t *testing.T) {
			dir := privateDir(t)
			journal := open(t, dir)
			docker := newFakeDocker()
			if err := journal.Record("aaaa", []string{"dittobench-aaaa", "dittobench-aaab"}, []string{"ditto-job-aaaa"}); err != nil {
				t.Fatal(err)
			}
			docker.add("container", "dittobench-aaaa", owned("aaaa"))
			docker.add("container", "dittobench-aaab", labels)
			docker.add("network", "ditto-job-aaaa", owned("aaaa"))
			if _, err := journal.Reconcile(t.Context(), docker); !errors.Is(err, ErrForeign) {
				t.Fatalf("err = %v", err)
			}
			for _, name := range []string{"dittobench-aaaa", "dittobench-aaab", "ditto-job-aaaa"} {
				if docker.removed(name) {
					t.Fatalf("removed %s despite a foreign journaled name", name)
				}
			}
			if pending, _ := journal.Pending(); !pending {
				t.Fatal("journal rotated after a refused reconciliation")
			}
		})
	}
}

func TestCrashBetweenJournalWriteAndStartReconciles(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	if err := journal.Record("cccc", []string{"dittobench-cccc"}, []string{"ditto-job-cccc"}); err != nil {
		t.Fatal(err)
	}
	// The process dies before docker create: nothing exists. A torn append
	// from a later Record that never returned is also on disk.
	file, err := os.OpenFile(filepath.Join(dir, FileName), os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = file.WriteString(`{"schema":"dittobench-coding-launch-journal-entry-v1","attempt":"attempt-1","wor`)
	_ = file.Close()
	_ = journal.Close()
	restarted := open(t, dir)
	pending, err := restarted.Pending()
	if err != nil || !pending {
		t.Fatalf("pending = %v %v", pending, err)
	}
	// A later append completes the torn line first and stays readable.
	if err := restarted.Record("dddd", []string{"dittobench-dddd"}, nil); err != nil {
		t.Fatal(err)
	}
	entries, err := restarted.Entries()
	if err != nil || len(entries) != 2 {
		t.Fatalf("entries = %v %v", entries, err)
	}
	docker := newFakeDocker()
	report, err := restarted.Reconcile(t.Context(), docker)
	if err != nil || report.AbsentContainers != 2 || report.AbsentNetworks != 1 || report.RemovedContainers != 0 {
		t.Fatalf("report = %+v %v", report, err)
	}
	for _, call := range docker.calls {
		if call[1] != "inspect" {
			t.Fatalf("absent objects caused %v", call)
		}
	}
}

func TestReconcileFailsClosedWhenDockerCannotConfirm(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	if err := journal.Record("eeee", []string{"dittobench-eeee"}, nil); err != nil {
		t.Fatal(err)
	}
	docker := newFakeDocker()
	docker.inspectErr = true
	if _, err := journal.Reconcile(t.Context(), docker); !errors.Is(err, ErrUnconfirmed) {
		t.Fatalf("daemon error treated as absent: %v", err)
	}
	docker.inspectErr = false
	docker.add("container", "dittobench-eeee", owned("eeee"))
	docker.failRemove = true
	if _, err := journal.Reconcile(t.Context(), docker); !errors.Is(err, ErrUnconfirmed) {
		t.Fatalf("unremoved container accepted: %v", err)
	}
	if pending, _ := journal.Pending(); !pending {
		t.Fatal("rotated without confirmed removal")
	}
	docker.failRemove = false
	if _, err := journal.Reconcile(t.Context(), docker); err != nil {
		t.Fatal(err)
	}
}

func TestCorruptCompleteLineRefusesReconciliation(t *testing.T) {
	dir := privateDir(t)
	journal := open(t, dir)
	if err := journal.Record("ffff", []string{"dittobench-ffff"}, nil); err != nil {
		t.Fatal(err)
	}
	file, _ := os.OpenFile(filepath.Join(dir, FileName), os.O_WRONLY|os.O_APPEND, 0)
	_, _ = file.WriteString(`{"schema":"x"}` + "\n")
	_ = file.Close()
	if _, err := journal.Reconcile(t.Context(), newFakeDocker()); !errors.Is(err, ErrJournal) {
		t.Fatalf("corrupt journal reconciled: %v", err)
	}
}
