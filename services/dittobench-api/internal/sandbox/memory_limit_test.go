package sandbox

import (
	"context"
	"errors"
	"testing"
)

func capturedRunMemory(t *testing.T, d *LocalDocker, ctx context.Context) string {
	t.Helper()
	memory := ""
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[0] {
		case "run":
			for i := 0; i+1 < len(args); i++ {
				if args[i] == "--memory" {
					memory = args[i+1]
				}
			}
			return nil, errors.New("stop after capture")
		case "rm":
			return nil, nil
		default:
			t.Fatalf("unexpected docker command: %v", args)
			return nil, nil
		}
	}
	if _, err := d.Run(ctx, "img:latest", nil); err == nil {
		t.Fatal("expected the captured run to fail")
	}
	return memory
}

func TestRunAppliesTicketMemoryLimitOverHostDefault(t *testing.T) {
	d := NewLocalDocker()
	d.MemoryLimit = "3g"
	ctx := WithMemoryLimitMiB(context.Background(), 6144)
	if got := capturedRunMemory(t, d, ctx); got != "6144m" {
		t.Fatalf("--memory = %q, want the ticket's 6144m", got)
	}
	if d.MemoryLimit != "3g" {
		t.Fatalf("per-run cap mutated the shared default to %q", d.MemoryLimit)
	}
}

func TestRunKeepsHostDefaultWithoutTicketLimit(t *testing.T) {
	d := NewLocalDocker()
	d.MemoryLimit = "3g"
	for _, ctx := range []context.Context{
		context.Background(),
		WithMemoryLimitMiB(context.Background(), 0),
	} {
		if got := capturedRunMemory(t, d, ctx); got != "3g" {
			t.Fatalf("--memory = %q, want host default 3g", got)
		}
	}
}

func TestStartupIllegalInstruction(t *testing.T) {
	cases := []struct {
		name     string
		exitCode int
		logTail  string
		want     bool
	}{
		{"supervisord respawn loop", 0, "WARN exited: dittobench_miner (terminated by SIGILL; not expected)", true},
		{"shell report", 1, "/app/start.sh: line 3:    17 Illegal instruction (core dumped) ./miner", true},
		{"direct process exit", 132, "", true},
		{"ordinary crash", 1, "panic: index out of range", false},
		{"oom kill", 137, "terminated by SIGKILL; not expected", false},
	}
	for _, tc := range cases {
		if got := StartupIllegalInstruction(tc.exitCode, tc.logTail); got != tc.want {
			t.Errorf("%s: StartupIllegalInstruction = %t, want %t", tc.name, got, tc.want)
		}
	}
}
