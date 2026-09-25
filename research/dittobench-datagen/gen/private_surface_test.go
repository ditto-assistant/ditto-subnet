package gen

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type privateTransformFunc func(context.Context, PrivateSurfaceRequest) (string, error)

func (f privateTransformFunc) Transform(ctx context.Context, r PrivateSurfaceRequest) (string, error) {
	return f(ctx, r)
}

type privateValidateFunc func(context.Context, PrivateSurfaceRequest, string) error

func (f privateValidateFunc) Validate(ctx context.Context, r PrivateSurfaceRequest, s string) error {
	return f(ctx, r, s)
}

// Test validators below are plumbing fixtures, not semantic qualification.
var acceptPrivateFixture = privateValidateFunc(func(context.Context, PrivateSurfaceRequest, string) error { return nil })
var rewritePrivateFixture = privateTransformFunc(func(_ context.Context, r PrivateSurfaceRequest) (string, error) {
	return "Please consider: " + r.Text, nil
})

func privateFixture() DatasetArtifact {
	pair := protocol.MemoryPair{PairID: "p", Prompt: "The code is ZEBRA", Response: "Recorded."}
	return DatasetArtifact{
		BenchVersion: 13, SurfaceSalt: 7, Seed: 123,
		ToolCases:   []protocol.ToolCase{{ID: "tool", Prompt: "Search my notes.", PrerequisitePairs: []protocol.MemoryPair{pair}}},
		MemoryWaves: []protocol.SeedRequest{{Pairs: []protocol.MemoryPair{pair}}},
		MemoryCases: []ArtifactCase{{MemoryCase: protocol.MemoryCase{ID: "q", Question: "What is my code?", ExpectedAnswer: "ZEBRA"}}},
	}
}

func TestPrivateSurfaceAtomicAndRepeatedPairs(t *testing.T) {
	input := privateFixture()
	before := mustMarshal(t, input)
	calls := map[string]int{}
	transform := privateTransformFunc(func(_ context.Context, r PrivateSurfaceRequest) (string, error) {
		calls[r.Location]++
		if r.Location == "case:q" && len(r.Protected) != 0 {
			t.Fatal("question request leaked an absent grading value")
		}
		return "Please consider: " + r.Text, nil
	})
	output, err := ApplyPrivateSurface(context.Background(), input, transform, acceptPrivateFixture)
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(mustMarshal(t, input)) {
		t.Fatal("base artifact mutated")
	}
	if !reflect.DeepEqual(output.ToolCases[0].PrerequisitePairs, output.MemoryWaves[0].Pairs) {
		t.Fatal("repeated pair was rendered differently")
	}
	for location, count := range calls {
		if count != 1 {
			t.Fatalf("%s transformed %d times", location, count)
		}
	}
	if len(calls) != 4 || output.MemoryCases[0].ExpectedAnswer != "ZEBRA" {
		t.Fatalf("unexpected surfaces or modified grading: %d", len(calls))
	}
	output.MemoryWaves[0].Pairs[0].Prompt = "mutation"
	if string(before) != string(mustMarshal(t, input)) {
		t.Fatal("output aliases base artifact")
	}
}

func TestPrivateSurfaceFailsClosed(t *testing.T) {
	for _, tc := range []struct {
		name string
		fn   privateTransformFunc
		v    PrivateSurfaceValidator
	}{
		{"provider error", func(context.Context, PrivateSurfaceRequest) (string, error) {
			return "", errors.New("SECRET provider payload")
		}, acceptPrivateFixture},
		{"empty", func(context.Context, PrivateSurfaceRequest) (string, error) { return "", nil }, acceptPrivateFixture},
		{"invalid utf8", func(context.Context, PrivateSurfaceRequest) (string, error) { return string([]byte{0xff}), nil }, acceptPrivateFixture},
		{"oversized", func(_ context.Context, r PrivateSurfaceRequest) (string, error) {
			return strings.Repeat("x", 4*len(r.Text)+1025), nil
		}, acceptPrivateFixture},
		{"answer leak", func(_ context.Context, r PrivateSurfaceRequest) (string, error) { return r.Text + " ZEBRA", nil }, acceptPrivateFixture},
		{"changed value", func(_ context.Context, r PrivateSurfaceRequest) (string, error) {
			return "Please: " + strings.ReplaceAll(r.Text, "ZEBRA", "HORSE"), nil
		}, acceptPrivateFixture},
		{"unchanged", func(_ context.Context, r PrivateSurfaceRequest) (string, error) { return r.Text, nil }, acceptPrivateFixture},
		{"semantic error", rewritePrivateFixture, privateValidateFunc(func(context.Context, PrivateSurfaceRequest, string) error {
			return errors.New("SECRET semantic payload")
		})},
		{"missing validator", rewritePrivateFixture, nil},
		{"missing transformer", nil, acceptPrivateFixture},
	} {
		t.Run(tc.name, func(t *testing.T) {
			input := privateFixture()
			before := mustMarshal(t, input)
			var transform PrivateSurfaceTransformer
			if tc.fn != nil {
				transform = tc.fn
			}
			got, err := ApplyPrivateSurface(context.Background(), input, transform, tc.v)
			if err == nil || !reflect.DeepEqual(got, DatasetArtifact{}) {
				t.Fatalf("failure returned usable artifact: %v", err)
			}
			if strings.Contains(err.Error(), "SECRET") || string(before) != string(mustMarshal(t, input)) {
				t.Fatal("failure exposed provider text or mutated input")
			}
		})
	}
}

