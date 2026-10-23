// Command challenge-scorer runs generator-competition challenge packs against
// panels of agent harnesses.
//
// Local rehearsal (a generator miner, or an operator spot-check):
//
//	challenge-scorer run -pack pack.json \
//	    -agent a1=http://127.0.0.1:7070,owner=me \
//	    -agent a2=http://127.0.0.1:7071,owner=other,champion \
//	    -out panel.json
//	challenge score -pack pack.json -panel panel.json   # from dittobench-datagen
//
// Each -agent must be a fresh harness or one that scopes memory by user_id;
// runs use per-run opaque user IDs either way.
//
// Platform mode (the trusted shadow scorer):
//
//	DITTO_GENERATOR_SCORER_TOKEN=... challenge-scorer serve \
//	    -platform https://platform.example -harness-map panel-harnesses.json
//
// serve leases one pack at a time from Platform, validates it with the
// contract's own Validate, runs every panel agent it has a harness for, and
// posts the validation report plus per-agent results. Platform computes the
// score. The control token is read from the environment, sent only as a
// Bearer header to the configured Platform URL, and never logged.
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
)

// ScorerVersion is reported with every result so Platform can audit which
// executor produced it.
const ScorerVersion = "challenge-scorer/1"

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

func runLocal(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	packPath := fs.String("pack", "", "challenge pack path (required)")
	generatorOwner := fs.String("generator-owner", "", "owner key of the pack's generator; its agents are excluded when scoring")
	out := fs.String("out", "", "write the panel verdict file here (default: stdout)")
	concurrency := fs.Int("concurrency", challengerun.DefaultConcurrency, "concurrent cases per wave")
	var agents agentFlags
	fs.Var(&agents, "agent", "panel agent id=url[,owner=key][,champion] (repeatable)")
	_ = fs.Parse(args)
	if *packPath == "" || len(agents) == 0 {
		return errors.New("run: -pack and at least one -agent are required")
	}
	// Local rehearsal targets loopback harnesses by design.
	runner.Configure(true)
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
	in := challenge.ScoreInput{GeneratorOwnerKey: *generatorOwner}
	for _, a := range agents {
		log.Printf("running %d cases against %s (%s)", len(pack.Cases), a.ID, a.URL)
		member := runMember(ctx, pack, a.ID, a.Owner, a.Champion, a.URL, *concurrency)
		in.Panel = append(in.Panel, member)
	}
	b, err := json.MarshalIndent(in, "", "  ")
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
	LeaseID    string `json:"lease_id"`
	PackID     string `json:"pack_id"`
	PackSHA256 string `json:"pack_sha256"`
	// PackBase64 carries the exact uploaded bytes; the pin is their digest.
	PackBase64 string      `json:"pack_base64"`
	Panel      []panelSlot `json:"panel"`
}

type panelSlot struct {
	AgentID  string `json:"agent_id"`
	OwnerKey string `json:"owner_key"`
	Champion bool   `json:"champion"`
}

type resultRequest struct {
	LeaseID       string                  `json:"lease_id"`
	PackID        string                  `json:"pack_id"`
	PackSHA256    string                  `json:"pack_sha256"`
	ScorerVersion string                  `json:"scorer_version"`
	Validation    challenge.Report        `json:"validation"`
	Panel         []challenge.PanelMember `json:"panel"`
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
	payload, err := io.ReadAll(io.LimitReader(resp.Body, challenge.MaxPackBytes*2))
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
	once := fs.Bool("once", false, "process at most one pack and exit")
	allowPrivate := fs.Bool("allow-private-harness", false, "allow loopback/private harness URLs (local stacks)")
	concurrency := fs.Int("concurrency", challengerun.DefaultConcurrency, "concurrent cases per wave")
	_ = fs.Parse(args)
	token := strings.TrimSpace(os.Getenv(tokenEnv))
	if *platform == "" || *harnessMap == "" || token == "" {
		return fmt.Errorf("serve: -platform, -harness-map, and %s are required", tokenEnv)
	}
	runner.Configure(*allowPrivate)
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
		processed, err := processOne(ctx, client, harnesses, *concurrency)
		if err != nil {
			log.Printf("scorer cycle failed: %v", err)
		}
		if *once || ctx.Err() != nil {
			return nil
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

// processOne leases and scores one pack. It reports whether a pack was leased.
func processOne(ctx context.Context, client *platformClient, harnesses map[string]string, concurrency int) (bool, error) {
	var work workResponse
	if err := client.do(ctx, http.MethodGet, "/api/v1/generator-scorer/work", nil, &work); err != nil {
		return false, err
	}
	if work.Work == nil {
		return false, nil
	}
	item := work.Work
	result := resultRequest{LeaseID: item.LeaseID, PackID: item.PackID, PackSHA256: item.PackSHA256, ScorerVersion: ScorerVersion}
	raw, err := base64.StdEncoding.DecodeString(item.PackBase64)
	if err != nil {
		return true, fmt.Errorf("pack %s: pack_base64 is not base64", item.PackID)
	}
	if got := challenge.Digest(raw); got != item.PackSHA256 {
		// Platform pinned a different identity than the bytes it served:
		// refuse rather than score bytes nobody signed.
		return true, fmt.Errorf("pack %s: served bytes hash to %s, lease pins %s", item.PackID, got, item.PackSHA256)
	}
	result.Validation = challenge.ValidateBytes(raw)
	if result.Validation.Valid {
		pack, err := challenge.Decode(raw)
		if err != nil {
			return true, err
		}
		for _, slot := range item.Panel {
			url, ok := harnesses[slot.AgentID]
			if !ok {
				log.Printf("pack %s: no provisioned harness for panel agent %s; skipping", item.PackID, slot.AgentID)
				continue
			}
			result.Panel = append(result.Panel, runMember(ctx, pack, slot.AgentID, slot.OwnerKey, slot.Champion, url, concurrency))
		}
	}
	if ctx.Err() != nil {
		return true, ctx.Err()
	}
	log.Printf("pack %s: valid=%t panel=%d", item.PackID, result.Validation.Valid, len(result.Panel))
	return true, client.do(ctx, http.MethodPost, "/api/v1/generator-scorer/results", result, nil)
}
