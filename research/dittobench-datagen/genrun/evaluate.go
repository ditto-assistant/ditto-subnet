package genrun

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

// Plan is one generator evaluation's inputs.
type Plan struct {
	MinerHotkey string
	// Seeds are the scored seeds, in order.
	Seeds []string
	// ProbeSeed, when set, is generated and validated but never panel-run: its
	// case fingerprints let Platform compare generators on a shared seed.
	ProbeSeed string
	// Cases is the exact case count every pack must contain.
	Cases int
	// CheckDeterminism re-runs the first seed and requires identical bytes.
	CheckDeterminism bool
	// RoundBenchVersion is the bench version the round (or qualification) is
	// frozen at. Requests accept every grading policy through it
	// (challenge.NewGenerateRequestFor); zero, or a v13 round, sends exactly
	// the original v13 request.
	RoundBenchVersion int
}

// request is the plan's request for one seed.
func (p Plan) request(seed string) challenge.GenerateRequest {
	return challenge.NewGenerateRequestFor(seed, p.MinerHotkey, p.Cases, p.RoundBenchVersion)
}

// Validate checks every request the plan would send, so a request this
// scorer's pack contract refuses (for example cases above challenge.MaxCases)
// is reported as ErrInvalidRequest instead of a generator failure.
func (p Plan) Validate() error {
	if len(p.Seeds) == 0 {
		return fmt.Errorf("%w: plan has no seeds", ErrInvalidRequest)
	}
	seeds := p.Seeds
	if p.ProbeSeed != "" {
		seeds = append(append([]string(nil), p.Seeds...), p.ProbeSeed)
	}
	for _, seed := range seeds {
		if err := p.request(seed).Validate(); err != nil {
			return fmt.Errorf("%w: %v", ErrInvalidRequest, err)
		}
	}
	return nil
}

// SeedOutput is one generation run.
type SeedOutput struct {
	Seed       string           `json:"seed"`
	PackSHA256 string           `json:"pack_sha256,omitempty"`
	Report     challenge.Report `json:"validation"`
	// Error is the generator failure, when the run produced no pack.
	Error string `json:"error,omitempty"`
	// GradingBenchVersion is the grading policy the pack declares. It is set
	// only when the request accepted more than the baseline policy, so a v13
	// round's output is byte-identical to the original contract.
	GradingBenchVersion int `json:"grading_bench_version,omitempty"`
	// LegacyRetry records why the request accepting newer policies failed when
	// the output above is the retry on the original baseline request. A
	// generator built before newer policies existed (or one whose newer path
	// is broken) keeps its column at the baseline instead of being stranded.
	LegacyRetry string `json:"legacy_retry,omitempty"`
	DurationMS  int64  `json:"duration_ms"`
	// Pack is the raw output (never serialized; the scorer runs it).
	Pack []byte `json:"-"`
}

// Valid reports whether the run produced a contract-valid pack.
func (s SeedOutput) Valid() bool { return s.Error == "" && s.Report.Valid }

// BuildOutput is the build step's outcome.
type BuildOutput struct {
	OK           bool   `json:"ok"`
	BinarySHA256 string `json:"binary_sha256,omitempty"`
	Error        string `json:"error,omitempty"`
	DurationMS   int64  `json:"duration_ms"`
}

// Evaluation is the generator half of a scorer run.
type Evaluation struct {
	Sandbox string       `json:"sandbox"`
	Build   BuildOutput  `json:"build"`
	Seeds   []SeedOutput `json:"seeds"`
	Probe   *SeedOutput  `json:"probe,omitempty"`
	// Deterministic is nil when not checked (or the first seed failed).
	Deterministic *bool `json:"deterministic,omitempty"`
}

// Valid reports whether the build succeeded, every seed produced a valid pack,
// the probe (if any) is valid, and the determinism check (if run) passed.
func (e Evaluation) Valid() bool {
	if !e.Build.OK || len(e.Seeds) == 0 {
		return false
	}
	for _, s := range e.Seeds {
		if !s.Valid() {
			return false
		}
	}
	if e.Probe != nil && !e.Probe.Valid() {
		return false
	}
	return e.Deterministic == nil || *e.Deterministic
}

