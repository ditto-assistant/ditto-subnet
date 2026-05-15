package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// scoreWithMech bundles a Score with its mechanism so the aggregator
// can split weights per mechanism without losing the routing info.
type scoreWithMech struct {
	Mechanism bittensor.Mechanism
	Score     bittensor.Score
}

// scoreMiner drives every case in the plan against the given miner,
// scores each response, and returns the per-case Scores plus a
// challenge_id -> hotkey map for the aggregator.
//
// On any transport failure or refusal the case is recorded as a
// zero-Score with an explanatory note; one miner falling over never
// blocks the rest of the run.
func scoreMiner(
	ctx context.Context,
	hotkey string,
	commit MinerCommitment,
	plan partitionedPlan,
	cfg Config,
	logf func(string, ...any),
) ([]scoreWithMech, map[string]string, error) {
	l, err := chooseLauncher(cfg, commit)
	if err != nil {
		return nil, nil, err
	}

	driver, err := newHarnessDriver(ctx, hotkey, l)
	if err != nil {
		return nil, nil, err
	}
	defer driver.Close()

	scores := make([]scoreWithMech, 0, len(plan.Core)+len(plan.Retrieval))
	hkByChallenge := map[string]string{}

	for _, c := range plan.Core {
		cid := newChallengeID()
		hkByChallenge[cid] = hotkey
		req := buildCoreRequest(cid, c, cfg)
		score := scoreCoreCase(ctx, driver, cid, c, req, plan.CoreVis[c.ID], cfg)
		scores = append(scores, scoreWithMech{Mechanism: bittensor.MechanismCore, Score: score})
	}
	for _, c := range plan.Retrieval {
		cid := newChallengeID()
		hkByChallenge[cid] = hotkey
		req := buildRetrievalRequest(cid, c, cfg)
		score := scoreRetrievalCase(ctx, driver, cid, c, req, plan.RetrVis[c.ID], cfg)
		scores = append(scores, scoreWithMech{Mechanism: bittensor.MechanismRetrieval, Score: score})
	}
	logf("  -> %d cases scored for %s", len(scores), hotkey)
	return scores, hkByChallenge, nil
}

func chooseLauncher(cfg Config, commit MinerCommitment) (launcher, error) {
	if cfg.SelfTest || commit.Image == "" {
		return echoLauncher{}, nil
	}
	return dockerLauncher{commitment: commit}, nil
}

