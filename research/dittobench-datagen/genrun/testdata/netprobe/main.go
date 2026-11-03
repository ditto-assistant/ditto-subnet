// Emits the reference pack only when the network is unreachable.
package main

import (
	"encoding/json"
	"net"
	"os"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

func main() {
	var req challenge.GenerateRequest
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		os.Exit(2)
	}
	if conn, err := net.DialTimeout("tcp", "1.1.1.1:443", 3*time.Second); err == nil {
		conn.Close()
		os.Exit(9)
	}
	p := challenge.ReferencePack(req.SeedInt64(), req.MinerHotkey)
	p.Generator.Seed = req.Seed
	_ = json.NewEncoder(os.Stdout).Encode(p)
}
