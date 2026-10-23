// Command generator is the DittoBench generator competition starter kit: a
// deterministic program that emits a challenge pack for a seed and hotkey.
//
//	go run . -seed 42 -hotkey <ss58> -out pack.json
//	(cd ../../research/dittobench-datagen && go run ./cmd/challenge validate "$OLDPWD/pack.json")
//	uv run ditto generator upload pack.json --wallet.name <cold> --wallet.hotkey <hot>
//
// Packs earn credit for cases the top agents split on. Fork families/ to add
// the kinds of cases you believe current agents get wrong.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-generator-starter-kit/families"
)

// Name and Version identify this generator in the pack's provenance.
const (
	Name    = "starter-kit"
	Version = "1"
)

// Generate builds a pack: perFamily cases from each family plus filler.
func Generate(seed int64, hotkey string, perFamily int) challenge.Pack {
	b := families.NewBuilder(seed)
	for i := 0; i < perFamily; i++ {
		for _, family := range families.Default {
			b.Add(family(b, i))
			families.Filler(b)
		}
	}
	return b.Pack(hotkey, Name, Version, seed)
}

func main() {
	seed := flag.Int64("seed", 1, "generator seed")
	hotkey := flag.String("hotkey", "", "SS58 hotkey the pack is bound to (required)")
	perFamily := flag.Int("per-family", 4, "cases per family (4 families; 2..6)")
	out := flag.String("out", "", "write the pack here (default: stdout)")
	flag.Parse()
	if *hotkey == "" || *perFamily < 2 || *perFamily > 6 {
		fmt.Fprintln(os.Stderr, "usage: generator -hotkey <ss58> [-seed N] [-per-family 2..6] [-out pack.json]")
		os.Exit(2)
	}
	pack := Generate(*seed, *hotkey, *perFamily)
	if r := challenge.Validate(pack); !r.Valid {
		b, _ := json.MarshalIndent(r.Issues, "", "  ")
		fmt.Fprintf(os.Stderr, "generated pack is invalid:\n%s\n", b)
		os.Exit(1)
	}
	b, err := json.MarshalIndent(pack, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	b = append(b, '\n')
	if *out == "" {
		os.Stdout.Write(b)
		return
	}
	if err := os.WriteFile(*out, b, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "wrote %d cases to %s (sha256 %s)\n", len(pack.Cases), *out, challenge.Digest(b))
}