func newChallengeID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// Fall back to a time-based identifier; the validator does not
		// require cryptographic strength here, just uniqueness within a
		// run so the aggregator can map back to a miner.
		return fmt.Sprintf("t-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}

func buildCoreRequest(cid string, c ToolCallCase, cfg Config) bittensor.ChallengeRequest {
	return bittensor.ChallengeRequest{
		SchemaVersion: bittensor.SchemaVersion,
		ChallengeID:   cid,
		Mechanism:     bittensor.MechanismCore,
		CaseID:        c.ID,
		Category:      c.Category,
		Domain:        c.Domain,
		Prompt:        c.Prompt,
		ValidatorSeed: cfg.Secret,
		IssuedAt:      time.Now().UTC(),
		DeadlineMs:    cfg.DeadlineMs,
	}
}

func buildRetrievalRequest(cid string, c RetrievalCase, cfg Config) bittensor.ChallengeRequest {
	return bittensor.ChallengeRequest{
		SchemaVersion: bittensor.SchemaVersion,
		ChallengeID:   cid,
		Mechanism:     bittensor.MechanismRetrieval,
		CaseID:        c.ID,
		Category:      c.Category,
		Query:         c.Query,
		K:             c.K,
		UserFixtureID: c.UserFixtureID,
		IncludeAnswer: false,
		ValidatorSeed: cfg.Secret,
		IssuedAt:      time.Now().UTC(),
		DeadlineMs:    cfg.DeadlineMs,
	}
}

func scoreCoreCase(
	ctx context.Context,
	driver *harnessDriver,
	cid string,
	c ToolCallCase,
	req bittensor.ChallengeRequest,
	visibility string,
	cfg Config,
) bittensor.Score {
	resp, err := driver.send(ctx, req, cfg.DeadlineMs)
	if err != nil {
		return zeroScore(cid, c.ID, c.Category, c.Domain, visibility, bittensor.MechanismCore, classifyHarnessError(err))
	}
	if resp.Refusal != "" {
		return zeroScore(cid, c.ID, c.Category, c.Domain, visibility, bittensor.MechanismCore, "refusal:"+resp.Refusal)
	}

	// Without a private arg-matcher pipeline the validator falls back
	// to the same naive multiset F1 the Python contributor runner uses
	// (see ditto/bench/runner/run.py:_naive_tool_score). Production
	// validators replace this with their full arg-matcher pipeline.
	tool := naiveToolScore(c.ExpectedTools, resp.ToolCalls)
	in := bittensor.CoreScoreInputs{
		CaseID:           c.ID,
		Category:         c.Category,
		Domain:           c.Domain,
		Visibility:       visibility,
		NumExpectedTools: len(c.ExpectedTools),
		Tool:             tool,
		LatencyMs:        resp.TotalLatencyMs,
		BudgetLatencyMs:  cfg.BudgetLatencyMs,
	}
	s := bittensor.ScoreCore(in)
	s.ChallengeID = cid
	maybeFlagDeadline(&s, resp.TotalLatencyMs, cfg.DeadlineMs)
	return s
}

func scoreRetrievalCase(
	ctx context.Context,
	driver *harnessDriver,
	cid string,
	c RetrievalCase,
	req bittensor.ChallengeRequest,
	visibility string,
	cfg Config,
) bittensor.Score {
	resp, err := driver.send(ctx, req, cfg.DeadlineMs)
	if err != nil {
		return zeroScore(cid, c.ID, c.Category, "", visibility, bittensor.MechanismRetrieval, classifyHarnessError(err))
	}
	if resp.Refusal != "" {
		return zeroScore(cid, c.ID, c.Category, "", visibility, bittensor.MechanismRetrieval, "refusal:"+resp.Refusal)
	}

	retr := naiveRetrievalScore(c, resp.EvidenceIDs)
	in := bittensor.RetrievalScoreInputs{
		CaseID:              c.ID,
		Category:            c.Category,
		Visibility:          visibility,
		NumExpectedPairIDs:  len(c.ExpectedPairIDs),
		NumForbiddenPairIDs: len(c.ForbiddenPairIDs),
		ExpectNoTools:       c.ExpectNoTools,
		Retrieval:           retr,
		UsedTools:           len(resp.EvidenceIDs) > 0,
		LatencyMs:           resp.TotalLatencyMs,
		BudgetLatencyMs:     cfg.BudgetLatencyMs,
	}
	s := bittensor.ScoreRetrieval(in)
	s.ChallengeID = cid
	maybeFlagDeadline(&s, resp.TotalLatencyMs, cfg.DeadlineMs)
	return s
}

// naiveToolScore is the Go twin of the Python _naive_tool_score helper.
// It produces a name-only multiset F1; production validators inject
// their own arg-matcher result here.
func naiveToolScore(expected []struct {
	Name string `json:"name"`
}, observed []bittensor.ToolCall) bittensor.ToolCallScore {
	if len(expected) == 0 && len(observed) == 0 {
		return bittensor.ToolCallScore{ArgF1: 1.0, AbstainCorrect: true}
	}
	if len(expected) == 0 {
		return bittensor.ToolCallScore{}
	}
	expSet := map[string]bool{}
	for _, e := range expected {
		expSet[e.Name] = true
	}
	obsSet := map[string]bool{}
	for _, o := range observed {
		obsSet[o.Name] = true
	}
	tp := 0
	for n := range obsSet {
		if expSet[n] {
			tp++
		}
	}
	var precision, recall float64
	if len(obsSet) > 0 {
		precision = float64(tp) / float64(len(obsSet))
	}
	if len(expSet) > 0 {
		recall = float64(tp) / float64(len(expSet))
	}
	f1 := 0.0
	if precision+recall > 0 {
		f1 = 2 * precision * recall / (precision + recall)
	}
	return bittensor.ToolCallScore{NamePrecision: precision, NameRecall: recall, NameF1: f1}
}

func naiveRetrievalScore(c RetrievalCase, evidence []string) bittensor.RetrievalScore {
	expected := map[string]bool{}
	for _, id := range c.ExpectedPairIDs {
		expected[id] = true
	}
	forbidden := map[string]bool{}
	for _, id := range c.ForbiddenPairIDs {
		forbidden[id] = true
	}
	if len(expected) == 0 {
		return bittensor.RetrievalScore{AbstainCorrect: len(evidence) == 0}
	}
	top5 := evidence
	if len(top5) > 5 {
		top5 = top5[:5]
	}
	hits5 := 0
	for _, id := range top5 {
		if expected[id] {
			hits5++
		}
	}
	recall5 := float64(hits5) / float64(len(expected))
	k := c.K
	if k <= 0 {
		k = 10
	}
	if len(evidence) < k {
		k = len(evidence)
	}
	needleSeen := map[string]bool{}
	for i := 0; i < k; i++ {
		needleSeen[evidence[i]] = true
	}
	needleHit := true
	for id := range expected {
		if !needleSeen[id] {
			needleHit = false
			break
		}
	}
	var mrr float64
	for rank, id := range evidence {
		if expected[id] {
			mrr = 1.0 / float64(rank+1)
			break
		}
	}
	forbiddenHit := 0
	for _, id := range evidence {
		if forbidden[id] {
			forbiddenHit++
		}
	}
	return bittensor.RetrievalScore{
		NDCG5:             recall5,
		MRR:               mrr,
		Recall5:           recall5,
		NeedleHit:         needleHit,
		NumForbiddenHit:   forbiddenHit,
		ContradictionPass: forbiddenHit == 0,
	}
}

func zeroScore(challengeID, caseID, category, domain, visibility string, mech bittensor.Mechanism, note string) bittensor.Score {
	return bittensor.Score{
		SchemaVersion: bittensor.SchemaVersion,
		ChallengeID:   challengeID,
		CaseID:        caseID,
		Category:      category,
		Domain:        domain,
		Visibility:    visibility,
		Mechanism:     mech,
		Score:         0.0,
		Notes:         []string{note},
		GradedAt:      time.Now().UTC(),
	}
}

func classifyHarnessError(err error) string {
	if err == errHarnessTimeout {
		return "harness_timeout"
	}
	return "harness_error"
}
