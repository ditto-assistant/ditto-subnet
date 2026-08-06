// Command generate-service is the HTTP wrapper the SN118 platform deploys to
// pin a per-submission dataset.
//
// POST /generate?seed=<int64>&run_size=<small|medium|full>&bench_version=<2..8> returns the FULL
// canonical DatasetArtifact (JSON): the tool cases, the staged memory seeding
// waves, the memory cases (with their unlock wave + graph user_id), and the
// tool-fixture digests — everything a validator needs to replay the eval, plus
// the oracle (expected tools/answers) the validator scores against. The
// artifact's SHA-256 and version are surfaced in X-Dataset-SHA256 and
// X-Bench-Version so the platform can pin them without re-marshaling. An
// omitted bench_version is a deprecated v2-only compatibility path for the old
// platform; new callers must select an explicit immutable contract.
//
// The platform calls this once per submission at screening pass and pins
// (seed, dataset_sha256, run_size, bench_version); validators regenerate the
// dataset from that complete contract and fail a run whose regenerated version
// or hash mismatches.
//
// The DEPLOYMENT is private (an IAM-gated Cloud Run service only the platform
// may invoke, so platform infrastructure cannot be farmed as a generation
// oracle), but the code carries no secret: generation is fully DETERMINISTIC
// and non-LLM (gen.GenerateDataset), byte-reproducible from (seed, run_size) —
// exactly what the public cmd/generate CLI computes. The anti-precompute
// guarantee is the seed, which is fixed from an on-chain block hash only after
// the miner commits their submission.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// defaultRunSize is used when the caller omits run_size. small is the cheapest
// profile (mirrors the scoring engine's profile default).
const defaultRunSize = "small"

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("POST /generate", handleGenerate)

	addr := ":" + envOr("PORT", "8090")
	log.Printf("dittobench-datagen generate service listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}

// handleGenerate produces one seeded dataset artifact. seed is required (the
// platform supplies a fresh one per submission); run_size selects the profile
// (defaults to small). bench_version selects the immutable generation contract;
// omission exists only for the deprecated v2 compatibility path. The response
// is the canonical DatasetArtifact JSON.
func handleGenerate(w http.ResponseWriter, r *http.Request) {
	seed, err := strconv.ParseInt(r.URL.Query().Get("seed"), 10, 64)
	if err != nil {
		http.Error(w, "seed query param required (int64)", http.StatusBadRequest)
		return
	}
	runSize := r.URL.Query().Get("run_size")
	if runSize == "" {
		runSize = defaultRunSize
	}

	versionText := r.URL.Query().Get("bench_version")
	legacyV2 := versionText == ""
	if legacyV2 {
		// Compatibility for platform versions deployed before version negotiation.
		// New/canonical callers must always send bench_version explicitly.
		versionText = strconv.Itoa(protocol.BenchVersionV2)
	}
	version, err := strconv.Atoi(versionText)
	if err != nil || !protocol.SupportedBenchVersion(version) {
		http.Error(w, "bench_version query param required (supported: 2, 3, 4, 5, 6, 7, 8)", http.StatusBadRequest)
		return
	}

	prof, ok := gen.ProfileForVersion(runSize, version)
	if !ok {
		http.Error(w, "unknown run_size (want small|medium|full)", http.StatusBadRequest)
		return
	}

	artifact, err := gen.GenerateDataset(seed, prof, version)
	if err != nil {
		http.Error(w, "generate dataset", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Bench-Version", strconv.Itoa(version))
	if legacyV2 {
		w.Header().Set("Deprecation", "true")
		w.Header().Set("Warning", `299 - "omitted bench_version is legacy v2 compatibility; send bench_version=2 explicitly"`)
	}
	// Surface the content hash so the platform can record/pin it with the ticket
	// without re-marshaling.
	if h, _, herr := artifact.SHA256Hex(); herr == nil {
		w.Header().Set("X-Dataset-SHA256", h)
	}
	if err := json.NewEncoder(w).Encode(artifact); err != nil {
		log.Printf("encode dataset (seed=%d run_size=%s): %v", seed, runSize, err)
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
