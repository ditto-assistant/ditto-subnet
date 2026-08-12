package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/v10qual"
)

func main() {
	manifestPath := flag.String("manifest", "", "path to the private source-safe manifest")
	resultsPath := flag.String("results", "", "path to private qualification measurements")
	approvedSHA256 := flag.String("approved-manifest-sha256", "", "Backroom-approved manifest content address")
	flag.Parse()
	if *manifestPath == "" || *resultsPath == "" || *approvedSHA256 == "" {
		fmt.Fprintln(os.Stderr, "-manifest, -results, and -approved-manifest-sha256 are required")
		os.Exit(2)
	}
	manifestFile, err := os.Open(*manifestPath)
	if err != nil {
		fail(err)
	}
	defer manifestFile.Close()
	manifest, err := v10qual.DecodeManifest(manifestFile)
	if err != nil {
		fail(err)
	}
	resultsFile, err := os.Open(*resultsPath)
	if err != nil {
		fail(err)
	}
	defer resultsFile.Close()
	results, err := v10qual.DecodeResults(resultsFile)
	if err != nil {
		fail(err)
	}
	report, err := v10qual.Evaluate(manifest, results, *approvedSHA256)
	if err != nil {
		fail(err)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(report); err != nil {
		fail(err)
	}
	if !report.Ready {
		os.Exit(1)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "v10 qualification:", err)
	os.Exit(2)
}
