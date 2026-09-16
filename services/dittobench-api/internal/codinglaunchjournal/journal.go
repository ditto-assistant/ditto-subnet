// Package codinglaunchjournal is the hosted coding runtime's durable launch
// intent journal and its reconciler (B5).
//
// Before the runtime creates any Docker container or network it appends one
// entry naming only identifiers: the attempt and worker ids, the ownership
// label value, and the container and network names it is about to create. The
// entry is fsynced, and so is the directory, before the create runs. A runner
// killed with SIGKILL therefore always leaves a journal that names every
// object it may have created.
//
// The reconciler reads the journal, force-removes every journaled container
// and network that still exists AND carries the runtime ownership label with
// the journaled value, verifies each is absent, and then rotates the journal
// atomically. It never lists, filters or prunes: an object that is not named in
// the journal is never inspected, and a journaled name whose object carries
// another label is left untouched and fails reconciliation. The runtime
// refuses to start an attempt until reconciliation succeeds.
//
// Nothing here runs on a host by itself; it is the runtime's own launch path.
package codinglaunchjournal

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"

	"golang.org/x/sys/unix"
)

const (
	// EntrySchema names one journal line. Its keys are closed.
	EntrySchema = "dittobench-coding-launch-journal-entry-v1"
	// FileName is the pending journal inside the journal directory.
	FileName = "launch-journal"
	// ReconciledName is the last reconciled journal, kept for inspection.
	ReconciledName = "launch-journal.reconciled"
	// LockName is held (flock) by the one process that owns the directory.
	LockName = "launch-journal.lock"
	// MaxBytes bounds the journal file; an append past it is refused, so the
	// launch it would have announced never happens.
	MaxBytes = 1 << 20
	// MaxEntries bounds the number of entries. An attempt journals one entry
	// per container (at most 1000 authoring tool calls, plus probes, grading
	// and the harness), so this leaves room without letting it grow unbounded.
	MaxEntries = 4096
	// OwnerLabel is the runtime ownership label. Every container and network
	// the hosted launch paths create carries it with the journaled run value.
	OwnerLabel = "io.heyditto.dittobench.run"
	// SentinelLabel marks the per-attempt sentinel network.
	SentinelLabel = "io.heyditto.dittobench.launch-sentinel"
	// SentinelPrefix starts every sentinel network name.
	SentinelPrefix = "ditto-job-sentinel-"

	maxEntryBytes    = 4096
	maxNamesPerEntry = 8
	maxDockerOutput  = 64 << 10
)

var (
	ErrJournal = errors.New("launch journal refused")
	// ErrPending means a journal exists that has not been reconciled.
	ErrPending = errors.New("launch journal has unreconciled entries")
	// ErrForeign means a journaled name exists without the journaled
	// ownership label. Nothing was removed.
	ErrForeign = errors.New("journaled name exists without the journaled ownership label")
	// ErrUnconfirmed means removal of a journaled object was not confirmed.
	ErrUnconfirmed = errors.New("journaled object removal unconfirmed")
	// ErrLocked means another live process owns the journal directory.
	ErrLocked = errors.New("launch journal directory is in use")
)

