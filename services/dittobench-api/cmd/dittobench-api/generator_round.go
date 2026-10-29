package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/challengerun"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// generatorRoundMode runs a generator-competition round dataset against one
// screened agent image. The validator receives it on a Platform-issued
// generator-round ticket and posts the per-case results to Platform's round
// endpoint; the report never enters the agent score ledger.
const generatorRoundMode = "generator-round-v1"

// generatorRoundMaxCasesFeature advertises the largest round column (cases
// per pack) this scorer runs: the pack contract's challenge.MaxCases. The
// validator forwards it to Platform as generator_round_max_cases, and Platform
// leases a round only to validators whose scorer can run its column size. A
// scorer without the feature is assumed to run the legacy 32-case column.
func generatorRoundMaxCasesFeature() string {
	return fmt.Sprintf("generator-round-max-cases-%d", challenge.MaxCases)
}

// generatorRoundBenchFeaturePrefix names one bench version this scorer runs a
// round at: generator-round-bench-<n> for every
// challenge.SupportedGradingBenchVersions entry. The validator forwards the set
// to Platform as generator_round_bench_versions, and Platform leases a round
// frozen at version n only to a validator whose scorer advertises n. A scorer
// without any of these features runs only v13 rounds (the original mode).
const generatorRoundBenchFeaturePrefix = "generator-round-bench-"

// generatorRoundBenchFeatures advertises every round bench version.
func generatorRoundBenchFeatures() []string {
	versions := challenge.SupportedGradingBenchVersions()
	out := make([]string, 0, len(versions))
	for _, v := range versions {
		out = append(out, fmt.Sprintf("%s%d", generatorRoundBenchFeaturePrefix, v))
	}
	return out
}

// runsGeneratorRoundAt reports whether this scorer runs a round frozen at v: a
// round at v runs packs graded at any supported policy through v, and v itself
// must be a supported policy so the round's newest columns can use it.
func runsGeneratorRoundAt(v int) bool {
	return challenge.SupportsGradingBenchVersion(v)
}

func validateGeneratorRoundRequest(req submitRequest, enabled bool) error {
	if !enabled || !runsGeneratorRoundAt(req.BenchVersion) {
		return errors.New("generator round mode unavailable")
	}
	if len(req.PrivateDatasetBytes) == 0 || len(req.PrivateDatasetBytes) > challenge.MaxRoundDatasetBytes || !canonicalSHA256(req.ExpectedDatasetSHA256) {
		return errors.New("generator round dataset bytes and canonical digest required")
	}
	return nil
}

