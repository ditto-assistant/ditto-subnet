package longmemeval

import (
	"bytes"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestSignedEvidenceBindsEveryRootAndScoreField(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	want, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	mutations := map[string]func(*Evidence){
		"schema":           func(e *Evidence) { e.SchemaVersion++ },
		"artifact":         func(e *Evidence) { e.ArtifactSHA256 = artifactDigestB },
		"bench":            func(e *Evidence) { e.BenchVersion = 8 },
		"profile checksum": func(e *Evidence) { e.ProfileChecksum = strings.Repeat("1", 64) },
		"dataset revision": func(e *Evidence) { e.DatasetRevision += "-changed" },
		"dataset digest":   func(e *Evidence) { e.DatasetSHA256 = strings.Repeat("2", 64) },
		"case-set digest":  func(e *Evidence) { e.CaseSetDigest = strings.Repeat("3", 64) },
		"latency":          func(e *Evidence) { e.LatencyMS++ },
		"mean":             func(e *Evidence) { e.Score.LongMemMean += 0.01 },
		"stderr":           func(e *Evidence) { e.Score.LongMemStdErr += 0.01 },
		"case count":       func(e *Evidence) { e.Score.CaseCount++ },
		"capability order": func(e *Evidence) {
			e.Score.PerCapability[0], e.Score.PerCapability[1] = e.Score.PerCapability[1], e.Score.PerCapability[0]
		},
		"capability identity": func(e *Evidence) { e.Score.PerCapability[0].Capability = CapabilityPreference },
		"capability correct":  func(e *Evidence) { e.Score.PerCapability[0].Correct++ },
		"capability count":    func(e *Evidence) { e.Score.PerCapability[0].Count++ },
		"capability mean":     func(e *Evidence) { e.Score.PerCapability[0].Mean += 0.1 },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := cloneEvidence(evidence)
			mutate(&changed)
			assertCannotRetainDigest(t, changed, profile, selection, want)
		})
	}
}

func TestSignedEvidenceBindsEveryProviderLaneField(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	want, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	mutations := map[string]func(*ProviderEvidence){
		"lane":               func(e *ProviderEvidence) { e.Lane += "-changed" },
		"cost source":        func(e *ProviderEvidence) { e.CostSource = "estimate" },
		"currency":           func(e *ProviderEvidence) { e.Currency = "EUR" },
		"provider":           func(e *ProviderEvidence) { e.Provider += "-changed" },
		"profile revision":   func(e *ProviderEvidence) { e.ProfileRevision += "-changed" },
		"model":              func(e *ProviderEvidence) { e.Model += "-changed" },
		"fallback":           func(e *ProviderEvidence) { e.FallbackUsed = true },
		"requests":           func(e *ProviderEvidence) { e.Requests++ },
		"successes":          func(e *ProviderEvidence) { e.Successes-- },
		"receipted requests": func(e *ProviderEvidence) { e.ReceiptedRequests-- },
		"prompt tokens":      func(e *ProviderEvidence) { e.PromptTokens++; e.TotalTokens++ },
		"completion tokens":  func(e *ProviderEvidence) { e.CompletionTokens++; e.TotalTokens++ },
		"total tokens":       func(e *ProviderEvidence) { e.TotalTokens++ },
		"integer cost":       func(e *ProviderEvidence) { e.CostUSDmicros++ },
		"receipt digest":     func(e *ProviderEvidence) { e.ReceiptSetSHA256 = strings.Repeat("4", 64) },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := cloneEvidence(evidence)
			mutate(&changed.ProviderEvidence[0])
			assertCannotRetainDigest(t, changed, profile, selection, want)
		})
	}
}

