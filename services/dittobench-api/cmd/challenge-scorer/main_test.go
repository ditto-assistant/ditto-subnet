package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

// oracleHarness answers every case with its gold answer. Several packs may
// share question templates, so it tells packs apart by the memory each opaque
// user was seeded with.
func oracleHarness(t *testing.T, packs ...challenge.Pack) *httptest.Server {
	t.Helper()
	answers := make([]map[string]string, len(packs))
	promptPack := map[string]int{}
	for i, p := range packs {
		answers[i] = map[string]string{}
		for _, c := range p.Cases {
			a := c.ExpectedAnswer
			if len(c.AnswerItems) > 0 {
				a = strings.Join(c.AnswerItems, " and ")
			}
			answers[i][c.Question] = a
		}
		for _, w := range p.MemoryWaves {
			for _, pr := range w.Pairs {
				if prior, seen := promptPack[pr.Prompt]; seen && prior != i {
					promptPack[pr.Prompt] = -1 // shared filler identifies no pack
				} else {
					promptPack[pr.Prompt] = i
				}
			}
		}
	}
	var mu sync.Mutex
	userPack := map[string]int{}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/health":
		case "/seed":
			var req protocol.SeedRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			mu.Lock()
			for _, pr := range req.Pairs {
				if i, ok := promptPack[pr.Prompt]; ok && i >= 0 {
					userPack[req.UserID] = i
				}
			}
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: len(req.Pairs)})
		case "/run":
			var req protocol.RunRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			mu.Lock()
			a := answers[userPack[req.UserID]][req.UserInput]
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(protocol.RunResponse{Answer: a, FinalText: a})
		}
	}))
}

