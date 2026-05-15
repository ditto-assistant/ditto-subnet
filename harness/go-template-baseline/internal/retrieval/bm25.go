// Package retrieval implements the baseline DittoRetrieval handler.
//
// On first call the handler loads the fixture manifest from
// DITTO_FIXTURES_PATH (default /fixtures), parses every pair file
// referenced by manifest.users.<user_fixture_id>.pairs, and builds a
// per-user BM25 index over the pair contents. Subsequent calls
// tokenise req.Query, rank pairs by BM25 score, and return the top
// req.K pair IDs as MinerResponse.EvidenceIDs.
//
// The validator's documented manifest shape:
//
//	{
//	  "users": {
//	    "<user_fixture_id>": {"pairs": "<relative path to pairs.jsonl>"}
//	  }
//	}
//
// Each pair file is JSONL with one record per line:
//
//	{"pair_id": "...", "content": "..."}
//
// When the manifest is missing (typical when contributors run the
// harness against the public split) the handler refuses cleanly so the
// validator can score zero for the case without crashing the run.
package retrieval

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"unicode"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// BM25Handler is the baseline DittoRetrieval handler.
type BM25Handler struct {
	once  sync.Once
	users map[string]*userIndex
	err   error
	root  string
}

type userIndex struct {
	pairs   []pair
	df      map[string]int // term -> document frequency
	avgLen  float64
	pairIdx map[string]int
}

type pair struct {
	ID     string
	Tokens []string
	TermFq map[string]int
	Len    int
}

const (
	bm25K1 = 1.5
	bm25B  = 0.75
)

// NewBM25Handler constructs a handler with no preloaded data. Indexing
// is deferred to the first Handle call so the binary still launches
// when DITTO_FIXTURES_PATH is missing during build-time smoke tests.
func NewBM25Handler() *BM25Handler {
	return &BM25Handler{}
}

// Handle ranks pair IDs by BM25 against req.Query and returns the top K.
func (h *BM25Handler) Handle(_ context.Context, req bittensor.ChallengeRequest) (bittensor.MinerResponse, error) {
	h.once.Do(h.load)
	if h.err != nil {
		// Reset so a manifest landing after the first call still gets a
		// chance on the next request.
		return bittensor.MinerResponse{Refusal: "fixtures_unavailable"}, nil
	}
	idx, ok := h.users[req.UserFixtureID]
	if !ok {
		return bittensor.MinerResponse{Refusal: "unknown_user_fixture"}, nil
	}
	k := req.K
	if k <= 0 {
		k = 10
	}
	ranked := idx.rank(tokenise(req.Query))
	if len(ranked) > k {
		ranked = ranked[:k]
	}
	return bittensor.MinerResponse{EvidenceIDs: ranked}, nil
}

func (h *BM25Handler) load() {
	root := os.Getenv("DITTO_FIXTURES_PATH")
	if root == "" {
		root = "/fixtures"
	}
	h.root = root
	manifestPath := filepath.Join(root, "manifest.json")
	buf, err := os.ReadFile(manifestPath)
	if err != nil {
		h.err = fmt.Errorf("read %s: %w", manifestPath, err)
		return
	}
	var manifest struct {
		Users map[string]struct {
			Pairs string `json:"pairs"`
		} `json:"users"`
	}
	if err := json.Unmarshal(buf, &manifest); err != nil {
		h.err = fmt.Errorf("decode %s: %w", manifestPath, err)
		return
	}
	h.users = make(map[string]*userIndex, len(manifest.Users))
	for ufid, entry := range manifest.Users {
		path := filepath.Join(root, entry.Pairs)
		idx, err := buildIndex(path)
		if err != nil {
			h.err = fmt.Errorf("index %s: %w", path, err)
			return
		}
		h.users[ufid] = idx
	}
}

func buildIndex(path string) (*userIndex, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	out := &userIndex{
		df:      map[string]int{},
		pairIdx: map[string]int{},
	}
	var totalLen int
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		var rec struct {
			PairID  string `json:"pair_id"`
			Content string `json:"content"`
		}
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			return nil, err
		}
		toks := tokenise(rec.Content)
		tf := map[string]int{}
		for _, t := range toks {
			tf[t]++
		}
		for t := range tf {
			out.df[t]++
		}
		out.pairIdx[rec.PairID] = len(out.pairs)
		out.pairs = append(out.pairs, pair{
			ID:     rec.PairID,
			Tokens: toks,
			TermFq: tf,
			Len:    len(toks),
		})
		totalLen += len(toks)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(out.pairs) > 0 {
		out.avgLen = float64(totalLen) / float64(len(out.pairs))
	}
	return out, nil
}

func (u *userIndex) rank(query []string) []string {
	if len(u.pairs) == 0 || len(query) == 0 {
		return nil
	}
	type scored struct {
		id    string
		score float64
	}
	scores := make([]scored, len(u.pairs))
	N := float64(len(u.pairs))
	for i, p := range u.pairs {
		var s float64
		for _, q := range query {
			df := u.df[q]
			if df == 0 {
				continue
			}
			idf := math.Log(1 + (N-float64(df)+0.5)/(float64(df)+0.5))
			tf := float64(p.TermFq[q])
			if tf == 0 {
				continue
			}
			norm := tf * (bm25K1 + 1)
			denom := tf + bm25K1*(1-bm25B+bm25B*float64(p.Len)/u.avgLen)
			s += idf * (norm / denom)
		}
		scores[i] = scored{id: p.ID, score: s}
	}
	// Insertion sort: indexes are small (per-user corpora are typically
	// hundreds of pairs) and avoids importing sort just for this.
	for i := 1; i < len(scores); i++ {
		for j := i; j > 0 && scores[j-1].score < scores[j].score; j-- {
			scores[j-1], scores[j] = scores[j], scores[j-1]
		}
	}
	out := make([]string, 0, len(scores))
	for _, s := range scores {
		if s.score <= 0 {
			break
		}
		out = append(out, s.id)
	}
	return out
}

// tokenise lowercases and splits on non-alphanumeric runes. It mirrors
// the normalisation used by go/bittensor.NormalisePromptForCanaryCheck
// so a paraphrase-only canary still tokenises identically to its public
// twin (which would zero the canary recall and surface the memorisation
// signal that anti-gaming relies on).
func tokenise(s string) []string {
	var out []string
	var b strings.Builder
	flush := func() {
		if b.Len() == 0 {
			return
		}
		out = append(out, b.String())
		b.Reset()
	}
	for _, r := range s {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(unicode.ToLower(r))
		default:
			flush()
		}
	}
	flush()
	return out
}
