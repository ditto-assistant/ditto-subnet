package challenge

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"strconv"
)

// GeneratorContractV1 is the generator program contract.
//
// A v1 generator is a Go main module. The scorer builds it offline with a
// pinned toolchain (CGO disabled, no module proxy, vendored dependencies
// only) and runs the binary with no network, a read-only filesystem, and
// bounded CPU, memory, and wall time. The binary reads one GenerateRequest
// JSON object from stdin, writes one pack JSON object to stdout, and exits 0.
// Stderr is diagnostic only. The same request must always produce the same
// stdout bytes: generation is a pure function of the request, so no clock,
// no randomness except the request seed, and no model.
const GeneratorContractV1 = "dittobench-generator/v1"

// Generator program limits. Wall time and memory are the scorer's sandbox
// limits; GenerateRequest.Cases is bounded by the pack contract.
const (
	// GenerateTimeoutSeconds bounds one generation run.
	GenerateTimeoutSeconds = 60
	// GenerateMemoryBytes bounds one generation run's memory.
	GenerateMemoryBytes = 1 << 30
)

// GenerateRequest is the only input a generator receives.
type GenerateRequest struct {
	Contract string `json:"contract"`
	// Seed is 32 bytes of scorer-chosen randomness, as 64 lowercase hex. The
	// generator must derive every random choice from it.
	Seed string `json:"seed"`
	// MinerHotkey is the submitting hotkey; the pack must echo it.
	MinerHotkey string `json:"miner_hotkey"`
	// Cases is the exact number of cases the pack must contain.
	Cases int `json:"cases"`
	// GradingBenchVersion is the baseline grading policy: the version a
	// generator that knows only one policy emits (GradingBenchVersion, 13).
	GradingBenchVersion int `json:"grading_bench_version"`
	// GradingBenchVersions, when present, lists every grading policy the
	// scorer accepts for this request, ascending, starting at
	// GradingBenchVersion. A version-aware generator declares the newest one
	// it supports in the pack's grading_bench_version (NewestAccepted). It is
	// absent when only the baseline is accepted (every v13 round), so a v13
	// request is byte-identical to the original contract, and a generator
	// built before the field existed ignores it and emits the baseline.
	GradingBenchVersions []int `json:"grading_bench_versions,omitempty"`
}

// NewGenerateRequest builds a v1 request that accepts only the baseline
// grading policy: exactly the original v13 request.
func NewGenerateRequest(seed, minerHotkey string, cases int) GenerateRequest {
	return GenerateRequest{
		Contract:            GeneratorContractV1,
		Seed:                seed,
		MinerHotkey:         minerHotkey,
		Cases:               cases,
		GradingBenchVersion: GradingBenchVersion,
	}
}

// NewGenerateRequestFor builds the request for a round (or a qualification)
// frozen at roundBenchVersion: it accepts the supported grading policies in the
// round's window (GradingBenchVersionsThrough). Zero, or a version that accepts only the baseline,
// yields NewGenerateRequest exactly.
//
// The request's grading_bench_version is the oldest policy the round accepts.
// While the window still holds the baseline that is GradingBenchVersion, the
// value every generator built before v14 requires; once the window moves past
// it (a round two versions above the baseline), the request floor moves with
// it and a baseline-only generator refuses the request, which is exactly the
// policy's retirement for that round.
func NewGenerateRequestFor(seed, minerHotkey string, cases, roundBenchVersion int) GenerateRequest {
	req := NewGenerateRequest(seed, minerHotkey, cases)
	accepted := GradingBenchVersionsThrough(roundBenchVersion)
	if len(accepted) == 0 || (len(accepted) == 1 && accepted[0] == GradingBenchVersion) {
		return req
	}
	req.GradingBenchVersion = accepted[0]
	if len(accepted) > 1 {
		req.GradingBenchVersions = accepted
	}
	return req
}

// AcceptsBaseline reports whether the request accepts the baseline policy
// (GradingBenchVersion), the only one a legacy retry can request.
func (r GenerateRequest) AcceptsBaseline() bool {
	return r.Accepts(GradingBenchVersion)
}

// Accepted returns the grading policies this request accepts, ascending.
func (r GenerateRequest) Accepted() []int {
	if len(r.GradingBenchVersions) == 0 {
		return []int{r.GradingBenchVersion}
	}
	return append([]int(nil), r.GradingBenchVersions...)
}

// Accepts reports whether a pack graded at v satisfies this request.
func (r GenerateRequest) Accepts(v int) bool {
	for _, a := range r.Accepted() {
		if a == v {
			return true
		}
	}
	return false
}

// NewestAccepted returns the newest grading policy the request accepts that
// the generator also supports (supported lists the policies its families are
// written for; nil means every policy this package supports). It falls back
// to the baseline, which every request accepts.
func (r GenerateRequest) NewestAccepted(supported []int) int {
	if supported == nil {
		supported = supportedGradingBenchVersions
	}
	best := r.GradingBenchVersion
	for _, a := range r.Accepted() {
		for _, s := range supported {
			if a == s && a > best {
				best = a
			}
		}
	}
	return best
}