var (
	idPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)
	runPattern       = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,127}$`)
	containerPattern = regexp.MustCompile(`^dittobench-[a-z0-9][a-z0-9-]{0,100}$`)
	networkPattern   = regexp.MustCompile(`^ditto-job-[a-z0-9][a-z0-9-]{0,100}$`)
	objectIDPattern  = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// Entry is one journal line: identifiers only. No environment, path, image,
// command, output or credential has a field here, and decoding refuses any
// other key.
type Entry struct {
	Schema     string   `json:"schema"`
	Attempt    string   `json:"attempt"`
	Worker     string   `json:"worker"`
	Run        string   `json:"run"`
	Containers []string `json:"containers"`
	Networks   []string `json:"networks"`
}

// Validate checks every identifier against its closed pattern.
func (e Entry) Validate() error {
	if e.Schema != EntrySchema || !idPattern.MatchString(e.Attempt) || !idPattern.MatchString(e.Worker) || !runPattern.MatchString(e.Run) ||
		e.Containers == nil || e.Networks == nil || len(e.Containers)+len(e.Networks) == 0 ||
		len(e.Containers) > maxNamesPerEntry || len(e.Networks) > maxNamesPerEntry {
		return ErrJournal
	}
	for _, name := range e.Containers {
		if !containerPattern.MatchString(name) {
			return ErrJournal
		}
	}
	for _, name := range e.Networks {
		if !networkPattern.MatchString(name) {
			return ErrJournal
		}
	}
	return nil
}

// DecodeEntry parses one line strictly: closed keys, one value, valid ids.
func DecodeEntry(line []byte) (Entry, error) {
	var entry Entry
	decoder := json.NewDecoder(bytes.NewReader(line))
	decoder.DisallowUnknownFields()
	if len(line) == 0 || len(line) > maxEntryBytes || decoder.Decode(&entry) != nil || decoder.More() || entry.Validate() != nil {
		return Entry{}, ErrJournal
	}
	// Re-encoding must reproduce the line: no duplicate keys, no whitespace,
	// no escapes that hide another spelling.
	encoded, err := json.Marshal(entry)
	if err != nil || !bytes.Equal(encoded, line) {
		return Entry{}, ErrJournal
	}
	return entry, nil
}

// Docker runs the docker CLI; Output returns combined output.
type Docker interface {
	Output(ctx context.Context, args ...string) ([]byte, error)
}

// Journal is an open journal directory owned by this process.
type Journal struct {
	mu        sync.Mutex
	directory *os.File
	lock      *os.File
	attempt   string
	worker    string
	// sync is replaced only by tests, to observe durability ordering.
	sync func(kind string, file *os.File) error
	// The lock owner is the only writer, so after one full read the entry
	// count and size are tracked instead of re-reading on every append.
	counted bool
	entries int
	size    int64
}

// Open validates the private journal directory and takes its exclusive lock.
// The directory must be an owner-only (0700) real directory owned by the
// effective user; symlinks are never followed.
func Open(directory, attempt, worker string) (*Journal, error) {
	if !idPattern.MatchString(attempt) || !idPattern.MatchString(worker) {
		return nil, ErrJournal
	}
	dir, err := openPrivateDirectory(directory)
	if err != nil {
		return nil, err
	}
	fd, err := unix.Openat(int(dir.Fd()), LockName, unix.O_RDWR|unix.O_CREAT|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0o600)
	if err != nil {
		_ = dir.Close()
		return nil, ErrJournal
	}
	lock := os.NewFile(uintptr(fd), "launch-journal-lock")
	if checkPrivateFile(lock, 0) != nil {
		_ = lock.Close()
		_ = dir.Close()
		return nil, ErrJournal
	}
	if unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB) != nil {
		_ = lock.Close()
		_ = dir.Close()
		return nil, ErrLocked
	}
	return &Journal{directory: dir, lock: lock, attempt: attempt, worker: worker, sync: func(_ string, file *os.File) error { return file.Sync() }}, nil
}

// Close releases the lock. A pending journal stays for the next reconcile.
func (j *Journal) Close() error {
	if j == nil {
		return nil
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	lockErr := j.lock.Close()
	return errors.Join(lockErr, j.directory.Close())
}

func openPrivateDirectory(path string) (*os.File, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == "/" {
		return nil, ErrJournal
	}
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, ErrJournal
	}
	dir := os.NewFile(uintptr(fd), "launch-journal-directory")
	info, err := dir.Stat()
	if err != nil {
		_ = dir.Close()
		return nil, ErrJournal
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	// O_NOFOLLOW covers the last component; the resolved path must also be the
	// path itself, so no ancestor is a symlink.
	real, realErr := filepath.EvalSymlinks(path)
	if !info.IsDir() || info.Mode().Perm() != 0o700 || !ok || stat.Uid != uint32(os.Geteuid()) || realErr != nil || real != path {
		_ = dir.Close()
		return nil, ErrJournal
	}
	return dir, nil
}

// checkPrivateFile requires a single-link, owner-only regular file of the
// effective user, at most maximum bytes when maximum is positive.
func checkPrivateFile(file *os.File, maximum int64) error {
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return ErrJournal
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || maximum > 0 && info.Size() > maximum {
		return ErrJournal
	}
	return nil
}

// Record durably appends one intent: the file and then the directory are
// fsynced before it returns. The caller creates nothing unless it returns nil.
func (j *Journal) Record(run string, containers, networks []string) error {
	if j == nil {
		return ErrJournal
	}
	entry := Entry{Schema: EntrySchema, Attempt: j.attempt, Worker: j.worker, Run: run,
		Containers: append([]string{}, containers...), Networks: append([]string{}, networks...)}
	if entry.Validate() != nil {
		return ErrJournal
	}
	line, err := json.Marshal(entry)
	if err != nil || len(line) > maxEntryBytes {
		return ErrJournal
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	fd, err := unix.Openat(int(j.directory.Fd()), FileName, unix.O_RDWR|unix.O_APPEND|unix.O_CREAT|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0o600)
	if err != nil {
		return ErrJournal
	}
	file := os.NewFile(uintptr(fd), "launch-journal")
	defer file.Close()
	if checkPrivateFile(file, MaxBytes) != nil {
		return ErrJournal
	}
	info, err := file.Stat()
	if err != nil {
		return ErrJournal
	}
	if !j.counted || j.size != info.Size() {
		j.counted, j.entries = false, 0
		if info.Size() > 0 {
			existing, err := readAll(file, info.Size())
			if err != nil {
				return ErrJournal
			}
			j.entries = bytes.Count(existing, []byte{'\n'})
			// A torn tail from an earlier crash is completed by a newline
			// first so this entry stays a line of its own; the torn part is
			// ignored.
			if existing[len(existing)-1] != '\n' {
				line = append([]byte{'\n'}, line...)
			}
		}
	}
	if j.entries >= MaxEntries {
		return ErrJournal
	}
	line = append(line, '\n')
	if info.Size()+int64(len(line)) > MaxBytes {
		return ErrJournal
	}
	written, err := file.Write(line)
	// Whatever happened, the next append recounts from the file.
	j.counted = false
	if err != nil || written != len(line) {
		return ErrJournal
	}
	if j.sync("file", file) != nil || j.sync("directory", j.directory) != nil {
		return ErrJournal
	}
	j.counted, j.entries, j.size = true, j.entries+bytes.Count(line, []byte{'\n'}), info.Size()+int64(len(line))
	return nil
}

func readAll(file *os.File, size int64) ([]byte, error) {
	body := make([]byte, size)
	if _, err := file.ReadAt(body, 0); err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	return body, nil
}

// Intent is the launch hook shape the sandbox and executor packages call.
func (j *Journal) Intent(_ context.Context, run string, containers, networks []string) error {
	return j.Record(run, containers, networks)
}

// Pending reports whether a journal file is waiting for reconciliation.
func (j *Journal) Pending() (bool, error) {
	_, present, err := j.read()
	return present, err
}

// Entries returns the complete entries of the pending journal.
func (j *Journal) Entries() ([]Entry, error) {
	entries, _, err := j.read()
	return entries, err
}

func (j *Journal) read() ([]Entry, bool, error) {
	if j == nil {
		return nil, false, ErrJournal
	}
	fd, err := unix.Openat(int(j.directory.Fd()), FileName, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if errors.Is(err, unix.ENOENT) {
		return nil, false, nil
	}
	if err != nil {
		return nil, true, ErrJournal
	}
	file := os.NewFile(uintptr(fd), "launch-journal")
	defer file.Close()
	if checkPrivateFile(file, MaxBytes) != nil {
		return nil, true, ErrJournal
	}
	info, err := file.Stat()
	if err != nil {
		return nil, true, ErrJournal
	}
	body, err := readAll(file, info.Size())
	if err != nil {
		return nil, true, ErrJournal
	}
	lines := bytes.Split(body, []byte{'\n'})
	// The final element is empty after a complete last line, or a torn append
	// whose Record never returned, so nothing was created for it. Anything
	// else there is corruption.
	if tail := lines[len(lines)-1]; len(tail) > 0 && !isTornPrefix(tail) {
		return nil, true, ErrJournal
	}
	lines = lines[:len(lines)-1]
	if len(lines) > MaxEntries {
		return nil, true, ErrJournal
	}
	entries := make([]Entry, 0, len(lines))
	for _, line := range lines {
		entry, err := DecodeEntry(line)
		if err != nil {
			// A torn tail followed by a later entry: the torn part is the
			// prefix Record completed with a newline.
			if isTornPrefix(line) {
				continue
			}
			return nil, true, ErrJournal
		}
		entries = append(entries, entry)
	}
	return entries, true, nil
}

// isTornPrefix reports a line that can only be an interrupted append: a strict
// prefix of a JSON entry, which never ends in the closing brace.
func isTornPrefix(line []byte) bool {
	if len(line) == 0 || len(line) > maxEntryBytes || line[0] != '{' || line[len(line)-1] == '}' {
		return false
	}
	for _, character := range line {
		if character < 0x20 || character > 0x7e {
			return false
		}
	}
	return true
}

// Report counts what one reconciliation did.
type Report struct {
	Entries           int
	RemovedContainers int
	RemovedNetworks   int
	AbsentContainers  int
	AbsentNetworks    int
}

type object struct {
	kind string // "container" or "network"
	name string
	run  string
	id   string
}

// Reconcile removes every journaled object that still exists and carries the
// journaled ownership label, confirms each is absent, and rotates the journal.
// Nothing is removed unless every present journaled object is owned. It is
// idempotent: with no pending journal it does nothing.
func (j *Journal) Reconcile(ctx context.Context, docker Docker) (Report, error) {
	if j == nil || ctx == nil || docker == nil {
		return Report{}, ErrJournal
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	entries, present, err := j.read()
	if err != nil {
		return Report{}, err
	}
	if !present {
		return Report{}, nil
	}
	report := Report{Entries: len(entries)}
	var objects []object
	seen := map[string]string{}
	for _, entry := range entries {
		for _, name := range entry.Containers {
			objects = append(objects, object{kind: "container", name: name, run: entry.Run})
		}
		for _, name := range entry.Networks {
			objects = append(objects, object{kind: "network", name: name, run: entry.Run})
		}
	}
	unique := objects[:0]
	for _, item := range objects {
		key := item.kind + "\x00" + item.name
		if run, ok := seen[key]; ok {
			if run != item.run {
				return report, ErrJournal
			}
			continue
		}
		seen[key] = item.run
		unique = append(unique, item)
	}
	objects = unique
	// Containers first: a network cannot be removed while one is attached.
	ordered := make([]object, 0, len(objects))
	for _, kind := range []string{"container", "network"} {
		for _, item := range objects {
			if item.kind == kind {
				ordered = append(ordered, item)
			}
		}
	}
	// Phase one: inspect every journaled name. Any foreign object stops
	// reconciliation before anything is removed.
	var owned []object
	for _, item := range ordered {
		id, labels, exists, err := inspect(ctx, docker, item.kind, item.name)
		if err != nil {
			return report, err
		}
		if !exists {
			if item.kind == "container" {
				report.AbsentContainers++
			} else {
				report.AbsentNetworks++
			}
			continue
		}
		if labels[OwnerLabel] != item.run {
			return report, ErrForeign
		}
		item.id = id
		owned = append(owned, item)
	}
	// Phase two: remove by exact id and confirm absence by id and by name.
	for _, item := range owned {
		args := []string{"container", "rm", "--force", item.id}
		if item.kind == "network" {
			args = []string{"network", "rm", item.id}
		}
		_, removeErr := docker.Output(ctx, args...)
		for _, reference := range []string{item.id, item.name} {
			_, _, exists, err := inspect(ctx, docker, item.kind, reference)
			if err != nil || exists {
				return report, errors.Join(ErrUnconfirmed, removeErr)
			}
		}
		if item.kind == "container" {
			report.RemovedContainers++
		} else {
			report.RemovedNetworks++
		}
	}
	if err := j.rotate(); err != nil {
		return report, err
	}
	return report, nil
}

// rotate atomically replaces the reconciled copy with the pending journal and
// syncs the directory, so a crash leaves either the pending journal (and the
// next reconcile repeats harmlessly) or none.
func (j *Journal) rotate() error {
	dirfd := int(j.directory.Fd())
	j.counted = false
	if unix.Renameat(dirfd, FileName, dirfd, ReconciledName) != nil {
		return ErrJournal
	}
	if j.sync("directory", j.directory) != nil {
		return ErrJournal
	}
	return nil
}

// inspect reads one object's id and labels. exists is false only for Docker's
// own not-found answer; any other failure is an error.
func inspect(ctx context.Context, docker Docker, kind, reference string) (string, map[string]string, bool, error) {
	format := "{{.Id}} {{json .Config.Labels}}"
	if kind == "network" {
		format = "{{.Id}} {{json .Labels}}"
	}
	output, err := docker.Output(ctx, kind, "inspect", "--format", format, reference)
	if err != nil {
		text := string(output)
		missing := strings.Contains(text, "No such container: "+reference) || strings.Contains(text, "No such object: "+reference)
		if kind == "network" {
			missing = strings.Contains(text, "No such network: "+reference) || strings.Contains(text, "network "+reference+" not found")
		}
		if missing {
			return "", nil, false, nil
		}
		return "", nil, false, fmt.Errorf("%w: inspect %s", ErrUnconfirmed, kind)
	}
	text := strings.TrimSpace(string(output))
	if strings.Contains(text, "\n") {
		return "", nil, false, fmt.Errorf("%w: %s name is ambiguous", ErrUnconfirmed, kind)
	}
	id, rawLabels, ok := strings.Cut(text, " ")
	var labels map[string]string
	if !ok || !objectIDPattern.MatchString(id) || json.Unmarshal([]byte(rawLabels), &labels) != nil {
		return "", nil, false, fmt.Errorf("%w: %s inspection is malformed", ErrUnconfirmed, kind)
	}
	return id, labels, true, nil
}

// Sentinel journals and creates this attempt's sentinel network: an internal
// bridge with no container, labelled as runtime-owned, that only journal
// reconciliation removes. After a SIGKILL it is always left behind, so its
// removal shows reconciliation ran from the journal.
func (j *Journal) Sentinel(ctx context.Context, docker Docker) (string, error) {
	if j == nil || ctx == nil || docker == nil {
		return "", ErrJournal
	}
	var value [8]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", ErrJournal
	}
	run := "sentinel-" + hex.EncodeToString(value[:])
	name := SentinelPrefix + hex.EncodeToString(value[:])
	if err := j.Record(run, nil, []string{name}); err != nil {
		return "", err
	}
	if _, err := docker.Output(ctx, "network", "create", "--driver", "bridge", "--internal",
		"--label", OwnerLabel+"="+run, "--label", SentinelLabel+"="+j.attempt, name); err != nil {
		return "", fmt.Errorf("%w: sentinel network was not created", ErrJournal)
	}
	return name, nil
}

// ExecDocker runs the docker executable with an explicit environment, or the
// process environment when Env is nil, and bounds its output.
type ExecDocker struct {
	Executable string
	Env        []string
}

// Output implements Docker.
func (d ExecDocker) Output(ctx context.Context, args ...string) ([]byte, error) {
	executable := d.Executable
	if executable == "" {
		executable = "docker"
	}
	command := execCommand(ctx, executable, args...)
	if d.Env != nil {
		command.Env = d.Env
	}
	var output bounded
	command.Stdout, command.Stderr = &output, &output
	err := command.Run()
	if output.overflow {
		return nil, errors.New("docker output exceeded its bound")
	}
	return output.Bytes(), err
}

type bounded struct {
	bytes.Buffer
	overflow bool
}

func (b *bounded) Write(value []byte) (int, error) {
	original := len(value)
	remaining := maxDockerOutput - b.Len()
	if len(value) > remaining {
		b.overflow = true
		value = value[:max(remaining, 0)]
	}
	_, _ = b.Buffer.Write(value)
	return original, nil
}

var execCommand = exec.CommandContext
