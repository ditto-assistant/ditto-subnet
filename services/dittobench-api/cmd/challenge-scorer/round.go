package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"log"
	"net/http"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

// Round work. Platform opens a round on the chain's epoch counter, freezes its
// generator columns, and derives one secret seed. The scorer does the two
// CPU-only halves of a round:
//
//   - generate: build every column's generator and run it on its column seed
//     (challenge.RoundColumnSeed), validate each pack against its request,
//     and encode the valid packs into the canonical round dataset validators
//     run every panel agent on;
//   - score: once validators have posted every panel agent's results, apply
//     challenge.ScoreRound to the matrix and post the column scores and the
//     pairwise-neutral agent standings.
//
// Neither half talks to a model. Mirrors apps/platform/ditto/api_models/
// generator_rounds.py.

type roundWorkResponse struct {
	Work *roundWork `json:"work"`
}

type roundWork struct {
	Kind       string `json:"kind"` // "generate" | "score"
	LeaseID    string `json:"lease_id"`
	RoundIndex int64  `json:"round_index"`

	// generate
	Seed           string           `json:"seed,omitempty"`
	CasesPerColumn int              `json:"cases_per_column,omitempty"`
	Columns        []roundSourceCol `json:"columns,omitempty"`
	// BenchVersion is the round's frozen bench contract: each column's
	// request accepts every pack grading policy through it, and the encoded
	// dataset is checked against it. Absent (a Platform that predates it) is
	// the original v13 round.
	BenchVersion int `json:"bench_version,omitempty"`

	// score
	Params *challenge.ScoreParams `json:"params,omitempty"`
	Matrix *challenge.RoundInput  `json:"matrix,omitempty"`
}

type roundSourceCol struct {
	ColumnID     string `json:"column_id"`
	SubmissionID string `json:"submission_id"`
	MinerHotkey  string `json:"miner_hotkey"`
	SourceSHA256 string `json:"source_sha256"`
	SourceBase64 string `json:"source_base64"`
}

type roundColumnResult struct {
	ColumnID     string             `json:"column_id"`
	SubmissionID string             `json:"submission_id"`
	OK           bool               `json:"ok"`
	Build        genrun.BuildOutput `json:"build"`
	Seed         *genrun.SeedOutput `json:"seed,omitempty"`
	Error        string             `json:"error,omitempty"`
}

type roundGenerateResult struct {
	LeaseID       string              `json:"lease_id"`
	RoundIndex    int64               `json:"round_index"`
	ScorerVersion string              `json:"scorer_version"`
	Sandbox       string              `json:"sandbox"`
	Columns       []roundColumnResult `json:"columns"`
	DatasetSHA256 string              `json:"dataset_sha256,omitempty"`
	DatasetBase64 string              `json:"dataset_base64,omitempty"`
}

type roundScoreResult struct {
	LeaseID       string               `json:"lease_id"`
	RoundIndex    int64                `json:"round_index"`
	ScorerVersion string               `json:"scorer_version"`
	Score         challenge.RoundScore `json:"score"`
}

// processRound leases and completes one round work item. It reports whether
// work was leased. A request this scorer cannot serve or a sandbox failure is
// reported as a fault (Platform voids the round or re-leases it) and never as
// a column result; a column's own failure (build, crash, invalid pack) is
// posted as that column's result.
func processRound(ctx context.Context, client *platformClient, sb genrun.Sandbox) (bool, error) {
	var resp roundWorkResponse
	if err := client.do(ctx, http.MethodGet, client.pollPath("/api/v1/generator-scorer/round-work"), nil, &resp); err != nil {
		return false, err
	}
	w := resp.Work
	if w == nil {
		return false, nil
	}
	switch w.Kind {
	case "generate":
		result, err := generateRound(ctx, sb, w)
		if err != nil {
			idx := w.RoundIndex
			return true, reportFault(ctx, client, faultReport{
				Work: "round", LeaseID: w.LeaseID, RoundIndex: &idx,
			}, fmt.Errorf("round %d: %w", w.RoundIndex, err))
		}
		return true, client.do(ctx, http.MethodPost, "/api/v1/generator-scorer/round-results", result, nil)
	case "score":
		if w.Params == nil || w.Matrix == nil {
			return true, fmt.Errorf("round %d: score work is incomplete", w.RoundIndex)
		}
		score, err := challenge.ScoreRound(*w.Matrix, *w.Params)
		if err != nil {
			return true, fmt.Errorf("round %d: %w", w.RoundIndex, err)
		}
		log.Printf("round %d: scored %d columns, %d agents", w.RoundIndex, len(score.Columns), len(score.Agents))
		return true, client.do(ctx, http.MethodPost, "/api/v1/generator-scorer/round-score", roundScoreResult{
			LeaseID: w.LeaseID, RoundIndex: w.RoundIndex, ScorerVersion: ScorerVersion, Score: score,
		}, nil)
	}
	return true, fmt.Errorf("round %d: unknown work kind %q", w.RoundIndex, w.Kind)
}

