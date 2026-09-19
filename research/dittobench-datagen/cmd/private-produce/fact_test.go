package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
)

func TestFactProducerPreflightNeverDispatches(t *testing.T) {
	t.Setenv("OPENROUTER_API_KEY", "")
	p := privatesurface.Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
	for _, size := range []string{"invalid", "small"} {
		out := filepath.Join(t.TempDir(), "candidate")
		if err := runFactProducer(42, size, out, p, 1); err == nil {
			t.Fatal("missing key or invalid size accepted")
		}
		if _, err := os.Stat(filepath.Join(out, "dataset.json")); !os.IsNotExist(err) {
			t.Fatal("failed preflight emitted dataset")
		}
	}
	if err := runFactProducer(42, "small", t.TempDir(), p, 1); err == nil {
		t.Fatal("existing directory accepted")
	}
}

func TestFactProfileDistinctFromLegacyRewrite(t *testing.T) {
	p := privatesurface.Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
	legacy, err := p.Digest()
	if err != nil {
		t.Fatal(err)
	}
	fact, err := privatesurface.FactProfileDigest(p)
	if err != nil {
		t.Fatal(err)
	}
	if fact == legacy || len(fact) != 64 {
		t.Fatal("fact and rewrite provenance conflated")
	}
	if _, err := privatesurface.FactProfileDigest(privatesurface.Profile{}); err == nil {
		t.Fatal("invalid profile accepted")
	}
}
