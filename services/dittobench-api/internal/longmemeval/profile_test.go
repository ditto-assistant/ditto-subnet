package longmemeval

import (
	"strings"
	"testing"
)

func TestFrozenProfileChecksum(t *testing.T) {
	profile := loadTestProfile(t)
	got, err := profile.Checksum()
	if err != nil {
		t.Fatal(err)
	}
	const want = "6b5815770b4677ee9b3245f79851495f48bce081fd56eacd839450c978687d2f"
	if got != want {
		t.Fatalf("profile checksum changed: got %s want %s", got, want)
	}
}

func TestProfileChecksumIgnoresProviderInputOrder(t *testing.T) {
	profile := loadTestProfile(t)
	want, err := profile.Checksum()
	if err != nil {
		t.Fatal(err)
	}
	profile.Providers[0], profile.Providers[1] = profile.Providers[1], profile.Providers[0]
	got, err := profile.Checksum()
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("provider order moved checksum: got %s want %s", got, want)
	}
}

func TestProfileChecksumMovesForEveryFrozenField(t *testing.T) {
	base := loadTestProfile(t)
	want, err := base.Checksum()
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string]func(*Profile){
		"revision":             func(p *Profile) { p.Revision += "-changed" },
		"dataset revision":     func(p *Profile) { p.DatasetRevision += "-changed" },
		"dataset digest":       func(p *Profile) { p.DatasetSHA256 = strings.Repeat("e", 64) },
		"selection seed":       func(p *Profile) { p.SelectionSeed++ },
		"quota":                func(p *Profile) { p.CasesPerCapability++ },
		"provider":             func(p *Profile) { p.Providers[0].Provider += "-changed" },
		"provider profile":     func(p *Profile) { p.Providers[0].ProfileRevision += "-changed" },
		"model":                func(p *Profile) { p.Providers[0].Model += "-changed" },
		"request cap":          func(p *Profile) { p.Providers[0].MaxRequests++ },
		"prompt token cap":     func(p *Profile) { p.Providers[0].MaxPromptTokens++ },
		"completion token cap": func(p *Profile) { p.Providers[0].MaxCompletionTokens++ },
		"total token cap":      func(p *Profile) { p.Providers[0].MaxTotalTokens++ },
		"cost cap":             func(p *Profile) { p.Providers[0].MaxCostUSDmicros++ },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := base
			changed.Providers = append([]ProviderPolicy(nil), base.Providers...)
			mutate(&changed)
			got, err := changed.Checksum()
			if err != nil {
				t.Fatal(err)
			}
			if got == want {
				t.Fatal("changed frozen field did not move checksum")
			}
		})
	}
}

func TestProfileValidationFailsClosed(t *testing.T) {
	base := loadTestProfile(t)
	tests := map[string]func(*Profile){
		"schema":                 func(p *Profile) { p.SchemaVersion++ },
		"empty revision":         func(p *Profile) { p.Revision = "" },
		"empty dataset revision": func(p *Profile) { p.DatasetRevision = " " },
		"wrong bench":            func(p *Profile) { p.BenchVersion = 8 },
		"uppercase digest":       func(p *Profile) { p.DatasetSHA256 = strings.ToUpper(p.DatasetSHA256) },
		"short digest":           func(p *Profile) { p.DatasetSHA256 = "abc" },
		"selector":               func(p *Profile) { p.SelectorRevision += "-unknown" },
		"one case":               func(p *Profile) { p.CasesPerCapability = 1 },
		"no providers":           func(p *Profile) { p.Providers = nil },
		"duplicate lane":         func(p *Profile) { p.Providers[1].Lane = p.Providers[0].Lane },
		"empty lane":             func(p *Profile) { p.Providers[0].Lane = "" },
		"empty provider":         func(p *Profile) { p.Providers[0].Provider = "" },
		"empty profile":          func(p *Profile) { p.Providers[0].ProfileRevision = "" },
		"empty model":            func(p *Profile) { p.Providers[0].Model = "" },
		"zero requests":          func(p *Profile) { p.Providers[0].MaxRequests = 0 },
		"zero prompt":            func(p *Profile) { p.Providers[0].MaxPromptTokens = 0 },
		"zero completion":        func(p *Profile) { p.Providers[0].MaxCompletionTokens = 0 },
		"zero total":             func(p *Profile) { p.Providers[0].MaxTotalTokens = 0 },
		"zero cost":              func(p *Profile) { p.Providers[0].MaxCostUSDmicros = 0 },
		"prompt above total":     func(p *Profile) { p.Providers[0].MaxPromptTokens = p.Providers[0].MaxTotalTokens + 1 },
		"completion above total": func(p *Profile) { p.Providers[0].MaxCompletionTokens = p.Providers[0].MaxTotalTokens + 1 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := base
			changed.Providers = append([]ProviderPolicy(nil), base.Providers...)
			mutate(&changed)
			if err := changed.Validate(); err == nil {
				t.Fatal("invalid profile accepted")
			}
		})
	}
}

func TestCapabilitiesReturnsIndependentCanonicalCopy(t *testing.T) {
	first := Capabilities()
	second := Capabilities()
	if len(first) != 6 || len(second) != 6 {
		t.Fatalf("wrong capability count: %d %d", len(first), len(second))
	}
	first[0] = Capability("mutated")
	if second[0] != CapabilityExtraction || Capabilities()[0] != CapabilityExtraction {
		t.Fatal("caller mutated canonical capability order")
	}
}
