package probe

import (
	"bytes"
	"io"
	"runtime"
	"slices"
	"strings"
	"sync"
	"testing"
)

const nonce = "0123456789abcdef"

func TestWorkloadArgumentsAreClosedPerMode(t *testing.T) {
	accepted := [][]string{
		{"hold", "--nonce", nonce, "--hold-ms", "10"},
		{"memory", "--nonce", nonce, "--hold-ms", "3000"},
		{"cpu", "--nonce", nonce, "--threads", "3", "--seconds", "8"},
		{"pids", "--nonce", nonce, "--hold-ms", "3000"},
		{"scratch", "--nonce", nonce, "--hold-ms", "3000", "--dir", "/tmp"},
		{"nofile", "--nonce", nonce},
		{"rootfs", "--nonce", nonce, "--hold-ms", "3000"},
		{"log", "--nonce", nonce, "--hold-ms", "3000", "--bytes", "65536"},
		{"hang", "--nonce", nonce, "--seconds", "600", "--setsid-child"},
	}
	for _, args := range accepted {
		options, err := ParseWorkloadArgs(args)
		if err != nil {
			t.Fatalf("%v: %v", args, err)
		}
		if rendered := WorkloadArgs(options); !slices.Equal(rendered[1:], args) || rendered[0] != WorkloadSubcommand {
			t.Fatalf("round trip %v != %v", rendered, args)
		}
	}
	refused := [][]string{
		{},
		{"shell", "--nonce", nonce},
		{"hold"},
		{"hold", "--nonce", "0123"},
		{"hold", "--nonce", nonce, "--bytes", "1"},
		{"hold", "--nonce", nonce, "--hold-ms", "120001"},
		{"cpu", "--nonce", nonce, "--threads", "2"},
		{"cpu", "--nonce", nonce, "--threads", "129", "--seconds", "1"},
		{"scratch", "--nonce", nonce, "--dir", "/workspace"},
		{"log", "--nonce", nonce, "--bytes", "67108865"},
		{"log", "--nonce", nonce},
		{"hang", "--nonce", nonce},
		{"hold", "--nonce", nonce, "extra"},
		{"memory", "--nonce", nonce, "--setsid-child"},
	}
	for _, args := range refused {
		if _, err := ParseWorkloadArgs(args); err == nil {
			t.Fatalf("accepted %v", args)
		}
	}
}

func TestLogWorkloadEmitsExactlyTheRequestedBytes(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("workloads are Linux-only")
	}
	var output bytes.Buffer
	options, err := ParseWorkloadArgs([]string{"log", "--nonce", nonce, "--bytes", "10000"})
	if err != nil {
		t.Fatal(err)
	}
	if err := RunWorkload(t.Context(), options, &output); err != nil {
		t.Fatal(err)
	}
	if output.Len() != 10000 || strings.Trim(output.String(), "x\n") != "" {
		t.Fatalf("emitted %d bytes", output.Len())
	}
}

func TestRootfsWorkloadFailsWhenTheRootIsNotReadOnly(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("workloads are Linux-only")
	}
	// On a developer or CI host the root is not a read-only filesystem, so
	// the create fails with EACCES (or succeeds as root), never EROFS.
	options, err := ParseWorkloadArgs([]string{"rootfs", "--nonce", nonce})
	if err != nil {
		t.Fatal(err)
	}
	if err := RunWorkload(t.Context(), options, io.Discard); err == nil {
		t.Fatal("a writable or permission-denied root counted as read-only")
	}
}

type pipeWriter struct {
	mu     sync.Mutex
	writer *io.PipeWriter
}

func (p *pipeWriter) Write(value []byte) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.writer.Write(value)
}

type syncBuffer struct {
	mu     sync.Mutex
	buffer bytes.Buffer
}

func (b *syncBuffer) Write(value []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.Write(value)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.String()
}

func ioPipe() (*io.PipeReader, *pipeWriter) {
	reader, writer := io.Pipe()
	return reader, &pipeWriter{writer: writer}
}
