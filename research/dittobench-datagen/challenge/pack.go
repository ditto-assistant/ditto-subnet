// Package challenge defines the DittoBench generator competition contract: the
// counterbalance to the agent competition.
//
// Agent miners are paid for harnesses that answer DittoBench cases. Generator
// miners are paid for *generators*: CPU-only programs that turn a seed into a
// challenge pack (a seeded memory haystack plus questions with deterministic
// gold answers). A miner submits generator source; Platform keeps it private,
// and a trusted scorer builds it offline, runs it in a no-network sandbox on
// fresh secret seeds, and runs the resulting packs against a panel of current
// top agents. A generator earns credit for cases strong agents split on:
// solved by at least two independent owners, failed by the rest, and
// preferably failed by the champion.
//
// The contract is two layers, both pure functions of their inputs:
//
//   - The generator contract (generator.go): the source archive rules, the
//     GenerateRequest a generator reads on stdin, and the binding checks that
//     tie an emitted pack to its request.
//   - The pack contract (this file, validate.go, score.go): Decode bounds the
//     output and rejects unknown structure; Validate checks schema, evidence
//     grounding, answer leakage, and grader self-consistency (the gold answer
//     passes the real memory grader; empty, abstaining, echoing, distractor,
//     and whole-memory-dump answers do not); ScoreCases and ScoreRun turn panel
//     verdicts into a discrimination score.
//
// No model is involved anywhere: generation is CPU compute, validation is
// deterministic, and every rule is reproducible by the miner locally with
// `go run ./cmd/challenge rehearse -dir <generator>`.
package challenge

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// ContractV1 is the only pack contract this package accepts.
const ContractV1 = "dittobench-challenge-pack/v1"

// GradingBenchVersion is the baseline memory grading policy: the version a
// v1 request always carries in grading_bench_version, and the version every
// generator built before v14 packs emit. A v13 round accepts only it, so v13
// requests, packs, and grading are byte-identical to the original contract.
//
// A pack declares its own policy in grading_bench_version, from
// SupportedGradingBenchVersions, and every case in it is graded under exactly
// that policy: a case cannot select an older, looser one.
const GradingBenchVersion = protocol.BenchVersionV13

// GradingBenchVersionV14 is the v14 memory grading policy (rendered reply
// text, number punctuation bounds, percent claims; grade.gradingPolicy). A v14
// pack is also validated against the v14 answer-text rule: every graded
// answer, distractor, and dump-guard form must read the same as rendered text.
const GradingBenchVersionV14 = protocol.BenchVersionV14

// supportedGradingBenchVersions is the ordered set of policies a pack may
// declare. It is also the set of bench versions a generator round can run at
// (Platform's GENERATOR_ROUND_BENCH_VERSIONS; the pin test diffs them): a
// round at version N accepts packs graded at any supported version <= N.
var supportedGradingBenchVersions = []int{GradingBenchVersion, GradingBenchVersionV14}

// SupportedGradingBenchVersions returns a copy of the ordered grading policies
// a pack may declare.
func SupportedGradingBenchVersions() []int {
	return append([]int(nil), supportedGradingBenchVersions...)
}

// SupportsGradingBenchVersion reports whether a pack may declare v.
func SupportsGradingBenchVersion(v int) bool {
	for _, s := range supportedGradingBenchVersions {
		if s == v {
			return true
		}
	}
	return false
}

// GradingBenchVersionsThrough returns the grading policies a round frozen at
// roundBenchVersion accepts: every supported version at or below it, oldest
// first. It is empty when roundBenchVersion predates every supported policy.
func GradingBenchVersionsThrough(roundBenchVersion int) []int {
	var out []int
	for _, s := range supportedGradingBenchVersions {
		if s <= roundBenchVersion {
			out = append(out, s)
		}
	}
	return out
}

// Bounds. They keep validation and execution cheap enough to run on every
// upload and keep one pack from monopolizing a panel run.
//
// MaxCases is the most cases one pack (one round column or qualification
// seed) may hold. 80 lets Platform's cases_per_seed reach ~70 per column, so
// five counted columns give a round about the 350 memory cases a canonical
// bench v13 run has. Raising it is not free for miners: a generator vendors
// this package, so one built against an older bound refuses a larger request
// in GenerateRequest.Validate until it is re-vendored and re-uploaded.
const (
	MaxPackBytes        = 4 << 20
	MinCases            = 8
	MaxCases            = 80
	MaxWaves            = 8
	MaxUsers            = 4
	MaxPairs            = 2000
	MaxPairTextRunes    = 4000
	MaxQuestionRunes    = 1000
	MaxAnswerRunes      = 400
	MaxAlternatives     = 8
	MaxDistractors      = 8
	MaxListItems        = 8
	MaxEvidencePairs    = 8
	MaxGeneratorNameLen = 64
)

var (
	idPattern     = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,63}$`)
	userIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,31}$`)
	hotkeyPattern = regexp.MustCompile(`^[1-9A-HJ-NP-Za-km-z]{47,48}$`)
	shaPattern    = regexp.MustCompile(`^[0-9a-f]{64}$`)
	labelPattern  = regexp.MustCompile(`^[a-z0-9][a-z0-9_.:-]{0,63}$`)
)

