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

// GradingBenchVersion is the memory grading policy every v1 pack case is
// graded under. Pinning it means a pack is judged by exactly the grader the
// agent competition's newest contract uses, and a case cannot select an older,
// looser policy.
const GradingBenchVersion = protocol.BenchVersionV13

// Bounds. They keep validation and execution cheap enough to run on every
// upload and keep one pack from monopolizing a panel run.
const (
	MaxPackBytes        = 4 << 20
	MinCases            = 8
	MaxCases            = 64
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

// GradingCase returns the MemoryCase the grader scores: the pack case with the
// pinned grading policy, regardless of what the author wrote.
func (c Case) GradingCase() protocol.MemoryCase {
	mc := c.MemoryCase
	mc.BenchVersion = GradingBenchVersion
	return mc
}
