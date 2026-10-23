// Command challenge-scorer evaluates generator-competition submissions: it
// builds a generator's source in a no-network sandbox, runs it on seeds, and
// runs every resulting challenge pack against a panel of agent harnesses.
//
// Local rehearsal (a generator miner, or an operator spot-check). Generators
// build and run in the Docker sandbox unless -sandbox local opts out:
//
//	challenge-scorer run -dir ./my-generator -hotkey 5Grw... -seeds 2 -cases 16 \
//	    -agent a1=http://127.0.0.1:7070,owner=me \
//	    -agent a2=http://127.0.0.1:7071,owner=other,champion \
//	    -generator-owner me -out run.json
//
// prints the run's score with the default parameters and writes the full
// verdict file (a challenge.RunInput plus its challenge.RunScore). -pack runs a
// single pack file instead of a generator. Each -agent must be a fresh harness
// or one that scopes memory by user_id; runs use per-run opaque user IDs.
//
// Platform mode (the trusted shadow scorer):
//
//	DITTO_GENERATOR_SCORER_TOKEN=... challenge-scorer serve \
//	    -platform https://platform.example -harness-map panel-harnesses.json
//
// serve leases one evaluation at a time: Platform serves the exact uploaded
// source archive, fresh secret seeds, the current probe seed, and a panel
// snapshot. The scorer refuses bytes that do not hash to the pin, builds and
// runs the generator in the Docker sandbox (genrun.Docker), validates every
// pack against its request, checks determinism, runs each valid pack against
// every panel agent it has a harness for, and posts the evaluation. Platform
// computes the score. A sandbox failure is never posted as a generator
// failure: the lease simply expires and the run is retried. The control token
// is read from the environment, sent only as a Bearer header to the configured
// Platform URL, and never logged.
package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/challengerun"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

// ScorerVersion is reported with every result so Platform can audit which
// executor produced it.
const ScorerVersion = "challenge-scorer/2"

const tokenEnv = "DITTO_GENERATOR_SCORER_TOKEN"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var err error
	switch os.Args[1] {
	case "run":
		err = runLocal(ctx, os.Args[2:])
	case "serve":
		err = serve(ctx, os.Args[2:])
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		log.Fatalf("challenge-scorer: %v", err)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: challenge-scorer run|serve [flags]")
}

// agentFlag is one -agent id=url[,owner=key][,champion] value.
type agentFlag struct {
	ID, URL, Owner string
	Champion       bool
}

type agentFlags []agentFlag

func (a *agentFlags) String() string { return fmt.Sprint(len(*a)) }

func (a *agentFlags) Set(v string) error {
	parsed, err := parseAgentFlag(v)
	if err != nil {
		return err
	}
	*a = append(*a, parsed)
	return nil
}

func parseAgentFlag(v string) (agentFlag, error) {
	parts := strings.Split(v, ",")
	id, url, ok := strings.Cut(parts[0], "=")
	if !ok || id == "" || url == "" {
		return agentFlag{}, fmt.Errorf("agent %q must be id=url[,owner=key][,champion]", v)
	}
	out := agentFlag{ID: id, URL: strings.TrimRight(url, "/"), Owner: id}
	for _, p := range parts[1:] {
		switch {
		case p == "champion":
			out.Champion = true
		case strings.HasPrefix(p, "owner="):
			out.Owner = strings.TrimPrefix(p, "owner=")
		default:
			return agentFlag{}, fmt.Errorf("agent %q: unknown option %q", v, p)
		}
	}
	return out, nil
}

func sandboxFor(name string) (genrun.Sandbox, error) {
	switch name {
	case "docker":
		return genrun.Docker{}, nil
	case "local":
		return genrun.Local{}, nil
	}
	return nil, fmt.Errorf("unknown sandbox %q (docker or local)", name)
}

