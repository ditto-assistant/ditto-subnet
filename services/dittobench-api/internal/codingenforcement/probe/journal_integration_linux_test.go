//go:build native_probe_integration

package probe

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinglaunchjournal"
)

// The hosted runtime's launch journal against a real Docker daemon (selected
// by DOCKER_HOST): a child process journals and creates a sentinel network and
// a labelled container, is killed with SIGKILL, and a new owner reconciles.
// An unjournaled container carrying the ownership label must survive. The
// image is DITTOBENCH_NATIVE_PROBE_REPOSITORY@DITTOBENCH_NATIVE_PROBE_IMAGE_DIGEST,
// only created, never started. Every object has a random unique name and is
// removed by the test.
func TestLaunchJournalReconcilesAKilledOwnerOnRealDocker(t *testing.T) {
	if os.Getenv("DITTOBENCH_JOURNAL_CHILD") == "1" {
		journalChild()
		return
	}
	repository := os.Getenv("DITTOBENCH_NATIVE_PROBE_REPOSITORY")
	digest := os.Getenv("DITTOBENCH_NATIVE_PROBE_IMAGE_DIGEST")
	if repository == "" || digest == "" {
		t.Fatal("DITTOBENCH_NATIVE_PROBE_REPOSITORY and DITTOBENCH_NATIVE_PROBE_IMAGE_DIGEST are required")
	}
	root, err := os.MkdirTemp("", "launch-journal-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	dir := filepath.Join(root, "journal")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Minute)
	defer cancel()
	docker := codinglaunchjournal.ExecDocker{}
	suffix := randomSuffix(t)
	decoy := "dittobench-journal-decoy-" + suffix
	t.Cleanup(func() { _, _ = docker.Output(context.Background(), "container", "rm", "--force", decoy) })
	if output, err := docker.Output(ctx, "container", "create", "--name", decoy, "--network", "none",
		"--label", codinglaunchjournal.OwnerLabel+"=decoy-"+suffix, repository+"@"+digest, "/absent"); err != nil {
		t.Fatalf("create decoy: %s %v", output, err)
	}

	child := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestLaunchJournalReconcilesAKilledOwnerOnRealDocker$")
	child.Env = append(os.Environ(), "DITTOBENCH_JOURNAL_CHILD=1", "DITTOBENCH_JOURNAL_DIR="+dir, "DITTOBENCH_JOURNAL_SUFFIX="+suffix,
		"DITTOBENCH_JOURNAL_IMAGE="+repository+"@"+digest)
	stdout, err := child.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	var sentinel string
	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		if name, ok := strings.CutPrefix(scanner.Text(), "ready "); ok {
			sentinel = name
			break
		}
	}
	container := "dittobench-journal-" + suffix
	t.Cleanup(func() {
		_, _ = docker.Output(context.Background(), "container", "rm", "--force", container)
		if sentinel != "" {
			_, _ = docker.Output(context.Background(), "network", "rm", sentinel)
		}
	})
	if sentinel == "" {
		_ = child.Process.Kill()
		_ = child.Wait()
		t.Fatal("child never became ready")
	}
	if err := child.Process.Signal(syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	_ = child.Wait()

	exists := func(kind, name string) bool {
		_, err := docker.Output(ctx, kind, "inspect", "--format", "{{.Id}}", name)
		return err == nil
	}
	if !exists("container", container) || !exists("network", sentinel) {
		t.Fatal("SIGKILL left no journaled leftovers")
	}
	body, err := os.ReadFile(filepath.Join(dir, codinglaunchjournal.FileName))
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(strings.TrimSuffix(string(body), "\n"), "\n") {
		if _, err := codinglaunchjournal.DecodeEntry([]byte(line)); err != nil {
			t.Fatalf("journal line is not an ids-only entry: %q", line)
		}
	}
	journal, err := codinglaunchjournal.Open(dir, "reconciler", "reconciler")
	if err != nil {
		t.Fatalf("the killed owner's lock was not released: %v", err)
	}
	defer journal.Close()
	report, err := journal.Reconcile(ctx, docker)
	if err != nil || report.RemovedContainers != 1 || report.RemovedNetworks != 1 {
		t.Fatalf("report = %+v %v", report, err)
	}
	if exists("container", container) || exists("network", sentinel) {
		t.Fatal("journaled objects survived reconciliation")
	}
	if !exists("container", decoy) {
		t.Fatal("reconciliation removed an unjournaled container")
	}
	if again, err := journal.Reconcile(ctx, docker); err != nil || again != (codinglaunchjournal.Report{}) {
		t.Fatalf("second reconcile = %+v %v", again, err)
	}
}

func journalChild() {
	dir, suffix, image := os.Getenv("DITTOBENCH_JOURNAL_DIR"), os.Getenv("DITTOBENCH_JOURNAL_SUFFIX"), os.Getenv("DITTOBENCH_JOURNAL_IMAGE")
	journal, err := codinglaunchjournal.Open(dir, "journal-child", "journal-child")
	if err != nil {
		os.Exit(2)
	}
	docker := codinglaunchjournal.ExecDocker{}
	ctx := context.Background()
	sentinel, err := journal.Sentinel(ctx, docker)
	if err != nil {
		os.Exit(3)
	}
	run := "journal-" + suffix
	name := "dittobench-" + run
	if journal.Record(run, []string{name}, nil) != nil {
		os.Exit(4)
	}
	if _, err := docker.Output(ctx, "container", "create", "--name", name, "--network", "none", "--label", codinglaunchjournal.OwnerLabel+"="+run, image, "/absent"); err != nil {
		os.Exit(5)
	}
	fmt.Println("ready " + sentinel)
	time.Sleep(10 * time.Minute)
	os.Exit(6)
}

func randomSuffix(t *testing.T) string {
	t.Helper()
	var value [6]byte
	if _, err := rand.Read(value[:]); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(value[:])
}