// runGeneratorRoundJob executes every column of a round dataset against one
// sandboxed screened image and finishes the run with per-case memory scores.
//
// It reuses the scored path's primitives end to end: the same screened-image
// build and sandbox, the same ticket inference capability and embedding phase,
// the same relay accounting, and challengerun (which seeds and grades exactly
// like a benchmark memory case at each pack's declared grading policy). It skips what
// only the canonical benchmark needs: dataset regeneration, tool cases, the
// harness projection, and v9 base evidence. Case IDs in the report are
// challenge.RoundCaseID values; a case the harness never answered is reported
// under challenge.RoundFaultCategory and excluded by the round scorer.
func (s *server) runGeneratorRoundJob(ctx context.Context, runID string, req submitRequest, seed int64) {
	defer func() { <-s.runSlots }()
	defer s.unregisterRunCancel(runID)
	if req.InferenceSessionID != "" {
		defer s.broker.removeRun(req.InferenceSessionID, runID)
	}
	defer func() {
		if rec := recover(); rec != nil {
			s.store.Fail(runID, "internal panic during generator round job")
			log.Printf("generator round job %s panicked: %v", runID, rec)
		}
	}()

	_, columns, err := challenge.DecodeRoundDataset(req.PrivateDatasetBytes, req.ExpectedDatasetSHA256)
	if err != nil {
		s.store.Fail(runID, "generator round dataset integrity verification failed")
		return
	}
	// Each column is graded at the policy its pack declares; a round frozen at
	// v accepts only policies through v.
	if err := challenge.CheckRoundGradingVersions(columns, req.BenchVersion); err != nil {
		s.store.Fail(runID, err.Error())
		return
	}
	total := 0
	for _, col := range columns {
		total += len(col.Pack.Cases)
	}
	s.store.Update(runID, func(j *store.Job) { j.N = total })

	s.store.SetStage(runID, store.StatusBuilding, 0, total)
	image, buildLog, _, err := s.sandbox.Build(ctx, sourceFromReq(req))
	if err != nil {
		s.store.FailWith(runID, "build failed: "+err.Error(), screenedImageInfraFailure(err))
		log.Printf("generator round %s build failed: %v\n%s", runID, err, buildLog)
		return
	}
	defer s.sandbox.Release(context.Background(), image)

	sessionID := req.InferenceSessionID
	var handle *sandbox.Handle
	var sourceCapability string
	strictCleanupOnly := false
	defer func() {
		if handle == nil {
			return
		}
		if sourceCapability != "" {
			_ = s.broker.revokeSourceCapability(sessionID, runID, sourceCapability)
		}
		if strictCleanupOnly {
			_ = s.cleanupPartialSandboxStart(handle, image)
			return
		}
		s.finishSandboxRun(runID, handle)
	}()
	env := harnessSandboxEnv(req.Env, req.BenchVersion, sessionID)
	if sessionID != "" {
		if sourceCapability, err = s.broker.installSourceCapability(sessionID, runID); err != nil {
			s.store.Fail(runID, "inference session capability is unavailable")
			return
		}
		env, err = harnessSandboxEnvWithCapability(req.Env, req.BenchVersion, platformLockedProvider, sessionID, sourceCapability)
		if err != nil {
			_ = s.broker.revokeSourceCapability(sessionID, runID, sourceCapability)
			sourceCapability = ""
			s.store.Fail(runID, "inference session capability is unavailable")
			return
		}
		s.broker.setHarnessBase(sessionID, env["DITTOBENCH_INFERENCE_BASE_URL"])
	}
	gateway := env["DITTOBENCH_INFERENCE_BASE_URL"]
	handle, err = s.sandbox.Run(req.BenchmarkRuntime.sandboxContext(ctx), image, env)
	if err != nil {
		if sourceCapability != "" {
			_ = s.broker.revokeSourceCapability(sessionID, runID, sourceCapability)
			sourceCapability = ""
		}
		if handle != nil {
			if cleanupErr := s.cleanupPartialSandboxStart(handle, image); cleanupErr != nil {
				strictCleanupOnly = true
				s.store.Fail(runID, "partial container start isolation unavailable")
				return
			}
			handle = nil
		}
		s.store.FailWith(runID, "container start failed: "+err.Error(), sandboxStartInfraFailure(err))
		return
	}
	if sessionID != "" && !s.broker.bindSourceCapability(sessionID, runID, handle.SourceIP, sourceCapability) {
		s.store.Fail(runID, "inference session is unavailable")
		return
	}
	ctx = runner.TrustSandbox(ctx)
	if err := s.waitSandboxHealthy(ctx, handle, sandboxHealthTimeout); err != nil {
		s.store.Fail(runID, "harness never became healthy: "+err.Error())
		return
	}

	endMemoryPhase, admitted := s.beginMemoryPhase(ctx, sessionID, runID, false)
	if !admitted {
		if ctx.Err() == nil {
			s.store.Fail(runID, "embedding phase admission failed")
		}
		return
	}
	defer endMemoryPhase()
	relayStart, ok := s.relayRunStart(ctx, runID, req.BenchVersion, req.RunSize, harnessGateway(sessionID), sessionID)
	if !ok {
		return
	}
	concurrency := caseConcurrencyForRun(req.BenchmarkRuntime)
	if sessionID != "" && !s.broker.configureCaseConcurrency(sessionID, runID, concurrency) {
		s.store.FailWith(runID, "ticket inference session is unavailable", toolEndpointInfrastructureFailure())
		return
	}

	// A pack case is a memory question, but it reaches the harness exactly as
	// a scored memory case does: with the run's seeded tool catalog and an
	// observed tool_endpoint. A harness built for the scored path rejects a
	// /run without them. Each case's fixture is registered just before its
	// /run, under the opaque ID the harness sees.
	tools := catalog.CatalogForSeed(req.BenchVersion, seed)
	toolSrv := toolexec.NewServer()
	toolEndpoint, stopToolSrv, err := s.startToolServerForSession(
		toolSrv, handle.SourceIP, req.BenchVersion, sessionID, concurrency,
	)
	if err != nil {
		s.store.FailWith(runID, "tool endpoint start failed: "+err.Error(), toolEndpointInfrastructureFailure())
		return
	}
	defer stopToolSrv()
	if !toolEndpoint.available() {
		s.store.FailWith(runID, "generator round cannot observe tool execution: tool_endpoint unreachable from the harness", toolEndpointInfrastructureFailure())
		return
	}
	prepareCase := func(packCaseID, opaqueCaseID, opaqueUserID string) string {
		toolSrv.Register(opaqueCaseID, toolexec.BuildFixtureForVersion(seed, protocol.ToolCase{ID: packCaseID}, req.BenchVersion))
		return toolEndpoint.forCase(opaqueCaseID, opaqueUserID)
	}

	s.store.SetStage(runID, store.StatusRunning, 0, total)
	started := time.Now()
	perCase := make([]protocol.CaseScore, 0, total)
	for _, col := range columns {
		outcomes, execErr := challengerun.Execute(ctx, handle.BaseURL, col.Pack, challengerun.Options{
			Concurrency: concurrency,
			InferenceBaseURL: func(opaqueCaseID string) string {
				return v13CaseInferenceBaseURL(req.BenchVersion, gateway, opaqueCaseID)
			},
			// The agent runs at the round's bench version; each case is
			// graded at its pack's declared policy.
			BenchVersion:  req.BenchVersion,
			Tools:         tools,
			ToolEndpoint:  prepareCase,
			ObservedCalls: toolSrv.Observed,
		})
		if execErr != nil {
			if errors.Is(execErr, challengerun.ErrHarnessUnavailable) {
				s.store.FailWith(runID, fmt.Sprintf("column %s: %v", col.ColumnID, execErr), toolEndpointInfrastructureFailure())
			} else if ctx.Err() == nil {
				s.store.Fail(runID, fmt.Sprintf("column %s execution failed", col.ColumnID))
			}
			return
		}
		for _, o := range outcomes {
			category := challenge.RoundCaseCategory
			if o.Fault {
				category = challenge.RoundFaultCategory
			}
			perCase = append(perCase, protocol.CaseScore{
				CaseID:   challenge.RoundCaseID(col.ColumnID, o.CaseID),
				Category: category,
				Kind:     "memory",
				Score:    o.Score,
			})
		}
		s.store.SetStage(runID, store.StatusRunning, len(perCase), total)
	}
	endMemoryPhase()

	tokenUsage, _, ok := s.relayRunResult(ctx, runID, relayStart, harnessGateway(sessionID), sessionID)
	if !ok {
		return
	}
	s.store.SetStage(runID, store.StatusScoring, len(perCase), total)
	report := generatorRoundReport(runID, seed, req, perCase, tokenUsage)
	s.store.Finish(runID, report)
	log.Printf("generator round %s done: columns=%d cases=%d memory_mean=%.3f elapsed=%s", runID, len(columns), len(perCase), report.MemoryMean, time.Since(started).Round(time.Second))
}

// generatorRoundReport shapes round results as a score report. Composite is
// the plain mean over answered cases and is informational only: the round
// matrix is scored from per_case.
func generatorRoundReport(runID string, seed int64, req submitRequest, perCase []protocol.CaseScore, usage protocol.TokenUsage) protocol.ScoreReport {
	var sum float64
	answered := 0
	for _, c := range perCase {
		if c.Category == challenge.RoundFaultCategory {
			continue
		}
		sum += c.Score
		answered++
	}
	mean := 0.0
	if answered > 0 {
		mean = sum / float64(answered)
	}
	report := protocol.ScoreReport{
		RunID:        runID,
		Seed:         seed,
		GeneratedAt:  time.Now().UTC().Format(time.RFC3339),
		Composite:    mean,
		RawComposite: mean,
		MemoryMean:   mean,
		// Per-case latency is not measured on this path; zero means unknown,
		// not instant.
		MedianMs: 0,
		PerCase:  perCase,
		Details: &protocol.RunDetails{
			BenchVersion:  req.BenchVersion,
			RunSize:       req.RunSize,
			DatasetSHA256: req.ExpectedDatasetSHA256,
			MemoryMean:    mean,
			TokenUsage:    &usage,
		},
	}
	return report
}
