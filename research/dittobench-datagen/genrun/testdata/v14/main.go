// Emits the reference pack at the newest grading policy the request accepts.
package main

import (
	"encoding/json"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

func main() {
	var req challenge.GenerateRequest
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		os.Exit(2)
	}
	p := challenge.ReferencePack(req.SeedInt64(), req.MinerHotkey)
	p.Generator.Seed = req.Seed
	p.GradingBenchVersion = req.NewestAccepted(nil)
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