func TestCanonicalEvidencePreservesAndSortsEveryFrozenProviderLane(t *testing.T) {
	profile, selection := selectFixture(t)
	profile.Providers = append(profile.Providers, ProviderPolicy{
		Lane: "embedding", Provider: "openrouter", ProfileRevision: "embedding-shadow-v1",
		Model: "perplexity/pplx-embed-v1-0.6b", MaxRequests: 10,
		MaxPromptTokens: 1000, MaxCompletionTokens: 100, MaxTotalTokens: 1100, MaxCostUSDmicros: 10000,
	})
	// Provider policy is part of the profile checksum and therefore selection.
	selection, err := Select(profile, syntheticCases(profile.CasesPerCapability+5))
	if err != nil {
		t.Fatal(err)
	}
	score, err := Aggregate(selection, balancedOutcomes(selection, 0))
	if err != nil {
		t.Fatal(err)
	}
	providers := providerEvidenceFor(profile)
	providers[0], providers[2] = providers[2], providers[0]
	evidence, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, score, providers)
	if err != nil {
		t.Fatal(err)
	}
	if got := []string{evidence.ProviderEvidence[0].Lane, evidence.ProviderEvidence[1].Lane, evidence.ProviderEvidence[2].Lane}; !reflect.DeepEqual(got, []string{"embedding", "judge", "reader"}) {
		t.Fatalf("canonical lanes=%v", got)
	}
	want, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	reordered := cloneEvidence(evidence)
	reordered.ProviderEvidence[0], reordered.ProviderEvidence[2] = reordered.ProviderEvidence[2], reordered.ProviderEvidence[0]
	got, err := reordered.Digest(profile, selection)
	if err != nil || got != want {
		t.Fatalf("provider input order changed canonical digest: got=%s err=%v", got, err)
	}
	for index := range evidence.ProviderEvidence {
		missing := cloneEvidence(evidence)
		missing.ProviderEvidence = append(missing.ProviderEvidence[:index:index], missing.ProviderEvidence[index+1:]...)
		if _, err := missing.Digest(profile, selection); err == nil {
			t.Fatalf("missing provider lane %d became signable", index)
		}
	}
	duplicate := cloneEvidence(evidence)
	duplicate.ProviderEvidence[1] = duplicate.ProviderEvidence[0]
	if _, err := duplicate.Digest(profile, selection); err == nil {
		t.Fatal("duplicate provider lane became signable")
	}
}

func TestZeroScoreIsAvailableValidAndSignable(t *testing.T) {
	profile, selection := selectFixture(t)
	score, err := Aggregate(selection, balancedOutcomes(selection, 0))
	if err != nil {
		t.Fatal(err)
	}
	if score.LongMemMean != 0 || score.LongMemStdErr != 0 || score.CaseCount == 0 {
		t.Fatalf("zero score lost availability metadata: %#v", score)
	}
	evidence, err := NewEvidence(
		profile, selection, artifactDigestA, fixtureLatencyMS, score, providerEvidenceFor(profile),
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := evidence.Validate(profile, selection); err != nil {
		t.Fatal(err)
	}
	if _, err := evidence.Digest(profile, selection); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	var roundTrip Evidence
	if err := json.Unmarshal(raw, &roundTrip); err != nil {
		t.Fatal(err)
	}
	if err := roundTrip.Validate(profile, selection); err != nil {
		t.Fatal(err)
	}
}

func TestUnavailableOrMissingScoreFailsClosed(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	if err := (Evidence{}).Validate(profile, selection); err == nil {
		t.Fatal("unavailable evidence validated as a zero score")
	}
	if _, err := (Evidence{}).Digest(profile, selection); err == nil {
		t.Fatal("unavailable evidence became signable as a zero score")
	}

	zeroScore, err := Aggregate(selection, balancedOutcomes(selection, 0))
	if err != nil {
		t.Fatal(err)
	}
	zeroEvidence, err := NewEvidence(
		profile, selection, artifactDigestA, fixtureLatencyMS, zeroScore, providerEvidenceFor(profile),
	)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(zeroEvidence)
	if err != nil {
		t.Fatal(err)
	}
	missingMean := bytes.Replace(raw, []byte(`"longmem_mean":0,`), nil, 1)
	if bytes.Equal(missingMean, raw) {
		t.Fatal("test did not remove zero longmem_mean")
	}
	var decoded Evidence
	if err := json.Unmarshal(missingMean, &decoded); err == nil {
		t.Fatal("missing zero longmem_mean decoded as an available score")
	}

	missingScore := bytes.Replace(raw, []byte(`"score":`), []byte(`"missing_score":`), 1)
	if err := json.Unmarshal(missingScore, &decoded); err == nil {
		t.Fatal("missing score object decoded as available")
	}
	_ = evidence
}

func TestStrictTypedJSONRejectsMissingZeroValuedSignedFields(t *testing.T) {
	profile, selection := selectFixture(t)
	zeroScore, err := Aggregate(selection, balancedOutcomes(selection, 0))
	if err != nil {
		t.Fatal(err)
	}
	zeroCostProviders := providerEvidenceFor(profile)
	zeroCostProviders[0].CostUSDmicros = 0
	evidence, err := NewEvidence(
		profile, selection, artifactDigestA, fixtureLatencyMS, zeroScore, zeroCostProviders,
	)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	mutations := map[string]func([]byte) []byte{
		"latency": func(value []byte) []byte {
			return bytes.Replace(value, []byte(`"latency_ms":1234,`), nil, 1)
		},
		"zero mean": func(value []byte) []byte {
			return bytes.Replace(value, []byte(`"longmem_mean":0,`), nil, 1)
		},
		"zero capability correct": func(value []byte) []byte {
			return bytes.Replace(value, []byte(`"correct":0,`), nil, 1)
		},
		"false fallback": func(value []byte) []byte {
			return bytes.Replace(value, []byte(`"fallback_used":false,`), nil, 1)
		},
		"zero provider cost": func(value []byte) []byte {
			return bytes.Replace(value, []byte(`"cost_usd_micros":0,`), nil, 1)
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := mutate(append([]byte(nil), raw...))
			if bytes.Equal(changed, raw) {
				t.Fatal("test mutation did not remove field")
			}
			var decoded Evidence
			if err := json.Unmarshal(changed, &decoded); err == nil {
				t.Fatal("missing zero-valued signed field decoded successfully")
			}
		})
	}
}

