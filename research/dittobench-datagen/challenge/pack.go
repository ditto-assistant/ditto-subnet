// Package challenge defines the DittoBench generator competition contract: the
// counterbalance to the agent competition.
//
// Agent miners are paid for harnesses that answer DittoBench cases. Generator
// miners are paid for *cases*: a generator runs offline on the miner's machine
// and emits a challenge pack (a seeded memory haystack plus questions with
// deterministic gold answers). Platform holds the pack private, a trusted
// scorer runs it against a panel of current top agents, and the pack earns
// credit for cases that strong agents split on: solved by at least two
// independent owners, failed by the rest, and preferably failed by the
// champion.
//
// Everything a pack is judged on before any agent sees it lives here and is a
// pure function of the pack bytes:
//
//   - Decode bounds the input and rejects unknown structure.
//   - Validate checks schema, evidence grounding, answer leakage, and grader
//     self-consistency (the gold answer passes the real memory grader; empty,
//     abstaining, echoing, distractor, and whole-memory-dump answers do not).
//   - Score turns a panel verdict matrix into a discrimination score.
//
// Packs carry data, never code. A generator is the miner's private program;
// only its inert output crosses the trust boundary, so no screener sandbox is
// needed and every validator rule is reproducible by the miner locally with
// `go run ./cmd/challenge validate`.
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

// Generator identifies the program that produced a pack. It is provenance the
// miner asserts; nothing about scoring trusts it.
type Generator struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	// Seed is the generator seed the miner used, so the miner can reproduce the
	// exact pack from their own program.
	Seed int64 `json:"seed"`
	// SourceSHA256 optionally commits to the generator source the miner ran.
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

// Pack is a generator miner's submission.
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
// unknown fields: a pack is an authored artifact, so a misspelled field is far
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

// Digest is a pack's identity: the hex SHA-256 of the exact bytes the miner
// uploaded. It is what the miner signs, what Platform stores and pins, and
// what the scorer re-checks before running a panel. Hashing the raw bytes (not
// a re-encoding) keeps the identity reproducible in any language.
func Digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// Fingerprint is a case's content identity across packs: its normalized
// question and gold answer. Platform uses it to give the first submitter of a
// case the credit and to zero later copies, regardless of IDs or haystack.
func Fingerprint(c Case) string {
	h := sha256.New()
	h.Write([]byte(grade.Normalize(c.Question)))
	h.Write([]byte{0})
	h.Write([]byte(grade.Normalize(c.ExpectedAnswer)))
	for _, item := range c.AnswerItems {
		h.Write([]byte{0})
		h.Write([]byte(grade.Normalize(item)))
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
