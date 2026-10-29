// Command generator is the DittoBench generator competition starter kit: a
// deterministic, CPU-only program that turns a GenerateRequest into a
// challenge pack.
//
// The scorer builds this module offline and runs it in a no-network sandbox:
// it writes one request JSON object to stdin and reads one pack JSON object
// from stdout. Rehearse exactly that path locally:
//
//	cd ../../research/dittobench-datagen
//	go run ./cmd/challenge rehearse -dir "$OLDPWD" -hotkey <ss58> -seeds 3 -cases 80
//	go run ./cmd/challenge rehearse -dir "$OLDPWD" -hotkey <ss58> -sandbox docker
//	go run ./cmd/challenge rehearse -dir "$OLDPWD" -hotkey <ss58> -round-bench 14
//
// and submit the source with `uv run ditto generator upload --dir .`.
//
// Generators earn credit for cases the top agents split on, on seeds nobody
// sees in advance. Fork families/ to add the kinds of cases you believe
// current agents get wrong.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-generator-starter-kit/families"
)

// Name and Version identify this generator in the pack's provenance.
// Version 4 declares the newest grading policy the request accepts.
const (
	Name    = "starter-kit"
	Version = "4"
)

// GradingBenchVersions are the pack grading policies the default families are
// written for. Every family states its answers, distractors, and dump guards
// as plain text, so its cases pass the v14 answer-text rule unchanged. A v13
// round's request accepts only v13 and gets exactly the v13 pack; a v14
// round's request also accepts v14, and the kit declares v14.
var GradingBenchVersions = []int{challenge.GradingBenchVersion, challenge.GradingBenchVersionV14}

// MaxCases is the most cases the starter pools can fill without reusing a
// value (twenty rounds of the four default families). It matches the pack
// contract's challenge.MaxCases, the most cases_per_seed Platform can set, so
// the kit can fill any round column. A generator that cannot fill the live
// cases_per_seed fails its round seed and is rejected.
const MaxCases = 80

// Generate builds the pack for one request: the default families in rotation,
// with a filler pair after every round that has room for both of its cases,
// until the requested count is reached.
func Generate(req challenge.GenerateRequest) (challenge.Pack, error) {
	if err := req.Validate(); err != nil {
		return challenge.Pack{}, err
	}
	if req.Cases > MaxCases {
		return challenge.Pack{}, fmt.Errorf("starter kit fills at most %d cases, request asks for %d", MaxCases, req.Cases)
	}
	b := families.NewBuilder(req)
	for i := 0; b.Len() < req.Cases; i++ {
		round, family := i/len(families.Default), families.Default[i%len(families.Default)]
		b.Add(family(b, round))
		// The filler pair closes a round only when both of its cases fit.
		if i%len(families.Default) == len(families.Default)-1 && b.Len()+2 <= req.Cases {
			families.Filler(b)
		}
	}
	pack := b.Pack(req, Name, Version)
	pack.GradingBenchVersion = req.NewestAccepted(GradingBenchVersions)
	return pack, nil
}

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "generator:", err)
		os.Exit(1)
	}
}

func run(in io.Reader, out io.Writer) error {
	var req challenge.GenerateRequest
	if err := json.NewDecoder(in).Decode(&req); err != nil {
		return fmt.Errorf("read request: %w", err)
	}
	pack, err := Generate(req)
	if err != nil {
		return err
	}
	// Self-check before emitting: a generator should never ship a case the
	// scorer will reject.
	raw, err := json.Marshal(pack)
	if err != nil {
		return err
	}
	if r := challenge.ValidateGenerated(raw, req); !r.Valid {
		b, _ := json.MarshalIndent(r.Issues, "", "  ")
		return fmt.Errorf("generated pack is invalid:\n%s", b)
	}
	_, err = out.Write(append(raw, '\n'))
	return err
}
