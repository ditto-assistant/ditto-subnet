// Package rerankpool turns a DittoBench dataset into reranking pools for the
// SN118 Season 2 reranker competition (docs/reranker-season.md).
//
// A pool is one memory question plus PoolSize memories of that question's
// graph in base order, with the planted evidence marked gold. Every gold memory
// is in the pool; the rest are the hardest negatives, the non-evidence memories
// BM25 ranks highest. The pool is then ordered by BM25, so a gold memory that
// is lexically weak sits low in base order. That is the situation a product
// reranker exists for: the evidence was retrieved but mis-ranked. Gold
// comes from the generator's own record of which pairs it planted for the case
// (StagedCase.RequiredPairIDs, carried as ArtifactCase.V10EvidencePairIDs), so
// labels are exact rather than inferred from answer strings.
//
// Base order is a deterministic BM25 ranking. The product's base order is its
// composite retriever (embeddings plus recency and subject signals); matching
// that needs an embedding model and is deliberately deferred. The reranker is
// scored on how much it improves whatever base order it is given, so the
// contract holds as long as every candidate sees the same pools.
//
// Everything here is a pure function of the artifact: the same dataset bytes
// always produce the same pool bytes.
package rerankpool

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// SchemaVersion names the pool wire format consumed by dittobench-reranker.
const SchemaVersion = "dittobench-rerank-pools-v1"

// DefaultPoolSize is the candidate count the product reranks
// (ceRerankPoolSize in backend get-memories-v3.go).
const DefaultPoolSize = 20

// MaxCandidateRunes bounds candidate text. The scorer truncates each
// (query, memory) pair to 256 WordPiece tokens, and every token covers at least
// one character, so text past this bound can never reach the model. Cutting it
// only shrinks the files.
const MaxCandidateRunes = 4096

// BM25 parameters: the conventional Okapi defaults.
const (
	bm25K1 = 1.2
	bm25B  = 0.75
)

// Candidate is one memory in a pool. ID is a pool-local alias ("c00".."c19")
// so generator pair ids, which can name the family that planted them, never
// reach a scorer or a miner.
type Candidate struct {
	ID   string `json:"id"`
	Text string `json:"text"`
}

// Pool is one scored reranking problem. Candidates are in base order, best
// first. Gold lists the candidate ids that carry planted evidence.
type Pool struct {
	PoolID     string      `json:"pool_id"`
	Query      string      `json:"query"`
	Candidates []Candidate `json:"candidates"`
	Gold       []string    `json:"gold"`
}

// Stats explains how many cases became pools and why the rest did not.
type Stats struct {
	MemoryCases      int `json:"memory_cases"`
	WithEvidence     int `json:"with_evidence"`
	EmptyQuery       int `json:"empty_query"`
	MissingEvidence  int `json:"missing_evidence"`
	TooMuchEvidence  int `json:"too_much_evidence"`
	TooFewCandidates int `json:"too_few_candidates"`
	Kept             int `json:"kept"`
}

// Set is the pools derived from one dataset artifact.
type Set struct {
	Schema       string `json:"schema"`
	Seed         int64  `json:"seed"`
	BenchVersion int    `json:"bench_version"`
	SurfaceSalt  uint64 `json:"surface_salt,omitempty"`
	PoolSize     int    `json:"pool_size"`
	Stats        Stats  `json:"stats"`
	Pools        []Pool `json:"pools"`
}

// Options control pool construction.
type Options struct {
	// PoolSize is the number of candidates per pool (DefaultPoolSize if <= 0).
	PoolSize int
	// MinCandidates drops cases whose graph is too small to make ranking
	// meaningful. Defaults to PoolSize: a short pool would make NDCG easy.
	MinCandidates int
}

func (o Options) normalized() Options {
	if o.PoolSize <= 0 {
		o.PoolSize = DefaultPoolSize
	}
	if o.MinCandidates <= 0 {
		o.MinCandidates = o.PoolSize
	}
	return o
}

