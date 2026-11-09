package challenge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Frontier oracle.
//
// A case that no two independent panel owners solve is "unsolved" and, on its
// own, indistinguishable from a broken one. Solving proves validity, but
// failing does not prove invalidity: a valid case can simply be beyond every
// current agent. The frontier oracle is the second witness. It is a
// subnet-run reference solver that answers the case from a small, fixed-size
// context derived from the case's own answer key. If it passes the production
// grader, the case is attested valid and scores as frontier (ScoreCases).
//
// The context cannot be the whole visible memory: a pack may seed 2,000 pairs
// of up to 4,000 characters each, far beyond any prompt. It also cannot be
// the cited evidence alone: the generator writes the answer key, so a case
// citing a decoy (or a superseded fact) as its evidence, with a gold answer to
// match, would be "answered" by repeating the planted fact. OracleContext
// therefore mixes the cited evidence with the visible pairs most likely to
// contradict it, and hides which is which:
//
//   - every evidence pair the case cites (at most MaxEvidencePairs);
//   - the visible pairs (same user, seeded at or before run_after_wave) that
//     state a declared distractor, forbidden answer, or dump-guard value: the
//     facts the generator itself says compete with the gold;
//   - the visible pairs ranked highest by BM25 against the question and
//     evidence text, until OracleCompetitorPairs competitors are chosen;
//   - all of them in chronological order with timestamps, and no pair id,
//     session id, or evidence marker.
//
// The oracle must still pick the right fact among its closest competitors,
// which is what makes a pass a uniqueness witness rather than a grounding
// check (Validate already proves the gold appears in the evidence). A
// contradicting fact phrased too differently to rank is the residual risk the
// sampled operator audit (FrontierAuditSample) covers.
const (
	// OracleContractV1 names the oracle context schema.
	OracleContractV1 = "dittobench-frontier-oracle/v1"
	// OracleCompetitorPairs is how many non-evidence pairs the context holds
	// at most, so a context is at most MaxEvidencePairs + OracleCompetitorPairs
	// pairs (about 32 x 8,000 characters in the worst case).
	OracleCompetitorPairs = 24
	// OracleMaxPairs bounds one oracle context.
	OracleMaxPairs = MaxEvidencePairs + OracleCompetitorPairs

	frontierAuditDomain = "dittobench-frontier-audit/v1"
	bm25K1              = 1.2
	bm25B               = 0.75
)

// OracleMemory is one pair as the oracle sees it: no ids, so nothing reveals
// which pairs the generator cited.
type OracleMemory struct {
	Timestamp string `json:"timestamp"`
	Prompt    string `json:"prompt"`
	Response  string `json:"response"`
}

// OracleCase is one oracle request: the case's question and its bounded,
// chronological memory context. ContextSHA256 commits to the exact bytes the
// oracle answered from, so an audit can reproduce the attestation.
type OracleCase struct {
	Contract      string         `json:"contract"`
	CaseID        string         `json:"case_id"`
	Question      string         `json:"question"`
	Memories      []OracleMemory `json:"memories"`
	ContextSHA256 string         `json:"context_sha256"`
}

