package main

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
)

func runFactProducer(seed int64, size, out string, profile privatesurface.Profile, limit float64) error {
	if _, ok := gen.ProfileForVersion(size, 13); !ok {
		return errors.New("fact producer: invalid run size")
	}
	profileSHA, err := privatesurface.FactProfileDigest(profile)
	if err != nil {
		return err
	}
	if err := os.Mkdir(out, 0700); err != nil {
		return errors.New("fact producer: output must be a new directory")
	}
	call := 0
	renderer, err := privatesurface.NewFactRenderer(profile, os.Getenv("OPENROUTER_API_KEY"), limit, func(s privatesurface.BudgetSnapshot) error { return writeBudgetCheckpoint(out, s) }, func(a privatesurface.FactRenderAudit) error {
		raw, err := json.Marshal(a)
		if err != nil {
			return err
		}
		call++
		return writePrivate(out, fmt.Sprintf("call-%04d.json", call), raw)
	})
	if err != nil {
		return err
	}
	var entropy [16]byte
	if _, err := rand.Read(entropy[:]); err != nil {
		return errors.New("fact producer: entropy unavailable")
	}
	world := int64(binary.BigEndian.Uint64(entropy[:8]))
	presentation := int64(binary.BigEndian.Uint64(entropy[8:]))
	if world == 0 || world == seed {
		return errors.New("fact producer: invalid independent entropy")
	}
	identity, _ := json.Marshal(map[string]any{"revision": gen.V13FactGenerationRevision, "seed": seed, "world_seed": world, "presentation_seed": presentation, "run_size": size, "profile_sha256": profileSHA, "budget_usd": limit, "qualified": false})
	if err := writePrivate(out, "generation.json", identity); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Hour)
	defer cancel()
	artifact, err := gen.GenerateV13FactDataset(ctx, seed, world, presentation, size, renderer)
	if err != nil {
		return errors.New("fact producer: generation failed; inspect private call receipts and budget checkpoint")
	}
	pin, raw, err := artifact.SHA256Hex()
	if err != nil {
		return err
	}
	if _, err := gen.DecodePrivateArtifact(raw, pin, seed, size); err != nil {
		return errors.New("fact producer: reconstruction failed")
	}
	if err := writePrivate(out, "dataset.json", raw); err != nil {
		return err
	}
	receipt, _ := json.Marshal(map[string]any{"revision": "v13-fact-candidate-v1", "dataset_sha256": pin, "profile_sha256": profileSHA, "run_size": size, "calls": call, "qualified": false, "semantic_coverage": "business-personal-programs-and-stories-only", "remaining_surface_qualification_required": true})
	if err := writePrivate(out, "fact-candidate.json", receipt); err != nil {
		return err
	}
	fmt.Println("fact candidate produced privately; NOT qualified, pinned, leased or activated")
	return nil
}