// Build derives reranking pools from a generated dataset artifact.
func Build(artifact gen.DatasetArtifact, opts Options) (Set, error) {
	opts = opts.normalized()
	set := Set{
		Schema:       SchemaVersion,
		Seed:         artifact.Seed,
		BenchVersion: artifact.BenchVersion,
		SurfaceSalt:  artifact.SurfaceSalt,
		PoolSize:     opts.PoolSize,
		Pools:        []Pool{},
	}
	for _, mc := range artifact.MemoryCases {
		set.Stats.MemoryCases++
		if len(mc.V10EvidencePairIDs) == 0 {
			continue
		}
		set.Stats.WithEvidence++
		query := strings.TrimSpace(mc.Question)
		if query == "" {
			set.Stats.EmptyQuery++
			continue
		}
		docs := graphPairs(seedOrder(artifact), userOf(mc.UserID), mc.RunAfterWave)
		if len(docs) < opts.MinCandidates {
			set.Stats.TooFewCandidates++
			continue
		}
		evidence := make(map[string]bool, len(mc.V10EvidencePairIDs))
		for _, id := range mc.V10EvidencePairIDs {
			evidence[id] = true
		}
		ranked, status := selectPool(rankBM25(query, docs), evidence, opts.PoolSize)
		switch status {
		case poolMissingEvidence:
			set.Stats.MissingEvidence++
			continue
		case poolTooMuchEvidence:
			set.Stats.TooMuchEvidence++
			continue
		}
		pool := Pool{
			PoolID:     poolID(artifact.Seed, artifact.BenchVersion, artifact.SurfaceSalt, mc.ID),
			Query:      query,
			Candidates: make([]Candidate, len(ranked)),
			Gold:       []string{},
		}
		for i, d := range ranked {
			alias := fmt.Sprintf("c%02d", i)
			pool.Candidates[i] = Candidate{ID: alias, Text: truncateRunes(d.text, MaxCandidateRunes)}
			if evidence[d.pairID] {
				pool.Gold = append(pool.Gold, alias)
			}
		}
		set.Pools = append(set.Pools, pool)
		set.Stats.Kept++
	}
	return set, nil
}

