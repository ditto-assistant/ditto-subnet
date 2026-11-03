package genrun

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

// fixture materializes testdata/<name> as a generator module that depends on
// this datagen checkout, vendored when requested (the docker build is offline).
func fixture(t *testing.T, name string, vendor bool) string {
	t.Helper()
	datagen, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	src, err := os.ReadFile(filepath.Join("testdata", name, "main.go"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "main.go"), src, 0o644); err != nil {
		t.Fatal(err)
	}
	gomod := "module example.com/gen\n\ngo 1.23\n\nrequire github.com/ditto-assistant/dittobench-datagen v0.0.0\n\nreplace github.com/ditto-assistant/dittobench-datagen => " + datagen + "\n"
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte(gomod), 0o644); err != nil {
		t.Fatal(err)
	}
	if vendor {
		cmd := exec.Command("go", "mod", "vendor")
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GOPROXY=off", "GOFLAGS=")
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("go mod vendor: %v\n%s", err, out)
		}
	}
	return dir
}

func plan() Plan {
	return Plan{
		MinerHotkey:      testHotkey,
		Seeds:            []string{challenge.SeedFromInt64(1), challenge.SeedFromInt64(2)},
		ProbeSeed:        challenge.SeedFromInt64(99),
		Cases:            16,
		CheckDeterminism: true,
	}
}

// testLimits keeps the generate deadline short so the never-exiting sleep
// fixture times out quickly, but wide enough that a legitimate fixture does not
// trip it while the rest of the suite saturates the runner's CPUs.
func testLimits() Limits {
	l := DefaultLimits()
	l.GenerateTimeout = 10 * time.Second
	return l
}

func TestLocalEvaluate(t *testing.T) {
	ctx := context.Background()
	sb := Local{Limits: testLimits()}

	ev, err := Evaluate(ctx, sb, fixture(t, "ok", false), plan())
	if err != nil {
		t.Fatal(err)
	}
	if !ev.Valid() || ev.Deterministic == nil || !*ev.Deterministic || ev.Probe == nil || len(ev.Seeds) != 2 {
		t.Fatalf("ok fixture: %+v", ev)
	}
	if ev.Seeds[0].PackSHA256 == ev.Seeds[1].PackSHA256 || len(ev.Seeds[0].Pack) == 0 || ev.Probe.Pack != nil {
		t.Error("seeds should differ; probe bytes are dropped")
	}

	ev, err = Evaluate(ctx, sb, fixture(t, "nondeterministic", false), plan())
	if err != nil {
		t.Fatal(err)
	}
	if ev.Valid() || ev.Deterministic == nil || *ev.Deterministic || !ev.Seeds[0].Valid() {
		t.Fatalf("nondeterministic fixture: %+v", ev)
	}

	ev, err = Evaluate(ctx, sb, fixture(t, "crash", false), plan())
	if err != nil {
		t.Fatal(err)
	}
	if ev.Valid() || !strings.Contains(ev.Seeds[0].Error, "exit 3") || ev.Deterministic != nil {
		t.Fatalf("crash fixture: %+v", ev.Seeds[0])
	}

	ev, err = Evaluate(ctx, sb, fixture(t, "sleep", false), Plan{MinerHotkey: testHotkey, Seeds: []string{challenge.SeedFromInt64(1)}, Cases: 16})
	if err != nil {
		t.Fatal(err)
	}
	if ev.Valid() || !strings.Contains(ev.Seeds[0].Error, "timeout") {
		t.Fatalf("sleep fixture: %+v", ev.Seeds[0])
	}

	ev, err = Evaluate(ctx, sb, fixture(t, "broken", false), plan())
	if err != nil {
		t.Fatal(err)
	}
	if ev.Build.OK || !strings.Contains(ev.Build.Error, "compile_failed") || len(ev.Seeds) != 0 {
		t.Fatalf("broken fixture: %+v", ev.Build)
	}

	// The request binds the case count: the reference program emits 16.
	p := plan()
	p.Cases = 20
	ev, err = Evaluate(ctx, sb, fixture(t, "ok", false), p)
	if err != nil {
		t.Fatal(err)
	}
	if ev.Valid() || ev.Seeds[0].Report.Valid {
		t.Fatal("case-count binding not enforced")
	}
}

