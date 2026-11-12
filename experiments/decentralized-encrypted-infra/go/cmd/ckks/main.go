// Command ckks measures CKKS (Lattigo v6) cost for the SN-473 feasibility
// questions on one Apple Silicon host.
//
// EXPERIMENT ONLY. This is not production code and it never sees real data:
// the adapter workload reads a synthetic fixture exported from the
// query-adapter pilot's seeded generators, and the similarity workload draws
// seeded random unit vectors.
//
//	ckks adapter    -fixture F -mode plain|encrypted [-degree 15] [-queries 16] [-workers 10]
//	ckks similarity -mode plain|encrypted [-blocks 2] [-workers 10]
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"time"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	var (
		result any
		err    error
	)
	switch os.Args[1] {
	case "adapter":
		result, err = runAdapter(os.Args[2:])
	case "similarity":
		result, err = runSimilarity(os.Args[2:])
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: ckks adapter|similarity [flags]")
	os.Exit(2)
}

// hostInfo records what the numbers were measured on.
type hostInfo struct {
	GoVersion  string `json:"go_version"`
	GOOS       string `json:"goos"`
	GOARCH     string `json:"goarch"`
	NumCPU     int    `json:"num_cpu"`
	GOMAXPROCS int    `json:"gomaxprocs"`
	Library    string `json:"library"`
	StartedAt  string `json:"started_at"`
}

func newHostInfo() hostInfo {
	return hostInfo{
		GoVersion:  runtime.Version(),
		GOOS:       runtime.GOOS,
		GOARCH:     runtime.GOARCH,
		NumCPU:     runtime.NumCPU(),
		GOMAXPROCS: runtime.GOMAXPROCS(0),
		Library:    "github.com/tuneinsight/lattigo/v6 v6.2.0",
		StartedAt:  time.Now().UTC().Format(time.RFC3339),
	}
}