func runLocal(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	dir := fs.String("dir", "", "generator source directory")
	archive := fs.String("archive", "", "generator source archive")
	packPath := fs.String("pack", "", "run one challenge pack file instead of a generator")
	hotkey := fs.String("hotkey", "", "SS58 hotkey the generator's packs are bound to")
	seeds := fs.Int("seeds", 2, "seeds to generate (1..8)")
	firstSeed := fs.Int64("seed", 1, "first rehearsal seed (challenge.SeedFromInt64)")
	cases := fs.Int("cases", 16, "cases per pack")
	sandbox := fs.String("sandbox", "docker", "docker (isolated) or local (runs generator code on this host)")
	generatorOwner := fs.String("generator-owner", "", "owner key of the generator; its agents are excluded when scoring")
	out := fs.String("out", "", "write the verdict file here (default: stdout)")
	concurrency := fs.Int("concurrency", challengerun.DefaultConcurrency, "concurrent cases per wave")
	var agents agentFlags
	fs.Var(&agents, "agent", "panel agent id=url[,owner=key][,champion] (repeatable)")
	_ = fs.Parse(args)
	sources := 0
	for _, v := range []string{*dir, *archive, *packPath} {
		if v != "" {
			sources++
		}
	}
	if sources != 1 || len(agents) == 0 {
		return errors.New("run: exactly one of -dir/-archive/-pack and at least one -agent are required")
	}
	// Local rehearsal targets loopback harnesses by design.
	runner.Configure(true)
	slots := make([]panelSlot, len(agents))
	urls := map[string]string{}
	for i, a := range agents {
		slots[i] = panelSlot{AgentID: a.ID, OwnerKey: a.Owner, Champion: a.Champion}
		if _, dup := urls[a.ID]; dup {
			return fmt.Errorf("run: duplicate -agent id %q", a.ID)
		}
		urls[a.ID] = a.URL
	}

	var packs []seedPack
	if *packPath != "" {
		raw, err := os.ReadFile(*packPath)
		if err != nil {
			return err
		}
		pack, err := challenge.Decode(raw)
		if err != nil {
			return err
		}
		if r := challenge.Validate(pack); !r.Valid {
			return fmt.Errorf("pack is invalid (%d issues); run `challenge validate`", len(r.Issues))
		}
		packs = append(packs, seedPack{seed: pack.Generator.Seed, pack: pack})
	} else {
		if *hotkey == "" || *seeds < 1 || *seeds > 8 {
			return errors.New("run: -hotkey and -seeds 1..8 are required with -dir/-archive")
		}
		sb, err := sandboxFor(*sandbox)
		if err != nil {
			return err
		}
		var raw []byte
		if *archive != "" {
			raw, err = os.ReadFile(*archive)
		} else {
			raw, _, err = genrun.PackDir(ctx, *dir)
		}
		if err != nil {
			return err
		}
		plan := genrun.Plan{MinerHotkey: *hotkey, Cases: *cases, CheckDeterminism: true}
		for i := 0; i < *seeds; i++ {
			plan.Seeds = append(plan.Seeds, challenge.SeedFromInt64(*firstSeed+int64(i)))
		}
		ev, err := evaluateArchive(ctx, sb, raw, plan)
		if err != nil {
			return err
		}
		if !ev.Valid() {
			b, _ := json.MarshalIndent(ev, "", "  ")
			return fmt.Errorf("generator is invalid; rehearse it with `challenge rehearse`:\n%s", b)
		}
		packs, err = decodePacks(ev)
		if err != nil {
			return err
		}
	}

	in := challenge.RunInput{GeneratorOwnerKey: *generatorOwner}
	for _, sp := range packs {
		log.Printf("seed %.12s…: running %d cases against %d agents", sp.seed, len(sp.pack.Cases), len(slots))
		in.Seeds = append(in.Seeds, challenge.SeedPanel{
			Seed:  sp.seed,
			Cases: sp.pack.Refs(),
			Panel: runPanel(ctx, sp.pack, slots, urls, *concurrency, "local"),
		})
	}
	params := challenge.DefaultScoreParams()
	params.TargetCases = len(packs[0].pack.Cases)
	score, err := challenge.ScoreRun(in, params)
	if err != nil {
		return err
	}
	log.Printf("run %s: score %.4f (%d seeds)", score.Status, score.Score, len(score.Seeds))
	b, err := json.MarshalIndent(struct {
		Input  challenge.RunInput    `json:"input"`
		Params challenge.ScoreParams `json:"params"`
		Score  challenge.RunScore    `json:"score"`
	}{in, params, score}, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	if *out == "" {
		_, err = os.Stdout.Write(b)
		return err
	}
	return os.WriteFile(*out, b, 0o644)
}

type seedPack struct {
	seed string
	pack challenge.Pack
}

// evaluateArchive extracts a source archive into a scratch directory and runs
// the generator half of an evaluation there. An archive the contract rejects is
// the generator's failure (recorded as a failed build); only sandbox trouble is
// returned as an error.
func evaluateArchive(ctx context.Context, sb genrun.Sandbox, raw []byte, plan genrun.Plan) (genrun.Evaluation, error) {
	dir, err := os.MkdirTemp("", "challenge-scorer-src-*")
	if err != nil {
		return genrun.Evaluation{}, fmt.Errorf("%w: %v", genrun.ErrSandbox, err)
	}
	defer os.RemoveAll(dir)
	if _, err := challenge.ExtractArchive(raw, dir); err != nil {
		return genrun.Evaluation{Sandbox: sb.Name(), Build: genrun.BuildOutput{Error: "archive: " + err.Error()}}, nil
	}
	return genrun.Evaluate(ctx, sb, dir, plan)
}

func decodePacks(ev genrun.Evaluation) ([]seedPack, error) {
	out := make([]seedPack, 0, len(ev.Seeds))
	for _, s := range ev.Seeds {
		pack, err := challenge.Decode(s.Pack)
		if err != nil {
			return nil, err
		}
		out = append(out, seedPack{seed: s.Seed, pack: pack})
	}
	return out, nil
}

// runPanel runs one pack against every panel slot that has a harness.
func runPanel(ctx context.Context, pack challenge.Pack, slots []panelSlot, harnesses map[string]string, concurrency int, label string) []challenge.PanelMember {
	var members []challenge.PanelMember
	for _, slot := range slots {
		url, ok := harnesses[slot.AgentID]
		if !ok {
			log.Printf("%s: no provisioned harness for panel agent %s; skipping", label, slot.AgentID)
			continue
		}
		members = append(members, runMember(ctx, pack, slot.AgentID, slot.OwnerKey, slot.Champion, url, concurrency))
	}
	return members
}

// runMember executes the pack against one harness. A harness that cannot be
// reached or seeded faults every case: absence of evidence is never a miss.
func runMember(ctx context.Context, pack challenge.Pack, id, owner string, champion bool, url string, concurrency int) challenge.PanelMember {
	m := challenge.PanelMember{AgentID: id, OwnerKey: owner, Champion: champion}
	if err := runner.WaitHealthy(ctx, url, 2*time.Minute); err != nil {
		log.Printf("agent %s: harness never became healthy; faulting all cases", id)
		return faultAll(m, pack)
	}
	outcomes, err := challengerun.Execute(ctx, url, pack, challengerun.Options{Concurrency: concurrency})
	if err != nil {
		log.Printf("agent %s: %v; faulting all cases", id, err)
		return faultAll(m, pack)
	}
	for _, o := range outcomes {
		m.Results = append(m.Results, o.PanelCaseResult)
	}
	return m
}

func faultAll(m challenge.PanelMember, pack challenge.Pack) challenge.PanelMember {
	for _, c := range pack.Cases {
		m.Results = append(m.Results, challenge.PanelCaseResult{CaseID: c.ID, Fault: true})
	}
	return m
}

// Platform scorer API. Mirrors apps/platform/ditto/api_models/generator.py.

type workResponse struct {
	Work *workItem `json:"work"`
}

type workItem struct {
	LeaseID      string `json:"lease_id"`
	RunID        string `json:"run_id"`
	SubmissionID string `json:"submission_id"`
	MinerHotkey  string `json:"miner_hotkey"`
	SourceSHA256 string `json:"source_sha256"`
	// SourceBase64 carries the exact uploaded archive; the pin is its digest.
	SourceBase64 string      `json:"source_base64"`
	Seeds        []string    `json:"seeds"`
	ProbeSeed    string      `json:"probe_seed"`
	CasesPerSeed int         `json:"cases_per_seed"`
	Panel        []panelSlot `json:"panel"`
}

type panelSlot struct {
	AgentID  string `json:"agent_id"`
	OwnerKey string `json:"owner_key"`
	Champion bool   `json:"champion"`
}

type seedResult struct {
	genrun.SeedOutput
	Panel []challenge.PanelMember `json:"panel"`
}

type resultRequest struct {
	LeaseID       string             `json:"lease_id"`
	RunID         string             `json:"run_id"`
	SubmissionID  string             `json:"submission_id"`
	SourceSHA256  string             `json:"source_sha256"`
	ScorerVersion string             `json:"scorer_version"`
	Sandbox       string             `json:"sandbox"`
	Build         genrun.BuildOutput `json:"build"`
	Deterministic *bool              `json:"deterministic"`
	Seeds         []seedResult       `json:"seeds"`
	Probe         *genrun.SeedOutput `json:"probe"`
}

type platformClient struct {
	base  string
	token string
	http  *http.Client
}

func (c *platformClient) do(ctx context.Context, method, path string, body, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, challenge.MaxArchiveBytes*2))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("%s %s: HTTP %d", method, path, resp.StatusCode)
	}
	if out != nil {
		return json.Unmarshal(payload, out)
	}
	return nil
}