func TestEvaluateRefusesRequestsOutsideTheContract(t *testing.T) {
	// A plan the pack contract refuses is the scorer's problem, reported
	// before anything is built, never as a generator's seed error.
	for name, mutate := range map[string]func(*Plan){
		"too many cases": func(p *Plan) { p.Cases = challenge.MaxCases + 1 },
		"too few cases":  func(p *Plan) { p.Cases = challenge.MinCases - 1 },
		"bad probe seed": func(p *Plan) { p.ProbeSeed = "nope" },
		"no seeds":       func(p *Plan) { p.Seeds = nil },
	} {
		p := plan()
		mutate(&p)
		ev, err := Evaluate(context.Background(), Docker{Binary: "/nonexistent/docker"}, t.TempDir(), p)
		if !errors.Is(err, ErrInvalidRequest) || errors.Is(err, ErrSandbox) {
			t.Fatalf("%s: want ErrInvalidRequest before any build, got %v", name, err)
		}
		if ev.Build.OK || ev.Build.Error != "" || len(ev.Seeds) != 0 {
			t.Fatalf("%s: nothing should have run: %+v", name, ev)
		}
	}
	p := plan()
	p.Cases = challenge.MaxCases
	if err := p.Validate(); err != nil {
		t.Fatalf("the contract cap must be accepted: %v", err)
	}
}

func TestEvaluateSurfacesSandboxFailures(t *testing.T) {
	_, err := Evaluate(context.Background(), Docker{Binary: "/nonexistent/docker"}, fixture(t, "ok", false), plan())
	if !errors.Is(err, ErrSandbox) {
		t.Fatalf("missing docker should be a sandbox failure, got %v", err)
	}
}

// TestDockerEvaluate runs the real scorer sandbox. It needs a docker daemon
// and pulls the pinned images; set GENRUN_DOCKER=1 to run it.
func TestDockerEvaluate(t *testing.T) {
	if os.Getenv("GENRUN_DOCKER") != "1" {
		t.Skip("set GENRUN_DOCKER=1 to run the docker sandbox test")
	}
	ctx := context.Background()
	// Container start alone can take several seconds on a desktop Docker VM,
	// so the generate limit here is wider than the local tests'. The sleep
	// fixture never exits, so it still times out.
	limits := testLimits()
	limits.GenerateTimeout = 45 * time.Second
	sb := Docker{Limits: limits}
	ev, err := Evaluate(ctx, sb, fixture(t, "netprobe", true), plan())
	if err != nil {
		t.Fatal(err)
	}
	if !ev.Valid() || !*ev.Deterministic {
		t.Fatalf("docker netprobe (network must be unreachable): build=%+v seeds=%+v", ev.Build, ev.Seeds)
	}
	ev, err = Evaluate(ctx, sb, fixture(t, "sleep", true), Plan{MinerHotkey: testHotkey, Seeds: []string{challenge.SeedFromInt64(1)}, Cases: 16})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(ev.Seeds[0].Error, "timeout") {
		t.Fatalf("docker sleep: %+v", ev.Seeds[0])
	}
	ev, err = Evaluate(ctx, sb, fixture(t, "broken", true), plan())
	if err != nil {
		t.Fatal(err)
	}
	if ev.Build.OK || !strings.Contains(ev.Build.Error, "compile_failed") {
		t.Fatalf("docker broken: %+v", ev.Build)
	}
	// Containers run without --rm so their state can be inspected; every one
	// must still be removed.
	out, err := exec.Command("docker", "ps", "-a", "--filter", "name=genrun-", "-q").Output()
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(out)) != "" {
		t.Fatalf("leaked genrun containers: %s", out)
	}
}

func TestPartialLimitsFillDefaults(t *testing.T) {
	got := Limits{BuildTimeout: time.Minute}.withDefaults()
	want := DefaultLimits()
	want.BuildTimeout = time.Minute
	if got != want {
		t.Fatalf("partial limits: got %+v want %+v", got, want)
	}
}

func TestCallerCancellationIsNeitherGeneratorNorSandbox(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := Local{Limits: testLimits()}.Build(ctx, fixture(t, "ok", false))
	if !errors.Is(err, context.Canceled) || errors.Is(err, ErrSandbox) {
		t.Fatalf("cancelled build: %v", err)
	}
	var gen *GeneratorError
	if errors.As(err, &gen) {
		t.Fatalf("cancellation blamed on the generator: %v", err)
	}
}
