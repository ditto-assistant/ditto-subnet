package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

type fakeRoundPlatform struct {
	*httptest.Server
	work      []*roundWork
	generated []roundGenerateResult
	scored    []roundScoreResult
	faults    []faultReport
}

func newFakeRoundPlatform(t *testing.T, work ...*roundWork) *fakeRoundPlatform {
	t.Helper()
	fp := &fakeRoundPlatform{work: work}
	fp.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/v1/generator-scorer/round-work":
			var next *roundWork
			if len(fp.work) > 0 {
				next, fp.work = fp.work[0], fp.work[1:]
			}
			_ = json.NewEncoder(w).Encode(roundWorkResponse{Work: next})
		case "/api/v1/generator-scorer/round-results":
			var res roundGenerateResult
			_ = json.NewDecoder(r.Body).Decode(&res)
			fp.generated = append(fp.generated, res)
		case "/api/v1/generator-scorer/round-score":
			var res roundScoreResult
			_ = json.NewDecoder(r.Body).Decode(&res)
			fp.scored = append(fp.scored, res)
		case "/api/v1/generator-scorer/fault":
			var f faultReport
			_ = json.NewDecoder(r.Body).Decode(&f)
			fp.faults = append(fp.faults, f)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(fp.Close)
	return fp
}

func (fp *fakeRoundPlatform) client() *platformClient {
	return &platformClient{base: fp.URL, token: "secret-token", http: fp.Client()}
}

func sourceCol(id string, raw []byte) roundSourceCol {
	return roundSourceCol{
		ColumnID: id, SubmissionID: "sub-" + id, MinerHotkey: testHotkey,
		SourceSHA256: challenge.Digest(raw), SourceBase64: base64.StdEncoding.EncodeToString(raw),
	}
}

func TestProcessRoundGeneratesAndEncodesTheDataset(t *testing.T) {
	good := generatorArchive(t, false)
	broken := generatorArchive(t, true)
	seed := challenge.SeedFromInt64(77)
	fp := newFakeRoundPlatform(t, &roundWork{
		Kind: "generate", LeaseID: "lease-r", RoundIndex: 12, Seed: seed, CasesPerColumn: 16,
		Columns: []roundSourceCol{sourceCol("c0", good), sourceCol("c1", broken), sourceCol("c2", good)},
	})
	processed, err := processRound(context.Background(), fp.client(), genrun.Local{})
	if err != nil || !processed {
		t.Fatalf("processed=%v err=%v", processed, err)
	}
	if len(fp.generated) != 1 {
		t.Fatalf("posted %d results", len(fp.generated))
	}
	got := fp.generated[0]
	if got.RoundIndex != 12 || got.LeaseID != "lease-r" || got.ScorerVersion != ScorerVersion || len(got.Columns) != 3 {
		t.Fatalf("identity: %+v", got)
	}
	if !got.Columns[0].OK || got.Columns[1].OK || got.Columns[1].Error == "" || !got.Columns[2].OK {
		t.Fatalf("column outcomes: %+v", got.Columns)
	}
	raw, err := base64.StdEncoding.DecodeString(got.DatasetBase64)
	if err != nil {
		t.Fatal(err)
	}
	ds, cols, err := challenge.DecodeRoundDataset(raw, got.DatasetSHA256)
	if err != nil {
		t.Fatal(err)
	}
	if ds.RoundIndex != 12 || ds.Seed != seed || len(cols) != 2 || cols[0].ColumnID != "c0" || cols[1].ColumnID != "c2" {
		t.Fatalf("dataset: %+v", ds)
	}
	// The two columns run the same generator but on distinct column seeds,
	// so their seed-dependent cases differ.
	if got.Columns[0].Seed.Report.Cases[0].Fingerprint == got.Columns[2].Seed.Report.Cases[0].Fingerprint &&
		got.Columns[0].Seed.Report.Cases[1].Fingerprint == got.Columns[2].Seed.Report.Cases[1].Fingerprint {
		t.Fatal("columns must not share a seed")
	}
}

func TestProcessRoundRefusesMismatchedPin(t *testing.T) {
	good := generatorArchive(t, false)
	col := sourceCol("c0", good)
	col.SourceSHA256 = strings.Repeat("0", 64)
	fp := newFakeRoundPlatform(t, &roundWork{Kind: "generate", RoundIndex: 1, Seed: challenge.SeedFromInt64(1), CasesPerColumn: 16, Columns: []roundSourceCol{col}})
	if _, err := processRound(context.Background(), fp.client(), genrun.Local{}); err == nil || len(fp.generated) != 0 {
		t.Fatalf("mismatched pin generated: err=%v posted=%d", err, len(fp.generated))
	}
}