// Digest is the SHA-256 of the set's canonical JSON. Two parties holding the
// same artifact derive the same digest, which is what a score report pins.
func Digest(set Set) (string, error) {
	raw, err := json.Marshal(set)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// ErrNoPools is returned by callers that require at least one pool.
var ErrNoPools = errors.New("rerankpool: dataset produced no pools")

type doc struct {
	pairID string
	text   string
	order  int
}

func userOf(id string) string {
	if id == "" {
		return gen.PrimaryUser
	}
	return id
}

// seedOrder returns the seeding boundary exactly as the scorer runs it
// (dittobench-api toolPrerequisiteWave, validateV8EvidenceAvailability): tool
// prerequisite pairs are the primary user's initial world, seeded before the
// first memory wave, and memory waves then extend the same store. Many v13
// memory cases plant their evidence in those prerequisites.
func seedOrder(artifact gen.DatasetArtifact) []protocol.SeedRequest {
	prereq := protocol.SeedRequest{UserID: gen.PrimaryUser}
	for _, tc := range artifact.ToolCases {
		prereq.Pairs = append(prereq.Pairs, tc.PrerequisitePairs...)
	}
	return append([]protocol.SeedRequest{prereq}, artifact.MemoryWaves...)
}

// graphPairs returns the memories a case may be answered from: the case user's
// pairs from every wave up to and including the wave that unlocks the case.
// A repeated pair id keeps its latest content, matching harness upsert
// semantics, but keeps its first position so base-order ties stay stable.
func graphPairs(waves []protocol.SeedRequest, user string, runAfterWave int) []doc {
	index := map[string]int{}
	var docs []doc
	for _, wave := range waves {
		if userOf(wave.UserID) != user || wave.Wave > runAfterWave {
			continue
		}
		for _, p := range wave.Pairs {
			text := "User: " + p.Prompt + "\nDitto: " + p.Response
			if i, ok := index[p.PairID]; ok {
				docs[i].text = text
				continue
			}
			index[p.PairID] = len(docs)
			docs = append(docs, doc{pairID: p.PairID, text: text, order: len(docs)})
		}
	}
	return docs
}

type poolStatus int

const (
	poolOK poolStatus = iota
	poolMissingEvidence
	poolTooMuchEvidence
)

// selectPool keeps every evidence memory plus the highest-ranked non-evidence
// memories, up to size, preserving ranked order. Evidence that is not in the
// case's graph, or that would fill more than half the pool, drops the case:
// the first is a generator inconsistency and the second makes ranking trivial.
func selectPool(ranked []doc, evidence map[string]bool, size int) ([]doc, poolStatus) {
	gold := 0
	for _, d := range ranked {
		if evidence[d.pairID] {
			gold++
		}
	}
	if gold == 0 || gold < len(evidence) {
		return nil, poolMissingEvidence
	}
	if gold*2 > size {
		return nil, poolTooMuchEvidence
	}
	negatives := size - gold
	out := make([]doc, 0, size)
	for _, d := range ranked {
		if evidence[d.pairID] {
			out = append(out, d)
			continue
		}
		if negatives > 0 {
			out = append(out, d)
			negatives--
		}
	}
	return out, poolOK
}

func truncateRunes(s string, limit int) string {
	if utf8.RuneCountInString(s) <= limit {
		return s
	}
	return string([]rune(s)[:limit])
}

// rankBM25 orders docs by Okapi BM25 against query, best first. Ties keep
// insertion order, which is the seeding order of the haystack.
func rankBM25(query string, docs []doc) []doc {
	qTerms := uniqueTerms(tokenize(query))
	docTerms := make([][]string, len(docs))
	df := map[string]int{}
	total := 0
	for i, d := range docs {
		terms := tokenize(d.text)
		docTerms[i] = terms
		total += len(terms)
		seen := map[string]bool{}
		for _, t := range terms {
			if !seen[t] {
				seen[t] = true
				df[t]++
			}
		}
	}
	n := float64(len(docs))
	avgLen := 1.0
	if len(docs) > 0 && total > 0 {
		avgLen = float64(total) / n
	}
	scores := make([]float64, len(docs))
	for i, terms := range docTerms {
		tf := map[string]int{}
		for _, t := range terms {
			tf[t]++
		}
		dl := float64(len(terms))
		var s float64
		for _, q := range qTerms {
			f := float64(tf[q])
			if f == 0 {
				continue
			}
			idf := math.Log(1 + (n-float64(df[q])+0.5)/(float64(df[q])+0.5))
			s += idf * (f * (bm25K1 + 1)) / (f + bm25K1*(1-bm25B+bm25B*dl/avgLen))
		}
		scores[i] = s
	}
	ranked := append([]doc(nil), docs...)
	sort.SliceStable(ranked, func(a, b int) bool {
		sa, sb := scores[ranked[a].order], scores[ranked[b].order]
		if sa != sb {
			return sa > sb
		}
		return ranked[a].order < ranked[b].order
	})
	return ranked
}

// tokenize lowercases and splits on anything that is not a letter or digit.
func tokenize(s string) []string {
	return strings.FieldsFunc(strings.ToLower(s), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
}

func uniqueTerms(terms []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(terms))
	for _, t := range terms {
		if !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	return out
}

// poolID is a stable opaque id: a scorer can join results across validators
// without learning anything about the case it came from.
func poolID(seed int64, benchVersion int, salt uint64, caseID string) string {
	sum := sha256.Sum256(fmt.Appendf(nil, "%s|%d|%d|%d|%s", SchemaVersion, seed, benchVersion, salt, caseID))
	return hex.EncodeToString(sum[:8])
}