func TestParseAgentFlag(t *testing.T) {
	got, err := parseAgentFlag("a1=http://h:1/,owner=o9,champion")
	if err != nil || got.ID != "a1" || got.URL != "http://h:1" || got.Owner != "o9" || !got.Champion {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err := parseAgentFlag("nourl"); err == nil {
		t.Fatal("missing url accepted")
	}
	if _, err := parseAgentFlag("a=http://h,bogus"); err == nil {
		t.Fatal("unknown option accepted")
	}
}

// generatorArchive packages a generator that emits the reference pack bound to
// its request (or, with crash, exits non-zero), vendored like an upload.
func generatorArchive(t *testing.T, crash bool) []byte {
	t.Helper()
	datagen, err := filepath.Abs("../../../../research/dittobench-datagen")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	body := `package main

import (
	"encoding/json"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

func main() {
	var req challenge.GenerateRequest
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil || CRASH {
		os.Exit(3)
	}
	p := challenge.ReferencePack(req.SeedInt64(), req.MinerHotkey)
	p.Generator.Seed = req.Seed
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
`
	body = strings.Replace(body, "CRASH", fmt.Sprint(crash), 1)
	gomod := "module example.com/gen\n\ngo 1.23\n\nrequire github.com/ditto-assistant/dittobench-datagen v0.0.0\n\nreplace github.com/ditto-assistant/dittobench-datagen => " + datagen + "\n"
	for name, data := range map[string]string{"main.go": body, "go.mod": gomod} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, _, err := genrun.PackDir(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

type fakePlatform struct {
	*httptest.Server
	posted []resultRequest
	work   []*workItem
}

func newFakePlatform(t *testing.T, work ...*workItem) *fakePlatform {
	t.Helper()
	fp := &fakePlatform{work: work}
	fp.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/v1/generator-scorer/work":
			var next *workItem
			if len(fp.work) > 0 {
				next, fp.work = fp.work[0], fp.work[1:]
			}
			_ = json.NewEncoder(w).Encode(workResponse{Work: next})
		case "/api/v1/generator-scorer/results":
			var res resultRequest
			_ = json.NewDecoder(r.Body).Decode(&res)
			fp.posted = append(fp.posted, res)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(fp.Close)
	return fp
}

func (fp *fakePlatform) client() *platformClient {
	return &platformClient{base: fp.URL, token: "secret-token", http: fp.Client()}
}

func work(raw []byte, seeds ...string) *workItem {
	return &workItem{
		LeaseID: "lease-1", RunID: "run-1", SubmissionID: "sub-1", MinerHotkey: testHotkey,
		SourceSHA256: challenge.Digest(raw), SourceBase64: base64.StdEncoding.EncodeToString(raw),
		Seeds: seeds, ProbeSeed: challenge.SeedFromInt64(1000), CasesPerSeed: 16,
		Panel: []panelSlot{
			{AgentID: "a1", OwnerKey: "o1", Champion: true},
			{AgentID: "a2", OwnerKey: "o2"},
			{AgentID: "unprovisioned", OwnerKey: "o3"},
		},
	}
}

func TestProcessOneBuildsRunsAndPosts(t *testing.T) {
	runner.Configure(true)
	raw := generatorArchive(t, false)
	seeds := []string{challenge.SeedFromInt64(1), challenge.SeedFromInt64(2)}
	// The oracle answers every case of both seeds' packs.
	var packs []challenge.Pack
	for _, seed := range seeds {
		req := challenge.NewGenerateRequest(seed, testHotkey, 16)
		packs = append(packs, challenge.ReferencePack(req.SeedInt64(), testHotkey))
	}
	harness := oracleHarness(t, packs...)
	defer harness.Close()
	fp := newFakePlatform(t, work(raw, seeds...))
	harnesses := map[string]string{"a1": harness.URL, "a2": harness.URL}

	processed, err := processOne(context.Background(), fp.client(), genrun.Local{}, harnesses, 2)
	if err != nil || !processed {
		t.Fatalf("processed=%v err=%v", processed, err)
	}
	if len(fp.posted) != 1 {
		t.Fatalf("posted %d results", len(fp.posted))
	}
	got := fp.posted[0]
	if !got.Build.OK || got.Deterministic == nil || !*got.Deterministic || got.Probe == nil || !got.Probe.Report.Valid {
		t.Fatalf("evaluation: %+v", got)
	}
	if got.RunID != "run-1" || got.ScorerVersion != ScorerVersion || got.SourceSHA256 != challenge.Digest(raw) || len(got.Seeds) != 2 {
		t.Fatalf("identity: %+v", got)
	}
	for _, s := range got.Seeds {
		if !s.Report.Valid || len(s.Panel) != 2 {
			t.Fatalf("seed %s: valid=%v panel=%d", s.Seed, s.Report.Valid, len(s.Panel))
		}
		for _, m := range s.Panel {
			if len(m.Results) != 16 {
				t.Fatalf("%s: %d results", m.AgentID, len(m.Results))
			}
			for _, r := range m.Results {
				if r.Fault || r.Score != 1 {
					t.Fatalf("%s/%s: %+v", m.AgentID, r.CaseID, r)
				}
			}
		}
	}
	// Nothing left: the next cycle is idle.
	processed, err = processOne(context.Background(), fp.client(), genrun.Local{}, harnesses, 2)
	if processed || err != nil {
		t.Fatalf("idle cycle processed=%v err=%v", processed, err)
	}
}

func TestProcessOneRefusesMismatchedPin(t *testing.T) {
	raw := generatorArchive(t, false)
	item := work(raw, challenge.SeedFromInt64(1))
	item.SourceSHA256 = strings.Repeat("0", 64)
	fp := newFakePlatform(t, item)
	if _, err := processOne(context.Background(), fp.client(), genrun.Local{}, nil, 1); err == nil || len(fp.posted) != 0 {
		t.Fatalf("mismatched pin evaluated: err=%v posted=%d", err, len(fp.posted))
	}
}

func TestProcessOneReportsBrokenGeneratorsWithoutRunningThePanel(t *testing.T) {
	raw := generatorArchive(t, true)
	fp := newFakePlatform(t, work(raw, challenge.SeedFromInt64(1)))
	if _, err := processOne(context.Background(), fp.client(), genrun.Local{}, map[string]string{"a1": "http://127.0.0.1:1"}, 1); err != nil {
		t.Fatal(err)
	}
	got := fp.posted[0]
	if !got.Build.OK || got.Seeds[0].Error == "" || len(got.Seeds[0].Panel) != 0 || got.Deterministic != nil {
		t.Fatalf("broken generator: %+v", got)
	}
}

func TestProcessOneReportsBadArchivesAsBuildFailures(t *testing.T) {
	raw := []byte("not a gzip archive")
	fp := newFakePlatform(t, work(raw, challenge.SeedFromInt64(1)))
	if _, err := processOne(context.Background(), fp.client(), genrun.Local{}, nil, 1); err != nil {
		t.Fatal(err)
	}
	if got := fp.posted[0]; got.Build.OK || !strings.HasPrefix(got.Build.Error, "archive:") || len(got.Seeds) != 0 {
		t.Fatalf("bad archive: %+v", got)
	}
}

func TestProcessOneLeavesSandboxFailuresUnposted(t *testing.T) {
	raw := generatorArchive(t, false)
	fp := newFakePlatform(t, work(raw, challenge.SeedFromInt64(1)))
	_, err := processOne(context.Background(), fp.client(), genrun.Docker{Binary: "/nonexistent/docker"}, nil, 1)
	if !errors.Is(err, genrun.ErrSandbox) || len(fp.posted) != 0 {
		t.Fatalf("sandbox failure: err=%v posted=%d", err, len(fp.posted))
	}
}