func TestPrivateSurfaceRejectsPublicAndCancelledInputs(t *testing.T) {
	for _, version := range []int{12, 14} {
		input := privateFixture()
		input.BenchVersion = version
		if _, err := ApplyPrivateSurface(context.Background(), input, rewritePrivateFixture, acceptPrivateFixture); err == nil {
			t.Fatalf("accepted version %d", version)
		}
	}
	input := privateFixture()
	input.SurfaceSalt = 0
	if _, err := ApplyPrivateSurface(context.Background(), input, rewritePrivateFixture, acceptPrivateFixture); err == nil {
		t.Fatal("accepted public rehearsal artifact")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := ApplyPrivateSurface(ctx, privateFixture(), rewritePrivateFixture, acceptPrivateFixture); err == nil {
		t.Fatal("ignored cancellation")
	}
}

func TestPrivateSurfaceRejectsConflictingRepeatedPair(t *testing.T) {
	input := privateFixture()
	input.MemoryWaves[0].Pairs[0].Prompt = "A different record."
	if _, err := ApplyPrivateSurface(context.Background(), input, rewritePrivateFixture, acceptPrivateFixture); err == nil {
		t.Fatal("accepted conflicting repeated pair")
	}
}

func TestPrivateSurfaceGraphScopeAndValidatorIsolation(t *testing.T) {
	input := privateFixture()
	input.MemoryWaves = append(input.MemoryWaves, protocol.SeedRequest{
		UserID: "other-user", Pairs: []protocol.MemoryPair{{PairID: "p", Prompt: "Different graph.", Response: "Noted."}},
	})
	transform := privateTransformFunc(func(_ context.Context, r PrivateSurfaceRequest) (string, error) {
		if len(r.Protected) > 0 {
			r.Protected[0] = "provider mutation"
		}
		return "Please consider: " + r.Text, nil
	})
	validator := privateValidateFunc(func(_ context.Context, r PrivateSurfaceRequest, _ string) error {
		for _, value := range r.Protected {
			if value == "provider mutation" {
				t.Fatal("provider mutated validation metadata")
			}
		}
		return nil
	})
	if _, err := ApplyPrivateSurface(context.Background(), input, transform, validator); err != nil {
		t.Fatal(err)
	}
}

func TestPrivateSurfaceRealArtifactPreservesNonSurfaceFields(t *testing.T) {
	profile, _ := ProfileForVersion("full", 13)
	input, err := GenerateDatasetWithSurface(4242, profile, 13, SurfaceOptions{Salt: 91})
	if err != nil {
		t.Fatal(err)
	}
	// Deliberately NOT an actual private paraphrase or qualification result.
	// This checks that all real generated record identities can traverse the
	// atomic transformation machinery without changing any other fields.
	appendLine := privateTransformFunc(func(_ context.Context, r PrivateSurfaceRequest) (string, error) {
		return r.Text + "\n", nil
	})
	output, err := ApplyPrivateSurface(context.Background(), input, appendLine, acceptPrivateFixture)
	if err != nil {
		t.Fatal(err)
	}
	for i := range output.ToolCases {
		output.ToolCases[i].Prompt = input.ToolCases[i].Prompt
		output.ToolCases[i].PrerequisitePairs = input.ToolCases[i].PrerequisitePairs
	}
	for i := range output.MemoryCases {
		output.MemoryCases[i].Question = input.MemoryCases[i].Question
	}
	for i := range output.MemoryWaves {
		output.MemoryWaves[i].Pairs = input.MemoryWaves[i].Pairs
	}
	if string(mustMarshal(t, output)) != string(mustMarshal(t, input)) {
		t.Fatal("private transformation changed non-surface bytes")
	}
}

func TestPrivateSurfacePlanMatchesExecutionWithoutMutatingInput(t *testing.T) {
	profile, _ := ProfileForVersion("full", 13)
	input, err := GenerateDatasetWithSurface(4242, profile, 13, SurfaceOptions{Salt: 91})
	if err != nil {
		t.Fatal(err)
	}
	before := mustMarshal(t, input)
	requests, err := PrivateSurfaceRequests(input)
	if err != nil {
		t.Fatal(err)
	}
	index := 0
	transform := privateTransformFunc(func(_ context.Context, request PrivateSurfaceRequest) (string, error) {
		if index >= len(requests) || !reflect.DeepEqual(request, requests[index]) {
			t.Fatal("plan differs from execution")
		}
		index++
		return request.Text + "\n", nil
	})
	if _, err := ApplyPrivateSurface(context.Background(), input, transform, acceptPrivateFixture); err != nil {
		t.Fatal(err)
	}
	if index != len(requests) || string(before) != string(mustMarshal(t, input)) {
		t.Fatal("incomplete plan or mutated input")
	}
	t.Logf("full profile: %d unique surfaces, %d artifact bytes", len(requests), len(before))
}