// Generator identifies the program that produced a pack. Name and version are
// provenance the generator asserts; nothing about scoring trusts them.
type Generator struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	// Seed echoes the GenerateRequest seed (64 lowercase hex) the pack was
	// generated from. ValidateGenerated requires it to equal the request.
	Seed string `json:"seed"`
	// SourceSHA256 optionally commits to the generator source that ran.
	SourceSHA256 string `json:"source_sha256,omitempty"`
}

// Case is one challenge question. It reuses the agent competition's
// MemoryCase so a pack case is graded by the unchanged production grader.
// Grader-only MemoryCase fields tagged `json:"-"` (typed claims, twins) are
// deliberately unavailable to packs: v1 cases are graded by answer kind.
type Case struct {
	protocol.MemoryCase
	// UserID selects the memory graph the case is answered under; it must name
	// a wave's user. Empty is the default user.
	UserID string `json:"user_id,omitempty"`
	// RunAfterWave is the last wave that must be seeded before this case runs.
	RunAfterWave int `json:"run_after_wave,omitempty"`
	// EvidencePairIDs names the seeded pairs that ground the gold answer. The
	// validator requires every accepted answer item to appear in this text, so
	// a case cannot reward knowledge the haystack never supplied.
	EvidencePairIDs []string `json:"evidence_pair_ids"`
}

// Pack is one generator output: everything a panel run needs for one seed.
type Pack struct {
	Contract            string                 `json:"contract"`
	GradingBenchVersion int                    `json:"grading_bench_version"`
	MinerHotkey         string                 `json:"miner_hotkey"`
	Generator           Generator              `json:"generator"`
	MemoryWaves         []protocol.SeedRequest `json:"memory_waves"`
	Cases               []Case                 `json:"cases"`
}

// ErrTooLarge is returned when a pack exceeds MaxPackBytes.
var ErrTooLarge = errors.New("challenge pack exceeds the size limit")

// Decode parses pack bytes. It rejects oversize input, trailing data, and
// unknown fields: a pack is a generated artifact, so a misspelled field is far
// more likely a generator bug than forward-compatible metadata, and silently
// dropping it would grade a case differently from what its author intended.
func Decode(raw []byte) (Pack, error) {
	if len(raw) > MaxPackBytes {
		return Pack{}, ErrTooLarge
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var p Pack
	if err := dec.Decode(&p); err != nil {
		return Pack{}, fmt.Errorf("decode challenge pack: %w", err)
	}
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return Pack{}, errors.New("decode challenge pack: trailing data after the pack object")
	}
	return p, nil
}

// ValidHotkey reports whether s has the shape of an SS58 hotkey, the form every
// pack and generate request binds.
func ValidHotkey(s string) bool { return hotkeyPattern.MatchString(s) }

// Digest is the hex SHA-256 of exact bytes: a pack's identity (what the
// determinism check compares) and a source archive's identity (what the miner
// signs and Platform pins). Hashing the raw bytes, not a re-encoding, keeps it
// reproducible in any language.
func Digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// Fingerprint is a case's content identity across packs: its normalized
// question and gold answer. Platform uses it to give the first generator that
// emitted a case the credit and to zero later repeats, regardless of IDs or
// haystack, and to compare generators' outputs on a shared probe seed.
//
// Grading settings (answer kind, alternatives, distractors, guards, typed-list
// options) are deliberately excluded. They change how an answer is graded,
// not what the agent is asked, so flipping one never turns a repeated
// question and answer into a new case that earns credit or escapes the copy
// check.
func Fingerprint(c Case) string {
	h := sha256.New()
	// Each field is length-prefixed, so no field content (an embedded NUL
	// included) can shift text from one field into the next.
	field := func(s string) {
		b := []byte(grade.Normalize(s))
		fmt.Fprintf(h, "%d:", len(b))
		h.Write(b)
	}
	field(c.Question)
	field(c.ExpectedAnswer)
	items := c.AnswerItems
	if c.AnswerKind == protocol.AnswerList {
		// An unordered list is graded as a set, so item order is not part of
		// the case: reordering must not make a repeated case look new.
		items = make([]string, len(c.AnswerItems))
		for i, item := range c.AnswerItems {
			items[i] = grade.Normalize(item)
		}
		sort.Strings(items)
	}
	for _, item := range items {
		field(item)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// GradingCase returns the MemoryCase the grader scores under the baseline
// policy (GradingBenchVersion). Generators built before v14 packs call it; a
// version-aware caller grades a pack's case with GradingCaseAt or
// Pack.GradingCase.
func (c Case) GradingCase() protocol.MemoryCase {
	return c.GradingCaseAt(GradingBenchVersion)
}

// GradingCaseAt returns the MemoryCase the grader scores under policy v,
// regardless of the bench_version the author wrote on the case.
func (c Case) GradingCaseAt(v int) protocol.MemoryCase {
	mc := c.MemoryCase
	mc.BenchVersion = v
	return mc
}

// GradingCase returns case i as the grader scores it: under the policy the
// pack declares.
func (p Pack) GradingCase(i int) protocol.MemoryCase {
	return p.Cases[i].GradingCaseAt(p.GradingBenchVersion)
}