// OracleContext derives the frontier oracle's context for one case of a
// validated pack. It is a pure function of the pack and case id.
func OracleContext(p Pack, caseID string) (OracleCase, error) {
	var c *Case
	for i := range p.Cases {
		if p.Cases[i].ID == caseID {
			c = &p.Cases[i]
			break
		}
	}
	if c == nil {
		return OracleCase{}, fmt.Errorf("pack has no case %q", caseID)
	}
	type visiblePair struct {
		seededPair
		id string
	}
	var visible []visiblePair
	for wi, w := range p.MemoryWaves {
		if w.UserID != c.UserID || wi > c.RunAfterWave {
			continue
		}
		for _, pr := range w.Pairs {
			visible = append(visible, visiblePair{seededPair{pair: pr, userID: w.UserID, wave: wi}, pr.PairID})
		}
	}
	sort.Slice(visible, func(i, j int) bool { return visible[i].id < visible[j].id })
	byID := map[string]int{}
	for i, vp := range visible {
		byID[vp.id] = i
	}

	chosen := map[int]bool{}
	var query strings.Builder
	query.WriteString(c.Question)
	for _, pid := range c.EvidencePairIDs {
		i, ok := byID[pid]
		if !ok {
			return OracleCase{}, fmt.Errorf("case %q evidence pair %q is not visible to it", caseID, pid)
		}
		chosen[i] = true
		query.WriteString("\n")
		query.WriteString(visible[i].pair.Prompt)
		query.WriteString("\n")
		query.WriteString(visible[i].pair.Response)
	}
	if len(chosen) == 0 {
		return OracleCase{}, fmt.Errorf("case %q cites no evidence", caseID)
	}

	// Rank every other visible pair once by BM25 against the question and
	// evidence; declared competitors go first, in the same rank order.
	docs := make([][]string, len(visible))
	for i, vp := range visible {
		docs[i] = oracleTokens(vp.pair.Prompt + "\n" + vp.pair.Response)
	}
	scores := bm25(docs, oracleTokens(query.String()))
	competitors := make([]string, 0, len(c.DistractorAnswers)+len(c.DumpGuard)+1)
	competitors = append(competitors, c.DistractorAnswers...)
	competitors = append(competitors, c.DumpGuard...)
	if c.ForbiddenAnswer != "" {
		competitors = append(competitors, c.ForbiddenAnswer)
	}
	declared := func(i int) bool {
		text := visible[i].pair.Prompt + "\n" + visible[i].pair.Response
		for _, f := range competitors {
			if strings.TrimSpace(f) != "" && grade.Hit(f, text) {
				return true
			}
		}
		return false
	}
	var rest []int
	for i := range visible {
		if !chosen[i] {
			rest = append(rest, i)
		}
	}
	isDeclared := make(map[int]bool, len(rest))
	for _, i := range rest {
		isDeclared[i] = declared(i)
	}
	sort.SliceStable(rest, func(a, b int) bool {
		ia, ib := rest[a], rest[b]
		if isDeclared[ia] != isDeclared[ib] {
			return isDeclared[ia]
		}
		if scores[ia] != scores[ib] {
			return scores[ia] > scores[ib]
		}
		return visible[ia].id < visible[ib].id
	})
	for n, i := range rest {
		if n == OracleCompetitorPairs {
			break
		}
		if !isDeclared[i] && scores[i] <= 0 {
			// Shares no term with the question or evidence: not a competitor.
			break
		}
		chosen[i] = true
	}

	picked := make([]int, 0, len(chosen))
	for i := range chosen {
		picked = append(picked, i)
	}
	when := func(i int) time.Time {
		t, _ := time.Parse(time.RFC3339, visible[i].pair.Timestamp)
		return t
	}
	sort.Slice(picked, func(a, b int) bool {
		ta, tb := when(picked[a]), when(picked[b])
		if !ta.Equal(tb) {
			return ta.Before(tb)
		}
		return visible[picked[a]].id < visible[picked[b]].id
	})
	out := OracleCase{Contract: OracleContractV1, CaseID: c.ID, Question: c.Question, Memories: make([]OracleMemory, len(picked))}
	for n, i := range picked {
		pr := visible[i].pair
		out.Memories[n] = OracleMemory{Timestamp: pr.Timestamp, Prompt: pr.Prompt, Response: pr.Response}
	}
	digest, err := oracleContextDigest(out)
	if err != nil {
		return OracleCase{}, err
	}
	out.ContextSHA256 = digest
	return out, nil
}

