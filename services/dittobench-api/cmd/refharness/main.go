// Command refharness is a DETERMINISTIC reference harness for DittoBench.
//
// It implements the miner-harness wire contract (GET /health, POST /seed,
// POST /run) with a fixed, no-LLM, no-randomness policy: for each /run it routes
// to the catalog tool whose name+description keywords best overlap the prompt,
// emitting one call (or none, for prompts that match nothing). Because the
// policy is a pure function of the prompt, the ONLY thing that varies run to run
// is the dataset — which is exactly what the calibration harness needs to
// measure dataset *difficulty* without model or judge noise. It also serves as a
// zero-dependency harness for a single end-to-end smoke run (no OpenRouter key).
//
// Routing traps (categories whose surface wording points at the wrong tool) are
// missed by design, so the score reflects genuine per-dataset difficulty.
//
// Usage: refharness [-port 9000]
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"strconv"

	"github.com/ditto-assistant/dittobench-api/internal/pprofserver"
	"github.com/ditto-assistant/dittobench-api/internal/refharness"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// fixedLatencyMs is reported for every case so median latency is deterministic.
const fixedLatencyMs = 1

func main() {
	port := flag.Int("port", 9000, "HTTP listen port")
	flag.Parse()
	pprofserver.Start(context.Background(), "refharness", *port)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /seed", handleSeed)
	mux.HandleFunc("POST /run", handleRun)

	addr := ":" + strconv.Itoa(*port)
	log.Printf("refharness (deterministic reference) listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

// handleSeed is a no-op store: it accepts the haystack and echoes the counts.
// The reference policy holds no memory, so memory cases grade as not-recalled —
// fine for the tool-only direct path and a stable floor for full.
func handleSeed(w http.ResponseWriter, r *http.Request) {
	var req protocol.SeedRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad seed body"})
		return
	}
	writeJSON(w, http.StatusOK, protocol.SeedResponse{
		Pairs:    len(req.Pairs),
		Subjects: len(req.Subjects),
		Links:    len(req.Links),
	})
}

func handleRun(w http.ResponseWriter, r *http.Request) {
	var req protocol.RunRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad run body"})
		return
	}
	calls := refharness.Route(req.UserInput, req.Tools)
	// Legacy reachability preflight: keep accepting the reserved case even
	// though current validators self-check their listener instead.
	if refharness.IsPreflight(req.CaseID) {
		calls = refharness.PreflightCalls()
	}
	finalText := "ok"
	// Observed tool execution: when the validator advertises a mock tool
	// endpoint, actually EXECUTE the routed tools through it (so the validator
	// observes the trajectory) and incorporate the returned content into the
	// answer. A pure function of (prompt, tools, seeded endpoint) — still
	// deterministic, so the calibrator's difficulty signal is preserved.
	if result, err := refharness.Execute(r.Context(), req.ToolEndpoint, req.CaseID, req.UserID, calls); err == nil && result != "" {
		finalText = result
	}
	resp := protocol.RunResponse{
		FinalText: finalText,
		ToolCalls: calls,
		LatencyMs: fixedLatencyMs,
	}
	writeJSON(w, http.StatusOK, resp)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