func serve(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	platform := fs.String("platform", "", "Platform base URL (required)")
	harnessMap := fs.String("harness-map", "", `JSON object {"<agent_id>": "<harness url>"} for provisioned panel agents (required)`)
	interval := fs.Duration("interval", 30*time.Second, "idle poll interval")
	once := fs.Bool("once", false, "process at most one evaluation and exit")
	allowPrivate := fs.Bool("allow-private-harness", false, "allow loopback/private harness URLs (local stacks)")
	unsandboxed := fs.Bool("unsafe-local-sandbox", false, "build and run generators on the host with no isolation (local stacks only)")
	concurrency := fs.Int("concurrency", challengerun.DefaultConcurrency, "concurrent cases per wave")
	insecure := fs.Bool("insecure-platform", false, "allow an http:// Platform URL (local stacks only; the token travels in clear text)")
	_ = fs.Parse(args)
	token := strings.TrimSpace(os.Getenv(tokenEnv))
	if *platform == "" || *harnessMap == "" || token == "" {
		return fmt.Errorf("serve: -platform, -harness-map, and %s are required", tokenEnv)
	}
	if *interval <= 0 {
		return errors.New("serve: -interval must be positive")
	}
	if !strings.HasPrefix(strings.ToLower(*platform), "https://") && !*insecure {
		return errors.New("serve: -platform must be an https URL (the scorer token is a bearer credential); pass -insecure-platform only for a local stack")
	}
	runner.Configure(*allowPrivate)
	var sb genrun.Sandbox = genrun.Docker{}
	if *unsandboxed {
		log.Printf("WARNING: -unsafe-local-sandbox runs untrusted generator code on this host")
		sb = genrun.Local{}
	}
	harnesses := map[string]string{}
	raw, err := os.ReadFile(*harnessMap)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(raw, &harnesses); err != nil {
		return fmt.Errorf("harness map: %w", err)
	}
	client := &platformClient{base: strings.TrimRight(*platform, "/"), token: token, http: &http.Client{Timeout: 60 * time.Second}}
	for {
		processed, err := processOne(ctx, client, sb, harnesses, *concurrency)
		if err != nil {
			log.Printf("scorer cycle failed: %v", err)
		}
		if ctx.Err() != nil {
			return nil
		}
		if *once {
			// One-shot automation must see a failed cycle as a failure.
			return err
		}
		if processed && err == nil {
			continue
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(*interval):
		}
	}
}