// Evaluate builds the generator in srcDir and runs the plan. Generator
// failures are recorded in the Evaluation; a returned error is always a
// request this scorer cannot serve (errors.Is(err, ErrInvalidRequest),
// checked before the build), a sandbox failure (errors.Is(err, ErrSandbox)),
// or a cancelled context. None of them is the generator's fault.
func Evaluate(ctx context.Context, sb Sandbox, srcDir string, plan Plan) (Evaluation, error) {
	ev := Evaluation{Sandbox: sb.Name()}
	if err := plan.Validate(); err != nil {
		return ev, err
	}
	start := time.Now()
	prog, err := sb.Build(ctx, srcDir)
	ev.Build.DurationMS = time.Since(start).Milliseconds()
	if err != nil {
		var gerr *GeneratorError
		if errors.As(err, &gerr) {
			ev.Build.Error = gerr.Error()
			return ev, nil
		}
		return ev, err
	}
	defer prog.Close()
	ev.Build.OK, ev.Build.BinarySHA256 = true, prog.SHA256()

	attempt := func(req challenge.GenerateRequest) (SeedOutput, error) {
		out := SeedOutput{Seed: req.Seed}
		if err := req.Validate(); err != nil {
			out.Error = err.Error()
			return out, nil
		}
		t := time.Now()
		raw, err := prog.Generate(ctx, req)
		out.DurationMS = time.Since(t).Milliseconds()
		if err != nil {
			var gerr *GeneratorError
			if errors.As(err, &gerr) {
				out.Error = gerr.Error()
				return out, nil
			}
			return out, err
		}
		out.Pack = raw
		out.PackSHA256 = challenge.Digest(raw)
		out.Report = challenge.ValidateGenerated(raw, req)
		if len(req.GradingBenchVersions) > 0 {
			if p, derr := challenge.Decode(raw); derr == nil {
				out.GradingBenchVersion = p.GradingBenchVersion
			}
		}
		return out, nil
	}
	// run generates one seed and returns the request whose output it reports,
	// so the determinism check repeats exactly that request.
	run := func(seed string) (SeedOutput, challenge.GenerateRequest, error) {
		req := plan.request(seed)
		out, err := attempt(req)
		// The retry requests only the baseline, so it runs only when the
		// round's window still accepts the baseline (GradingPolicyWindow).
		if err != nil || out.Valid() || len(req.GradingBenchVersions) == 0 || !req.AcceptsBaseline() {
			return out, req, err
		}
		legacy := challenge.NewGenerateRequest(seed, plan.MinerHotkey, plan.Cases)
		retry, err := attempt(legacy)
		if err != nil {
			return out, req, err
		}
		if !retry.Valid() {
			// Report the newer-policy attempt: the baseline did no better.
			return out, req, nil
		}
		reason := out.Error
		if reason == "" {
			reason = fmt.Sprintf("invalid pack (%d issues)", len(out.Report.Issues))
		}
		if len(reason) > 500 {
			reason = reason[:500]
		}
		retry.LegacyRetry = fmt.Sprintf("request accepting grading policies %v failed (%s); retried on the baseline v%d request", req.Accepted(), reason, legacy.GradingBenchVersion)
		retry.GradingBenchVersion = legacy.GradingBenchVersion
		return retry, legacy, nil
	}

	var firstReq challenge.GenerateRequest
	for i, seed := range plan.Seeds {
		out, req, err := run(seed)
		if err != nil {
			return ev, err
		}
		if i == 0 {
			firstReq = req
		}
		ev.Seeds = append(ev.Seeds, out)
	}
	if plan.ProbeSeed != "" {
		out, _, err := run(plan.ProbeSeed)
		if err != nil {
			return ev, err
		}
		out.Pack = nil
		ev.Probe = &out
	}
	if plan.CheckDeterminism && len(ev.Seeds) > 0 && ev.Seeds[0].Error == "" {
		again, err := attempt(firstReq)
		if err != nil {
			return ev, err
		}
		same := again.Error == "" && again.PackSHA256 == ev.Seeds[0].PackSHA256
		ev.Deterministic = &same
	}
	return ev, nil
}
