// Valid packs whose bytes depend on the clock.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

func main() {
	var req challenge.GenerateRequest
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		os.Exit(2)
	}
	p := challenge.ReferencePack(req.SeedInt64(), req.MinerHotkey)
	p.Generator.Seed = req.Seed
	p.Generator.Version = fmt.Sprint(time.Now().UnixNano())
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