// processOne leases and evaluates one generator run. It reports whether work
// was leased. Returning an error without posting leaves the lease to expire,
// which is the only correct response to a scorer-side failure.
func processOne(ctx context.Context, client *platformClient, sb genrun.Sandbox, harnesses map[string]string, concurrency int) (bool, error) {
	var work workResponse
	if err := client.do(ctx, http.MethodGet, "/api/v1/generator-scorer/work", nil, &work); err != nil {
		return false, err
	}
	if work.Work == nil {
		return false, nil
	}
	item := work.Work
	raw, err := base64.StdEncoding.DecodeString(item.SourceBase64)
	if err != nil {
		return true, fmt.Errorf("run %s: source_base64 is not base64", item.RunID)
	}
	if got := challenge.Digest(raw); got != item.SourceSHA256 {
		// Platform pinned a different identity than the bytes it served:
		// refuse rather than run code nobody signed.
		return true, fmt.Errorf("run %s: served source hashes to %s, lease pins %s", item.RunID, got, item.SourceSHA256)
	}
	plan := genrun.Plan{
		MinerHotkey:      item.MinerHotkey,
		Seeds:            item.Seeds,
		ProbeSeed:        item.ProbeSeed,
		Cases:            item.CasesPerSeed,
		CheckDeterminism: true,
	}
	ev, err := evaluateArchive(ctx, sb, raw, plan)
	if err != nil {
		return true, fmt.Errorf("run %s: %w", item.RunID, err)
	}
	result := resultRequest{
		LeaseID: item.LeaseID, RunID: item.RunID, SubmissionID: item.SubmissionID,
		SourceSHA256: item.SourceSHA256, ScorerVersion: ScorerVersion,
		Sandbox: ev.Sandbox, Build: ev.Build, Deterministic: ev.Deterministic, Probe: ev.Probe,
	}
	// Only a fully valid generator meets the panel: one bad seed rejects the
	// run, so panel time is never spent on a generator that will score zero.
	valid := ev.Valid()
	for _, s := range ev.Seeds {
		sr := seedResult{SeedOutput: s}
		if valid {
			pack, err := challenge.Decode(s.Pack)
			if err != nil {
				return true, err
			}
			sr.Panel = runPanel(ctx, pack, item.Panel, harnesses, concurrency, "run "+item.RunID)
		}
		result.Seeds = append(result.Seeds, sr)
	}
	if ctx.Err() != nil {
		return true, ctx.Err()
	}
	log.Printf("run %s: build=%t valid=%t seeds=%d panel=%d", item.RunID, ev.Build.OK, valid, len(result.Seeds), len(item.Panel))
	return true, client.do(ctx, http.MethodPost, "/api/v1/generator-scorer/results", result, nil)
}
