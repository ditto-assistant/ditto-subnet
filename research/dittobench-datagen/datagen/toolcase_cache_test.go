package datagen

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"sort"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Many v13 assertions read the same (seed, size) tool-case run. Each run is
// generated once per test binary and shared; callers must not mutate it.
// TestMain re-hashes every cached run afterwards and fails the binary if a
// test changed one.

type toolCaseKey struct {
	seed int64
	n    int
}

type toolCaseEntry struct {
	once  sync.Once
	cases []protocol.ToolCase
	sum   [32]byte
	err   error
}

var toolCaseCache sync.Map // toolCaseKey -> *toolCaseEntry

func cachedV13Cases(seed int64, n int) ([]protocol.ToolCase, error) {
	v, _ := toolCaseCache.LoadOrStore(toolCaseKey{seed, n}, &toolCaseEntry{})
	e := v.(*toolCaseEntry)
	e.once.Do(func() {
		rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV13)
		if err != nil {
			e.err = err
			return
		}
		e.cases, _ = GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, n, protocol.BenchVersionV13)
		e.sum, e.err = toolCasesSum(e.cases)
	})
	return e.cases, e.err
}

// freshV13Cases generates an unshared run for a test that edits it in place.
func freshV13Cases(t *testing.T, seed int64, n int) []protocol.ToolCase {
	t.Helper()
	rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, n, protocol.BenchVersionV13)
	return cases
}

func toolCasesSum(cases []protocol.ToolCase) ([32]byte, error) {
	raw, err := json.Marshal(cases)
	if err != nil {
		return [32]byte{}, err
	}
	return sha256.Sum256(raw), nil
}

func TestMain(m *testing.M) {
	code := m.Run()
	if mutated := mutatedCachedV13Cases(); len(mutated) > 0 {
		fmt.Fprintf(os.Stderr, "FAIL: tests mutated shared cached v13 tool cases: %v\n", mutated)
		code = 1
	}
	os.Exit(code)
}

// mutatedCachedV13Cases re-hashes every cached run and names any that no
// longer matches the hash taken when it was generated.
func mutatedCachedV13Cases() []string {
	var mutated []string
	toolCaseCache.Range(func(key, value any) bool {
		k, e := key.(toolCaseKey), value.(*toolCaseEntry)
		if e.err != nil {
			return true
		}
		if sum, err := toolCasesSum(e.cases); err != nil || sum != e.sum {
			mutated = append(mutated, fmt.Sprintf("seed %d n %d", k.seed, k.n))
		}
		return true
	})
	sort.Strings(mutated)
	return mutated
}
