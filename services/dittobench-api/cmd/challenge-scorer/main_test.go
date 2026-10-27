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
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

func TestParseAgentFlag(t *testing.T) {
	got, err := parseAgentFlag("a1=http://h:1/,owner=o9,champion")
	if err != nil || got.ID != "a1" || got.URL != "http://h:1" || got.Owner != "o9" || !got.Champion {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err := parseAgentFlag("nourl"); err == nil {
		t.Fatal("missing url accepted")
	}
	for _, bad := range []string{"a=/", "a=h:1", "a=ftp://h", "a=http://h,owner="} {
		if _, err := parseAgentFlag(bad); err == nil {
			t.Errorf("accepted %q", bad)
		}
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
	faults []faultReport
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

func (fp *fakePlatform) client() *platformClient {
	return &platformClient{base: fp.URL, token: "secret-token", http: fp.Client()}
}

func work(raw []byte, seeds ...string) *workItem {
	return &workItem{
		LeaseID: "lease-1", RunID: "run-1", SubmissionID: "sub-1", MinerHotkey: testHotkey,
		SourceSHA256: challenge.Digest(raw), SourceBase64: base64.StdEncoding.EncodeToString(raw),
		Seeds: seeds, ProbeSeed: challenge.SeedFromInt64(1000), CasesPerSeed: 16,
	}
}

func TestProcessOneQualifiesAndPosts(t *testing.T) {
	raw := generatorArchive(t, false)
	seeds := []string{challenge.SeedFromInt64(1), challenge.SeedFromInt64(2)}
	fp := newFakePlatform(t, work(raw, seeds...))

	processed, err := processOne(context.Background(), fp.client(), genrun.Local{})
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
		if !s.Report.Valid || len(s.Report.Cases) != 16 {
			t.Fatalf("seed %s: valid=%v cases=%d", s.Seed, s.Report.Valid, len(s.Report.Cases))
		}
	}
	// Nothing left: the next cycle is idle.
	processed, err = processOne(context.Background(), fp.client(), genrun.Local{})
	if processed || err != nil {
		t.Fatalf("idle cycle processed=%v err=%v", processed, err)
	}
}

func TestProcessOneRefusesMismatchedPin(t *testing.T) {
	raw := generatorArchive(t, false)
	item := work(raw, challenge.SeedFromInt64(1))
	item.SourceSHA256 = strings.Repeat("0", 64)
	fp := newFakePlatform(t, item)
	if _, err := processOne(context.Background(), fp.client(), genrun.Local{}); err == nil || len(fp.posted) != 0 {
		t.Fatalf("mismatched pin evaluated: err=%v posted=%d", err, len(fp.posted))
	}
}

func TestProcessOneReportsBrokenGenerators(t *testing.T) {
	raw := generatorArchive(t, true)
	fp := newFakePlatform(t, work(raw, challenge.SeedFromInt64(1)))
	if _, err := processOne(context.Background(), fp.client(), genrun.Local{}); err != nil {
		t.Fatal(err)
	}
	got := fp.posted[0]
	if !got.Build.OK || got.Seeds[0].Error == "" || got.Deterministic != nil {
		t.Fatalf("broken generator: %+v", got)
	}
}

func TestProcessOneReportsBadArchivesAsBuildFailures(t *testing.T) {
	raw := []byte("not a gzip archive")
	fp := newFakePlatform(t, work(raw, challenge.SeedFromInt64(1)))
	if _, err := processOne(context.Background(), fp.client(), genrun.Local{}); err != nil {
		t.Fatal(err)
	}
	if got := fp.posted[0]; got.Build.OK || !strings.HasPrefix(got.Build.Error, "archive:") || len(got.Seeds) != 0 {
		t.Fatalf("bad archive: %+v", got)
	}
}

func TestProcessOneReportsSandboxFailuresAsFaults(t *testing.T) {
	raw := generatorArchive(t, false)
	fp := newFakePlatform(t, work(raw, challenge.SeedFromInt64(1)))
	_, err := processOne(context.Background(), fp.client(), genrun.Docker{Binary: "/nonexistent/docker"})
	if !errors.Is(err, genrun.ErrSandbox) || len(fp.posted) != 0 {
		t.Fatalf("sandbox failure: err=%v posted=%d", err, len(fp.posted))
	}
	if len(fp.faults) != 1 || fp.faults[0].ErrorKind != "sandbox" || fp.faults[0].RunID != "run-1" || fp.faults[0].Work != "qualification" {
		t.Fatalf("sandbox fault report: %+v", fp.faults)
	}
}

// A lease asking for more cases than this build's pack contract allows (the
// 2026-10-27 stale-scorer incident) is the scorer's fault: it is reported as
// an unsupported request before any generator runs, never as a seed error.
func TestProcessOneReportsUnsupportedCaseCountsAsFaults(t *testing.T) {
	raw := generatorArchive(t, false)
	item := work(raw, challenge.SeedFromInt64(1))
	item.CasesPerSeed = challenge.MaxCases + 1
	fp := newFakePlatform(t, item)
	_, err := processOne(context.Background(), fp.client(), genrun.Local{})
	if !errors.Is(err, genrun.ErrInvalidRequest) || len(fp.posted) != 0 {
		t.Fatalf("unsupported request: err=%v posted=%d", err, len(fp.posted))
	}
	f := fp.faults
	if len(f) != 1 || f[0].ErrorKind != "unsupported_request" || f[0].LeaseID != "lease-1" || f[0].SubmissionID != "sub-1" ||
		f[0].ScorerVersion != ScorerVersion || !strings.Contains(f[0].Detail, "request cases must be in") {
		t.Fatalf("fault report: %+v", f)
	}
}

func TestProcessOneDoesNotReportCancellation(t *testing.T) {
	fp := newFakePlatform(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := reportFault(ctx, fp.client(), faultReport{Work: "qualification", LeaseID: "lease-1"}, genrun.ErrSandbox)
	if !errors.Is(err, genrun.ErrSandbox) || len(fp.faults) != 0 {
		t.Fatalf("a stopping scorer must not report: err=%v faults=%d", err, len(fp.faults))
	}
}

// Draining waits for an in-flight request and prevents a qualification claim.
func TestServeDrainFinishesCurrentWorkBeforeExit(t *testing.T) {
	t.Setenv(tokenEnv, "test-token")
	entered := make(chan struct{})
	release := make(chan struct{})
	requests := make(chan string, 2)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests <- r.URL.Path
		close(entered)
		select {
		case <-release:
			fmt.Fprint(w, `{"work":null}`)
		case <-r.Context().Done():
			t.Error("drain cancelled the in-flight request")
		}
	}))
	defer srv.Close()
	drain, stopDrain := context.WithCancel(context.Background())
	defer stopDrain()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- serveWithDrain(ctx, drain, []string{"-platform", srv.URL, "-insecure-platform"}) }()
	<-entered
	stopDrain()
	select {
	case <-done:
		t.Fatal("drain exited before work finished")
	case <-time.After(20 * time.Millisecond):
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if path := <-requests; path != "/api/v1/generator-scorer/round-work" {
		t.Fatal(path)
	}
	select {
	case path := <-requests:
		t.Fatalf("claimed more work after drain: %s", path)
	default:
	}
}