func TestStrictTypedJSONRejectsUnknownNullAndTrailingFields(t *testing.T) {
	_, _, _, evidence := validEvidence(t)
	raw, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string][]byte{
		"unknown root":       append(append([]byte(nil), raw[:len(raw)-1]...), []byte(`,"unsigned_shadow":1}`)...),
		"unknown score":      bytes.Replace(raw, []byte(`"case_count":60,`), []byte(`"case_count":60,"unsigned_shadow":1,`), 1),
		"unknown capability": bytes.Replace(raw, []byte(`"correct":5,`), []byte(`"correct":5,"unsigned_shadow":1,`), 1),
		"unknown provider":   bytes.Replace(raw, []byte(`"cost_source":"provider_receipt_v1",`), []byte(`"cost_source":"provider_receipt_v1","unsigned_shadow":1,`), 1),
		"null latency":       bytes.Replace(raw, []byte(`"latency_ms":1234`), []byte(`"latency_ms":null`), 1),
		"null score":         bytes.Replace(raw, []byte(`"score":{`), []byte(`"score":null,"discarded":{`), 1),
		"trailing JSON":      append(append([]byte(nil), raw...), []byte(` {}`)...),
		"duplicate root":     bytes.Replace(raw, []byte(`"latency_ms":1234`), []byte(`"latency_ms":1,"latency_ms":1234`), 1),
		"duplicate score":    bytes.Replace(raw, []byte(`"longmem_mean":0.5`), []byte(`"longmem_mean":0,"longmem_mean":0.5`), 1),
		"duplicate provider": bytes.Replace(raw, []byte(`"fallback_used":false`), []byte(`"fallback_used":true,"fallback_used":false`), 1),
	}
	for name, changed := range tests {
		t.Run(name, func(t *testing.T) {
			if bytes.Equal(changed, raw) {
				t.Fatal("test mutation did not change JSON")
			}
			var decoded Evidence
			if err := json.Unmarshal(changed, &decoded); err == nil {
				t.Fatal("noncanonical signed JSON decoded successfully")
			}
		})
	}
}

func TestCanonicalSignedTypesContainNoMapFields(t *testing.T) {
	for _, value := range []any{Evidence{}, Score{}, CapabilityScore{}, ProviderEvidence{}} {
		assertTypeHasNoMaps(t, reflect.TypeOf(value), make(map[reflect.Type]bool))
	}
}

func TestLatencyIsRequiredAndDigestBound(t *testing.T) {
	profile, selection, score, evidence := validEvidence(t)
	if _, err := NewEvidence(profile, selection, artifactDigestA, 0, score, providerEvidenceFor(profile)); err == nil {
		t.Fatal("zero/unavailable latency accepted")
	}
	want, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	evidence.LatencyMS++
	got, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	if got == want {
		t.Fatal("latency mutation retained signed digest")
	}
	evidence.LatencyMS = 0
	if _, err := evidence.Digest(profile, selection); err == nil {
		t.Fatal("missing latency became signable")
	}
}

func assertCannotRetainDigest(
	t *testing.T,
	evidence Evidence,
	profile Profile,
	selection Selection,
	want string,
) {
	t.Helper()
	got, err := evidence.Digest(profile, selection)
	if err == nil && got == want {
		t.Fatal("material mutation retained canonical digest")
	}
}

func assertTypeHasNoMaps(t *testing.T, value reflect.Type, seen map[reflect.Type]bool) {
	t.Helper()
	if value == nil || seen[value] {
		return
	}
	seen[value] = true
	switch value.Kind() {
	case reflect.Map:
		t.Fatalf("signed type contains map field: %s", value)
	case reflect.Pointer, reflect.Slice, reflect.Array:
		assertTypeHasNoMaps(t, value.Elem(), seen)
	case reflect.Struct:
		for index := 0; index < value.NumField(); index++ {
			assertTypeHasNoMaps(t, value.Field(index).Type, seen)
		}
	}
}