func TestProcessRoundScoresTheMatrix(t *testing.T) {
	params := challenge.DefaultScoreParams()
	params.MinPanel = 2
	cases := []challenge.CaseRef{{CaseID: "c0/a", Fingerprint: "fa"}, {CaseID: "c0/b", Fingerprint: "fb"}}
	matrix := challenge.RoundInput{
		Columns: []challenge.RoundColumn{{ColumnID: "c0", Counted: true, Cases: cases}},
		Agents: []challenge.RoundAgent{
			{AgentID: "x", OwnerKey: "o1", Results: []challenge.PanelCaseResult{{CaseID: "c0/a", Score: 1}, {CaseID: "c0/b", Score: 1}}},
			{AgentID: "y", OwnerKey: "o2", Results: []challenge.PanelCaseResult{{CaseID: "c0/a", Score: 0}, {CaseID: "c0/b", Score: 1}}},
			{AgentID: "z", OwnerKey: "o3", Results: []challenge.PanelCaseResult{{CaseID: "c0/a", Score: 1}, {CaseID: "c0/b", Score: 1}}},
		},
	}
	fp := newFakeRoundPlatform(t, &roundWork{Kind: "score", LeaseID: "s", RoundIndex: 4, Params: &params, Matrix: &matrix})
	if _, err := processRound(context.Background(), fp.client(), genrun.Local{}); err != nil {
		t.Fatal(err)
	}
	if len(fp.scored) != 1 {
		t.Fatalf("posted %d scores", len(fp.scored))
	}
	want, _ := challenge.ScoreRound(matrix, params)
	gotRaw, _ := json.Marshal(fp.scored[0].Score)
	wantRaw, _ := json.Marshal(want)
	if string(gotRaw) != string(wantRaw) || fp.scored[0].RoundIndex != 4 {
		t.Fatalf("score: %s", gotRaw)
	}
}

// A round leased with more cases per column than this build supports voids
// as an infrastructure fault; no column result (which could reject a miner's
// generator) is ever posted.
func TestProcessRoundReportsUnsupportedCaseCountsAsFaults(t *testing.T) {
	good := generatorArchive(t, false)
	fp := newFakeRoundPlatform(t, &roundWork{
		Kind: "generate", LeaseID: "lease-r", RoundIndex: 25968, Seed: challenge.SeedFromInt64(1),
		CasesPerColumn: challenge.MaxCases + 1, Columns: []roundSourceCol{sourceCol("c0", good)},
	})
	_, err := processRound(context.Background(), fp.client(), genrun.Local{})
	if !errors.Is(err, genrun.ErrInvalidRequest) || len(fp.generated) != 0 {
		t.Fatalf("unsupported round: err=%v generated=%d", err, len(fp.generated))
	}
	f := fp.faults
	if len(f) != 1 || f[0].Work != "round" || f[0].RoundIndex == nil || *f[0].RoundIndex != 25968 ||
		f[0].ErrorKind != "unsupported_request" || f[0].LeaseID != "lease-r" {
		t.Fatalf("round fault: %+v", f)
	}
}

// A v14 round offers every policy through v14: a version-aware column
// declares v14, a generator that predates v14 keeps its v13 column, and the
// dataset holds both. A round without bench_version (an older Platform) or at
// v13 sends the original request, so no column reports a grading version.
func TestProcessRoundGeneratesAtTheRoundBenchVersion(t *testing.T) {
	legacy := generatorArchive(t, false)
	aware := versionAwareArchive(t)
	seed := challenge.SeedFromInt64(78)
	for _, tc := range []struct {
		bench int
		want  map[string]int
	}{
		{0, map[string]int{"c0": 0, "c1": 0}},
		{13, map[string]int{"c0": 0, "c1": 0}},
		{14, map[string]int{"c0": 13, "c1": 14}},
	} {
		fp := newFakeRoundPlatform(t, &roundWork{
			Kind: "generate", LeaseID: "lease-v", RoundIndex: 13, Seed: seed, CasesPerColumn: 16,
			BenchVersion: tc.bench,
			Columns:      []roundSourceCol{sourceCol("c0", legacy), sourceCol("c1", aware)},
		})
		if processed, err := processRound(context.Background(), fp.client(), genrun.Local{}); err != nil || !processed {
			t.Fatalf("bench %d: processed=%v err=%v", tc.bench, processed, err)
		}
		got := fp.generated[0]
		for _, col := range got.Columns {
			if !col.OK || col.Seed.GradingBenchVersion != tc.want[col.ColumnID] || col.Seed.LegacyRetry != "" {
				t.Fatalf("bench %d column %s: %+v", tc.bench, col.ColumnID, col.Seed)
			}
		}
		raw, _ := base64.StdEncoding.DecodeString(got.DatasetBase64)
		_, cols, err := challenge.DecodeRoundDataset(raw, got.DatasetSHA256)
		if err != nil {
			t.Fatal(err)
		}
		round := tc.bench
		if round == 0 {
			round = 13
		}
		if err := challenge.CheckRoundGradingVersions(cols, round); err != nil {
			t.Fatalf("bench %d: %v", tc.bench, err)
		}
	}
}
