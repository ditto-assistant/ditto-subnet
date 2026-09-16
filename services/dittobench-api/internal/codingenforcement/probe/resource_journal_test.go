package probe

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinglaunchjournal"
)

// journalDocker keeps only networks: the fake backend creates no container.
type journalDocker struct {
	mu       sync.Mutex
	networks map[string]map[string]string
}

func (d *journalDocker) Output(_ context.Context, args ...string) ([]byte, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	id := func(name string) string { sum := sha256.Sum256([]byte(name)); return hex.EncodeToString(sum[:]) }
	find := func(reference string) string {
		for name := range d.networks {
			if name == reference || id(name) == reference {
				return name
			}
		}
		return ""
	}
	switch {
	case args[0] == "network" && args[1] == "create":
		labels := map[string]string{}
		for index, arg := range args {
			if arg == "--label" {
				key, value, _ := strings.Cut(args[index+1], "=")
				labels[key] = value
			}
		}
		d.networks[args[len(args)-1]] = labels
		return nil, nil
	case args[1] == "inspect":
		reference := args[len(args)-1]
		name := find(reference)
		if args[0] != "network" || name == "" {
			return []byte("Error: No such container: " + reference + "\nError response from daemon: network " + reference + " not found"), errors.New("exit 1")
		}
		labels, _ := json.Marshal(d.networks[name])
		return []byte(id(name) + " " + string(labels)), nil
	case args[0] == "network" && args[1] == "rm":
		delete(d.networks, find(args[2]))
		return nil, nil
	}
	return nil, errors.New("unexpected docker call")
}

func journalAgent(t *testing.T, backend *fakeBackend) (*ResourceAgent, *journalDocker, string) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "journal")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	dir, _ = filepath.EvalSymlinks(dir)
	journal, err := codinglaunchjournal.Open(dir, "native-enforcement-test", "native-enforcement-agent")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = journal.Close() })
	docker := &journalDocker{networks: map[string]map[string]string{}}
	config := agentConfig(t, backend)
	config.LaunchJournal, config.JournalDocker = journal, docker
	agent, err := NewResourceAgent(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(agent.Close)
	return agent, docker, dir
}

func TestJournaledAgentRunsOneJournaledAttemptAtATime(t *testing.T) {
	backend := &fakeBackend{release: make(chan struct{}), receipt: WorkloadReceipt{Completed: true}}
	agent, docker, dir := journalAgent(t, backend)
	first := agent.Handle(startRequest(ClassExecutorAuthoring, "hold", "--hold-ms", "10"))
	if first.Error != "" {
		t.Fatal(first.Error)
	}
	if len(backend.specs) != 1 || backend.specs[0].LaunchIntent == nil {
		t.Fatal("the launch did not go through the journal hook")
	}
	if len(docker.networks) != 1 {
		t.Fatalf("sentinel networks = %v", docker.networks)
	}
	for name, labels := range docker.networks {
		if !strings.HasPrefix(name, codinglaunchjournal.SentinelPrefix) || labels[codinglaunchjournal.OwnerLabel] == "" {
			t.Fatalf("sentinel %s %v", name, labels)
		}
	}
	if second := agent.Handle(startRequest(ClassExecutorAuthoring, "hold", "--hold-ms", "10")); second.Error == "" {
		t.Fatal("a second journaled attempt started while one was active")
	}
	body, err := os.ReadFile(filepath.Join(dir, codinglaunchjournal.FileName))
	if err != nil || strings.Count(string(body), "\n") != 1 {
		t.Fatalf("journal = %q %v", body, err)
	}
	close(backend.release)
	waited := agent.Handle(ResourceRequest{Op: "wait", Run: first.Run, TimeoutMS: 5000})
	if !waited.Done {
		t.Fatal("run did not finish")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		docker.mu.Lock()
		remaining := len(docker.networks)
		docker.mu.Unlock()
		if remaining == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the finished attempt was not reconciled")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if _, err := os.Stat(filepath.Join(dir, codinglaunchjournal.FileName)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("journal not rotated after the attempt")
	}
}

func TestJournaledAgentRefusesAnUnreconcilableJournal(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "journal")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	dir, _ = filepath.EvalSymlinks(dir)
	journal, err := codinglaunchjournal.Open(dir, "native-enforcement-test", "native-enforcement-agent")
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	docker := &journalDocker{networks: map[string]map[string]string{"ditto-job-left": {}}}
	if err := journal.Record("left", nil, []string{"ditto-job-left"}); err != nil {
		t.Fatal(err)
	}
	config := agentConfig(t, &fakeBackend{})
	config.LaunchJournal, config.JournalDocker = journal, docker
	if _, err := NewResourceAgent(t.Context(), config); err == nil {
		t.Fatal("agent accepted requests over a journal naming an unlabelled network")
	}
	if _, ok := docker.networks["ditto-job-left"]; !ok {
		t.Fatal("the unlabelled network was removed")
	}
}
