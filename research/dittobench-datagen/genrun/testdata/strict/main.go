// Decodes the request strictly into the original v13 request shape, as a
// generator written before grading_bench_versions might: an unknown field
// makes it exit.
package main

import (
	"encoding/json"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

type v13Request struct {
	Contract            string `json:"contract"`
	Seed                string `json:"seed"`
	MinerHotkey         string `json:"miner_hotkey"`
	Cases               int    `json:"cases"`
	GradingBenchVersion int    `json:"grading_bench_version"`
}

func main() {
	var req v13Request
	dec := json.NewDecoder(os.Stdin)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		os.Exit(2)
	}
	r := challenge.NewGenerateRequest(req.Seed, req.MinerHotkey, req.Cases)
	p := challenge.ReferencePack(r.SeedInt64(), req.MinerHotkey)
	p.Generator.Seed = req.Seed
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
