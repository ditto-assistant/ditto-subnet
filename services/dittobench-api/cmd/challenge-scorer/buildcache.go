package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

// buildCacheSize bounds how many built generators serve keeps: a season's
// five columns, the public default generator, and a season battery's
// candidates. A Docker build is about a minute, a cached slice column a
// single sandboxed run, so a season's binaries are built once and every
// slice of the season reuses them (dueling-bench.md section 2, "Who
// generates").
const buildCacheSize = 24

// builtGenerator is one source archive's build. A generator's own failure
// (an archive the contract refuses, a compile error) is recorded in Failure
// and cached like a success: rebuilding the same bytes fails the same way. A
// build timeout is a sandbox fault instead: every dueling generator already
// built inside the same limit to qualify, so a timeout here is host load and
// must never count against it.
type builtGenerator struct {
	Prog genrun.Program
	// TreeSHA256 is the archive's source tree digest (challenge.TreeDigest),
	// the identity a season pins.
	TreeSHA256 string
	Failure    string
}

// buildCache keeps built generators by source archive digest, least recently
// used first out. serve is sequential, but the mutex keeps the cache safe for
// any future concurrent caller.
type buildCache struct {
	sb   genrun.Sandbox
	max  int
	mu   sync.Mutex
	byID map[string]*builtGenerator
	lru  []string // least recently used first
}

func newBuildCache(sb genrun.Sandbox, max int) *buildCache {
	if max < 1 {
		max = 1
	}
	return &buildCache{sb: sb, max: max, byID: map[string]*builtGenerator{}}
}

func (c *buildCache) touch(sum string) {
	for i, id := range c.lru {
		if id == sum {
			c.lru = append(c.lru[:i], c.lru[i+1:]...)
			break
		}
	}
	c.lru = append(c.lru, sum)
}

// get returns the build of raw, whose digest the caller already checked
// against its pin. A returned error is never the generator's fault: a sandbox
// failure or a cancelled context.
func (c *buildCache) get(ctx context.Context, raw []byte) (*builtGenerator, error) {
	sum := challenge.Digest(raw)
	c.mu.Lock()
	defer c.mu.Unlock()
	if b, ok := c.byID[sum]; ok {
		c.touch(sum)
		return b, nil
	}
	b, err := c.build(ctx, raw)
	if err != nil {
		return nil, err
	}
	c.byID[sum] = b
	c.touch(sum)
	for len(c.lru) > c.max {
		old := c.lru[0]
		c.lru = c.lru[1:]
		if e := c.byID[old]; e != nil && e.Prog != nil {
			_ = e.Prog.Close()
		}
		delete(c.byID, old)
	}
	return b, nil
}

func (c *buildCache) build(ctx context.Context, raw []byte) (*builtGenerator, error) {
	dir, err := os.MkdirTemp("", "challenge-scorer-src-*")
	if err != nil {
		return nil, fmt.Errorf("%w: %v", genrun.ErrSandbox, err)
	}
	defer os.RemoveAll(dir)
	// The Docker sandbox bind-mounts this directory read-only for an
	// unprivileged container user, which cannot traverse MkdirTemp's 0700.
	if err := os.Chmod(dir, 0o755); err != nil {
		return nil, fmt.Errorf("%w: %v", genrun.ErrSandbox, err)
	}
	info, err := challenge.ExtractArchive(raw, dir)
	if err != nil {
		return &builtGenerator{Failure: "archive: " + err.Error()}, nil
	}
	prog, err := c.sb.Build(ctx, dir)
	if err != nil {
		var gerr *genrun.GeneratorError
		if errors.As(err, &gerr) && gerr.Reason == "timeout" {
			return nil, fmt.Errorf("%w: generator build timed out (host load)", genrun.ErrSandbox)
		}
		if errors.As(err, &gerr) {
			return &builtGenerator{TreeSHA256: info.TreeSHA256, Failure: gerr.Error()}, nil
		}
		return nil, err
	}
	return &builtGenerator{Prog: prog, TreeSHA256: info.TreeSHA256}, nil
}

// Close releases every cached build.
func (c *buildCache) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, b := range c.byID {
		if b.Prog != nil {
			_ = b.Prog.Close()
		}
	}
	c.byID, c.lru = map[string]*builtGenerator{}, nil
}

// generateValidated runs one request on a built generator and validates the
// pack against it. failure is the generator's own failure (crash, timeout,
// invalid pack); a returned error is the sandbox's or a cancelled context.
func generateValidated(ctx context.Context, prog genrun.Program, req challenge.GenerateRequest) (raw []byte, failure string, err error) {
	raw, err = prog.Generate(ctx, req)
	if err != nil {
		var gerr *genrun.GeneratorError
		if errors.As(err, &gerr) {
			return nil, gerr.Error(), nil
		}
		return nil, "", err
	}
	if r := challenge.ValidateGenerated(raw, req); !r.Valid {
		first := ""
		if len(r.Issues) > 0 {
			first = fmt.Sprintf(", first %s: %s", r.Issues[0].Rule, r.Issues[0].Message)
		}
		return nil, fmt.Sprintf("invalid pack (%d issues%s)", len(r.Issues), first), nil
	}
	return raw, "", nil
}