// Validate rejects a malformed request.
func (r GenerateRequest) Validate() error {
	switch {
	case r.Contract != GeneratorContractV1:
		return fmt.Errorf("request contract must be %q", GeneratorContractV1)
	case !shaPattern.MatchString(r.Seed):
		return errors.New("request seed must be 64 lowercase hex characters")
	case !hotkeyPattern.MatchString(r.MinerHotkey):
		return errors.New("request miner_hotkey must be an SS58 hotkey")
	case r.Cases < MinCases || r.Cases > MaxCases:
		return fmt.Errorf("request cases must be in %d..%d", MinCases, MaxCases)
	case r.GradingBenchVersion < GradingBenchVersion:
		return fmt.Errorf("request grading_bench_version must be at least %d", GradingBenchVersion)
	}
	if len(r.GradingBenchVersions) > 0 {
		if r.GradingBenchVersions[0] != r.GradingBenchVersion {
			return errors.New("request grading_bench_versions must start at grading_bench_version")
		}
		for i, v := range r.GradingBenchVersions {
			if i > 0 && v <= r.GradingBenchVersions[i-1] {
				return errors.New("request grading_bench_versions must be strictly ascending")
			}
		}
	}
	// Versions this package does not know (a newer scorer offering a policy
	// released after this generator was built) are ignored, not refused:
	// every component matches versions, so a generator keeps working across
	// an upgrade while the request still accepts a policy it supports. A
	// request accepting none of them (the window moved past this build) is
	// refused: rebuild against the current package.
	for _, v := range r.Accepted() {
		if SupportsGradingBenchVersion(v) {
			return nil
		}
	}
	return fmt.Errorf("request accepts grading policies %v, none of which this package supports %v; rebuild against the current dittobench-datagen", r.Accepted(), supportedGradingBenchVersions)
}

// Bytes is the request as the generator reads it on stdin.
func (r GenerateRequest) Bytes() []byte {
	b, _ := json.Marshal(r)
	return append(b, '\n')
}

// Rand returns a math/rand source derived from the request seed. Generators
// may use it or derive their own randomness from Seed; either way, the pack
// must be a pure function of the request.
func (r GenerateRequest) Rand() *rand.Rand {
	return rand.New(rand.NewSource(r.SeedInt64()))
}

// SeedInt64 folds the 32-byte seed into an int64 for math/rand.
func (r GenerateRequest) SeedInt64() int64 {
	raw, err := hex.DecodeString(r.Seed)
	if err != nil || len(raw) != 32 {
		return 0
	}
	var x uint64
	for i := 0; i < 32; i += 8 {
		x ^= binary.BigEndian.Uint64(raw[i : i+8])
	}
	return int64(x)
}

// SeedFromInt64 derives a v1 seed from a small integer, for local rehearsal,
// fixtures, and tests. Production seeds are fresh random bytes.
func SeedFromInt64(n int64) string {
	sum := sha256.Sum256([]byte("dittobench-generator-seed:" + strconv.FormatInt(n, 10)))
	return hex.EncodeToString(sum[:])
}

// ValidateGenerated validates generator output for a request: every pack rule
// plus the binding rules (contract-checked seed, hotkey, and exact case count)
// that tie the output to this request rather than to one the generator chose.
func ValidateGenerated(raw []byte, req GenerateRequest) Report {
	r := ValidateBytes(raw)
	if err := req.Validate(); err != nil {
		r.Issues = append(r.Issues, Issue{Rule: "binding.request", Message: err.Error()})
		r.Valid = false
		return r
	}
	p, err := Decode(raw)
	if err != nil {
		return r
	}
	var extra []Issue
	if p.Generator.Seed != req.Seed {
		extra = append(extra, Issue{Rule: "binding.seed", Message: "generator.seed must equal the request seed"})
	}
	if p.MinerHotkey != req.MinerHotkey {
		extra = append(extra, Issue{Rule: "binding.miner_hotkey", Message: "miner_hotkey must equal the request miner_hotkey"})
	}
	if !req.Accepts(p.GradingBenchVersion) {
		extra = append(extra, Issue{Rule: "binding.grading_bench_version", Message: fmt.Sprintf("grading_bench_version %d is not one the request accepts %v", p.GradingBenchVersion, req.Accepted())})
	}
	if len(p.Cases) != req.Cases {
		extra = append(extra, Issue{Rule: "binding.cases", Message: fmt.Sprintf("pack must have exactly %d cases (the request), got %d", req.Cases, len(p.Cases))})
	}
	if len(extra) > 0 {
		r.Issues = append(r.Issues, extra...)
		r.Valid = false
	}
	return r
}