// generateRound runs every column generator on its column seed and encodes
// the valid packs. Source bytes that do not hash to the column's pin are a
// Platform inconsistency, never run, and fail the whole lease.
func generateRound(ctx context.Context, sb genrun.Sandbox, w *roundWork) (roundGenerateResult, error) {
	out := roundGenerateResult{LeaseID: w.LeaseID, RoundIndex: w.RoundIndex, ScorerVersion: ScorerVersion, Sandbox: sb.Name()}
	packs := map[string][]byte{}
	for _, col := range w.Columns {
		raw, err := base64.StdEncoding.DecodeString(col.SourceBase64)
		if err != nil {
			return out, fmt.Errorf("column %s: source_base64 is not base64", col.ColumnID)
		}
		if got := challenge.Digest(raw); got != col.SourceSHA256 {
			return out, fmt.Errorf("column %s: served source hashes to %s, round pins %s", col.ColumnID, got, col.SourceSHA256)
		}
		seed := challenge.RoundColumnSeed(w.Seed, col.ColumnID)
		ev, err := evaluateArchive(ctx, sb, raw, genrun.Plan{
			MinerHotkey:       col.MinerHotkey,
			Seeds:             []string{seed},
			Cases:             w.CasesPerColumn,
			RoundBenchVersion: w.BenchVersion,
		})
		if err != nil {
			return out, fmt.Errorf("column %s: %w", col.ColumnID, err)
		}
		res := roundColumnResult{ColumnID: col.ColumnID, SubmissionID: col.SubmissionID, Build: ev.Build}
		switch {
		case !ev.Build.OK:
			res.Error = "build failed"
		case len(ev.Seeds) != 1:
			res.Error = "no generation output"
		default:
			s := ev.Seeds[0]
			res.Seed = &s
			switch {
			case s.Error != "":
				res.Error = s.Error
			case !s.Report.Valid:
				res.Error = fmt.Sprintf("invalid pack (%d issues)", len(s.Report.Issues))
			default:
				res.OK = true
				packs[col.ColumnID] = s.Pack
			}
		}
		if res.Seed != nil && res.Seed.GradingBenchVersion != 0 {
			log.Printf("round %d column %s: grading v%d %s", w.RoundIndex, col.ColumnID, res.Seed.GradingBenchVersion, res.Seed.LegacyRetry)
		}
		log.Printf("round %d column %s: ok=%t %s", w.RoundIndex, col.ColumnID, res.OK, res.Error)
		out.Columns = append(out.Columns, res)
	}
	if len(packs) == 0 {
		return out, nil
	}
	dataset, sum, err := challenge.EncodeRoundDataset(w.RoundIndex, w.Seed, packs)
	if err != nil {
		// Every pack already validated; an encoding failure (size) is posted
		// as an empty dataset so Platform can void the round with the reason.
		for i := range out.Columns {
			if out.Columns[i].OK {
				out.Columns[i].Error = "dataset encoding failed: " + err.Error()
			}
		}
		return out, nil
	}
	out.DatasetSHA256 = sum
	out.DatasetBase64 = base64.StdEncoding.EncodeToString(dataset)
	return out, nil
}
