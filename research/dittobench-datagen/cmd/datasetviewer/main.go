// Command datasetviewer serves a loopback-only interface for inspecting the
// exact generated benchmark artifact and reviewer-only answerability metadata.
package main

import (
	"embed"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

//go:embed static/*
var staticFiles embed.FS

type viewerConfig struct {
	DefaultSeed         int64  `json:"default_seed"`
	DefaultRunSize      string `json:"default_run_size"`
	DefaultBenchVersion int    `json:"default_bench_version"`
	SupportedVersions   []int  `json:"supported_versions"`
}

type datasetSummary struct {
	ToolCases              int `json:"tool_cases"`
	MemoryCases            int `json:"memory_cases"`
	MemoryRecords          int `json:"memory_records"`
	PrerequisiteRecords    int `json:"prerequisite_records"`
	RepeatedPairIdentities int `json:"repeated_pair_identities"`
	MemoryWaves            int `json:"memory_waves"`
	Subjects               int `json:"subjects"`
	SubjectLinks           int `json:"subject_links"`
}

type datasetResponse struct {
	RunSize       string            `json:"run_size"`
	DatasetSHA256 string            `json:"dataset_sha256"`
	Summary       datasetSummary    `json:"summary"`
	Review        gen.DatasetReview `json:"review"`
}

func main() {
	var cfg viewerConfig
	var addr string
	flag.StringVar(&addr, "addr", "127.0.0.1:8787", "loopback listen address")
	flag.Int64Var(&cfg.DefaultSeed, "seed", 123456789, "default dataset seed")
	flag.StringVar(&cfg.DefaultRunSize, "run-size", "full", "default run size: small | medium | full")
	flag.IntVar(&cfg.DefaultBenchVersion, "bench-version", protocol.BenchVersionV8, "default benchmark version: 7 | 8 | 9")
	flag.Parse()
	cfg.SupportedVersions = []int{protocol.BenchVersionV7, protocol.BenchVersionV8, protocol.BenchVersionV9}
	if err := validateConfig(addr, cfg); err != nil {
		log.Fatal(err)
	}

	server := &http.Server{
		Addr:              addr,
		Handler:           newHandler(cfg),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      45 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("DittoBench dataset viewer: http://%s", addr)
	log.Printf("reviewer-only oracle fields are served on loopback and never sent to a harness")
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func validateConfig(addr string, cfg viewerConfig) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("invalid -addr: %w", err)
	}
	if host != "127.0.0.1" && host != "localhost" && host != "::1" {
		return fmt.Errorf("-addr must bind to loopback, got %q", host)
	}
	if !supportedViewerVersion(cfg.DefaultBenchVersion) {
		return fmt.Errorf("-bench-version must be 7, 8, or 9")
	}
	if _, ok := gen.ProfileForVersion(cfg.DefaultRunSize, cfg.DefaultBenchVersion); !ok {
		return fmt.Errorf("-run-size must be small, medium, or full")
	}
	return nil
}

func newHandler(cfg viewerConfig) http.Handler {
	assets, err := fs.Sub(staticFiles, "static")
	if err != nil {
		panic(err)
	}
	mux := http.NewServeMux()
	mux.Handle("GET /assets/", http.StripPrefix("/assets/", http.FileServer(http.FS(assets))))
	mux.HandleFunc("GET /api/config", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, cfg)
	})
	mux.HandleFunc("GET /api/fresh-seed", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]int64{"seed": gen.FreshSeed()})
	})
	mux.HandleFunc("GET /api/dataset", datasetHandler(cfg))
	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		data, readErr := staticFiles.ReadFile("static/index.html")
		if readErr != nil {
			http.Error(w, "viewer unavailable", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(data)
	})
	return securityHeaders(mux)
}

func datasetHandler(defaults viewerConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		seed, err := parseInt64(r.URL.Query().Get("seed"), defaults.DefaultSeed)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, "seed must be a signed 64-bit integer")
			return
		}
		version, err := parseInt(r.URL.Query().Get("bench_version"), defaults.DefaultBenchVersion)
		if err != nil || !supportedViewerVersion(version) {
			writeAPIError(w, http.StatusBadRequest, "bench_version must be 7, 8, or 9")
			return
		}
		runSize := strings.TrimSpace(r.URL.Query().Get("run_size"))
		if runSize == "" {
			runSize = defaults.DefaultRunSize
		}
		prof, ok := gen.ProfileForVersion(runSize, version)
		if !ok {
			writeAPIError(w, http.StatusBadRequest, "run_size must be small, medium, or full")
			return
		}

		review, err := gen.GenerateDatasetReview(seed, prof, version)
		if err != nil {
			writeAPIError(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		sha, _, err := review.Artifact.SHA256Hex()
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "hash generated artifact")
			return
		}
		summary := datasetSummary{
			ToolCases: len(review.Artifact.ToolCases), MemoryCases: len(review.Artifact.MemoryCases),
			MemoryWaves: len(review.Artifact.MemoryWaves),
		}
		recordSignatures := map[string]bool{}
		pairIdentityCounts := map[string]int{}
		for _, wave := range review.Artifact.MemoryWaves {
			for _, pair := range wave.Pairs {
				summary.MemoryRecords++
				recordSignatures[memoryRecordSignature(wave.UserID, pair)] = true
				identity := wave.UserID + "\x00" + pair.PairID
				pairIdentityCounts[identity]++
			}
			summary.Subjects += len(wave.Subjects)
			summary.SubjectLinks += len(wave.Links)
		}
		for _, toolCase := range review.Artifact.ToolCases {
			for _, pair := range toolCase.PrerequisitePairs {
				signature := memoryRecordSignature("miner", pair)
				if recordSignatures[signature] {
					continue
				}
				recordSignatures[signature] = true
				summary.PrerequisiteRecords++
				summary.MemoryRecords++
			}
		}
		for _, count := range pairIdentityCounts {
			if count > 1 {
				summary.RepeatedPairIdentities += count - 1
			}
		}
		writeJSON(w, http.StatusOK, datasetResponse{
			RunSize: runSize, DatasetSHA256: sha, Summary: summary, Review: review,
		})
	}
}

func memoryRecordSignature(userID string, pair protocol.MemoryPair) string {
	return strings.Join([]string{
		userID, pair.PairID, pair.SessionID, pair.Timestamp, pair.Prompt, pair.Response,
	}, "\x00")
}

func supportedViewerVersion(version int) bool {
	return version == protocol.BenchVersionV7 || version == protocol.BenchVersionV8 || version == protocol.BenchVersionV9
}

func parseInt64(raw string, fallback int64) (int64, error) {
	if strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	return strconv.ParseInt(raw, 10, 64)
}

func parseInt(raw string, fallback int) (int, error) {
	if strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	return strconv.Atoi(raw)
}

func writeAPIError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		next.ServeHTTP(w, r)
	})
}
