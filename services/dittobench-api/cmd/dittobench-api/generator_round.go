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
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// generatorRoundMode runs a generator-competition round dataset against one
// screened agent image. The validator receives it on a Platform-issued
// generator-round ticket and posts the per-case results to Platform's round
// endpoint; the report never enters the agent score ledger.
const generatorRoundMode = "generator-round-v1"

func validateGeneratorRoundRequest(req submitRequest, enabled bool) error {
	if !enabled || req.BenchVersion != protocol.BenchVersionV13 {
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
// like a benchmark memory case at the round's grading version). It skips what
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
	total := 0
	for _, col := range columns {
		total += len(col.Pack.Cases)
	}

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

	s.store.SetStage(runID, store.StatusRunning, 0, total)
	started := time.Now()
	perCase := make([]protocol.CaseScore, 0, total)
	for _, col := range columns {
		outcomes, execErr := challengerun.Execute(ctx, handle.BaseURL, col.Pack, challengerun.Options{
			Concurrency: concurrency,
			InferenceBaseURL: func(opaqueCaseID string) string {
				return v13CaseInferenceBaseURL(req.BenchVersion, gateway, opaqueCaseID)
			},
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
