// Declares v14 when offered but breaks the v14 answer-text rule; its v13 path
// is valid.
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
	if req.Accepts(challenge.GradingBenchVersionV14) {
		p.GradingBenchVersion = challenge.GradingBenchVersionV14
		c := &p.Cases[0]
		c.DistractorAnswers = append([]string(nil), c.DistractorAnswers...)
		c.DistractorAnswers[0] = "**" + c.DistractorAnswers[0] + "**"
	}
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