func oracleContextDigest(c OracleCase) (string, error) {
	c.ContextSHA256 = ""
	raw, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// VerifyOracleContext reports whether c is exactly the context OracleContext
// derives for its case from p.
func VerifyOracleContext(p Pack, c OracleCase) error {
	want, err := OracleContext(p, c.CaseID)
	if err != nil {
		return err
	}
	if c.ContextSHA256 != want.ContextSHA256 {
		return errors.New("oracle context does not match the pack")
	}
	return nil
}

var oracleStopwords = map[string]bool{
	"a": true, "an": true, "and": true, "are": true, "as": true, "at": true, "be": true, "by": true,
	"did": true, "do": true, "for": true, "from": true, "has": true, "have": true, "i": true, "in": true,
	"is": true, "it": true, "my": true, "of": true, "on": true, "or": true, "that": true, "the": true,
	"this": true, "to": true, "was": true, "what": true, "when": true, "which": true, "with": true,
	"you": true, "your": true, "me": true, "we": true, "our": true, "how": true, "much": true, "now": true,
}

// oracleTokens lowercases text and splits it into letter/digit runs, dropping
// stopwords. It is deliberately simple: ranking only has to be deterministic
// and reproducible by a miner, not state of the art.
func oracleTokens(text string) []string {
	fields := strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	out := fields[:0]
	for _, f := range fields {
		if !oracleStopwords[f] {
			out = append(out, f)
		}
	}
	return out
}

// bm25 scores every document against the query's distinct terms. Terms and
// documents are summed in a fixed order so the floats are reproducible.
func bm25(docs [][]string, query []string) []float64 {
	scores := make([]float64, len(docs))
	if len(docs) == 0 {
		return scores
	}
	seen := map[string]bool{}
	var terms []string
	for _, t := range query {
		if !seen[t] {
			seen[t] = true
			terms = append(terms, t)
		}
	}
	sort.Strings(terms)
	tf := make([]map[string]int, len(docs))
	df := map[string]int{}
	var total float64
	for i, d := range docs {
		tf[i] = map[string]int{}
		for _, t := range d {
			tf[i][t]++
		}
		for t := range tf[i] {
			if seen[t] {
				df[t]++
			}
		}
		total += float64(len(d))
	}
	avg := total / float64(len(docs))
	if avg == 0 {
		return scores
	}
	n := float64(len(docs))
	for _, t := range terms {
		if df[t] == 0 {
			continue
		}
		idf := math.Log(1 + (n-float64(df[t])+0.5)/(float64(df[t])+0.5))
		for i, d := range docs {
			f := float64(tf[i][t])
			if f == 0 {
				continue
			}
			scores[i] += idf * f * (bm25K1 + 1) / (f + bm25K1*(1-bm25B+bm25B*float64(len(d))/avg))
		}
	}
	return scores
}

// FrontierAuditSample picks which of a column's oracle-attested cases an
// operator audits: the n case ids with the lowest
//
//	sha256(domain ‖ 0x00 ‖ round_seed ‖ 0x00 ‖ column_id ‖ 0x00 ‖ case_id)
//
// in that order. The round seed is secret until the round reveals, so a
// generator cannot know which of its cases will be read, and Platform cannot
// grind the choice (the seed is commit-reveal). One invalid case in the
// sample voids every frontier attestation on the column for that round.
func FrontierAuditSample(roundSeed, columnID string, caseIDs []string, n int) []string {
	if n <= 0 || len(caseIDs) == 0 {
		return nil
	}
	type ranked struct {
		id  string
		key string
	}
	seen := map[string]bool{}
	all := make([]ranked, 0, len(caseIDs))
	for _, id := range caseIDs {
		if seen[id] {
			continue
		}
		seen[id] = true
		h := sha256.New()
		h.Write([]byte(frontierAuditDomain))
		h.Write([]byte{0})
		h.Write([]byte(roundSeed))
		h.Write([]byte{0})
		h.Write([]byte(columnID))
		h.Write([]byte{0})
		h.Write([]byte(id))
		all = append(all, ranked{id: id, key: hex.EncodeToString(h.Sum(nil))})
	}
	sort.Slice(all, func(i, j int) bool { return all[i].key < all[j].key })
	if n > len(all) {
		n = len(all)
	}
	out := make([]string, n)
	for i := range out {
		out[i] = all[i].id
	}
	return out
}

// OraclePack derives the pack the frontier oracle runs: one memory graph per
// listed case, seeded with only that case's OracleContext memories. Each case
// keeps its question, gold, and grading fields, and the pack keeps the source
// pack's grading policy, so the oracle is graded exactly like a panel agent.
//
// Every user is seeded before any case runs (each case runs after the last
// wave), so cases execute concurrently. The derived pack is not a valid
// generator pack (it has one user and one wave per case, beyond MaxUsers and
// MaxWaves); a runner validates the source pack and executes this one as a
// trusted derivation. Cases are returned in the order of caseIDs, which must
// be distinct pack case ids.
func OraclePack(p Pack, caseIDs []string) (Pack, []OracleCase, error) {
	if len(caseIDs) == 0 {
		return Pack{}, nil, errors.New("oracle pack needs at least one case")
	}
	byID := map[string]Case{}
	for _, c := range p.Cases {
		byID[c.ID] = c
	}
	out := Pack{
		Contract:            p.Contract,
		GradingBenchVersion: p.GradingBenchVersion,
		MinerHotkey:         p.MinerHotkey,
		Generator:           p.Generator,
		MemoryWaves:         make([]protocol.SeedRequest, 0, len(caseIDs)),
		Cases:               make([]Case, 0, len(caseIDs)),
	}
	contexts := make([]OracleCase, 0, len(caseIDs))
	seen := map[string]bool{}
	for i, id := range caseIDs {
		c, ok := byID[id]
		if !ok || seen[id] {
			return Pack{}, nil, fmt.Errorf("oracle case %q is not a distinct pack case", id)
		}
		seen[id] = true
		oc, err := OracleContext(p, id)
		if err != nil {
			return Pack{}, nil, err
		}
		user := fmt.Sprintf("oracle-%03d", i)
		wave := protocol.SeedRequest{UserID: user, Wave: i, Pairs: make([]protocol.MemoryPair, len(oc.Memories))}
		for j, m := range oc.Memories {
			wave.Pairs[j] = protocol.MemoryPair{
				PairID:    fmt.Sprintf("%s-%02d", user, j),
				SessionID: user,
				Timestamp: m.Timestamp,
				Prompt:    m.Prompt,
				Response:  m.Response,
			}
		}
		out.MemoryWaves = append(out.MemoryWaves, wave)
		c.UserID = user
		c.EvidencePairIDs = nil
		out.Cases = append(out.Cases, c)
		contexts = append(contexts, oc)
	}
	for i := range out.Cases {
		out.Cases[i].RunAfterWave = len(out.MemoryWaves) - 1
	}
	return out, contexts, nil
}
