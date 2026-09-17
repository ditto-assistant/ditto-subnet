package gen

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// PrivateSurfaceRequest contains only one existing surface and values already
// present in it. In particular it does not expose absent grading answers to a
// paraphraser. Location is private provenance, never a harness field.
type PrivateSurfaceRequest struct {
	Location  string
	Text      string
	Protected []string
}

// PrivateSurfaceTransformer is intentionally separate from the legacy public
// TranslationPass. Provider failures must be errors, never unchanged fallback
// text. The caller must persist successful artifact bytes; retries must not
// regenerate a non-deterministic provider response.
type PrivateSurfaceTransformer interface {
	Transform(context.Context, PrivateSurfaceRequest) (string, error)
}

// PrivateSurfaceValidator must independently check semantic equivalence,
// including negation, temporal relationships, units, and instruction priority.
// The mechanical value check below is necessary but NOT sufficient. Production
// admission must separately require a qualified transformer/validator profile.
type PrivateSurfaceValidator interface {
	Validate(context.Context, PrivateSurfaceRequest, string) error
}

// ApplyPrivateSurface produces a detached artifact or no artifact at all. It
// does not activate a benchmark, attest qualification, persist bytes, or change
// the public rehearsal path. Input must already have received the salted v13
// assembly pass. Only tool prompts, memory questions, and pair text may change;
// catalogs, grading rules, graph identities and fixture content remain frozen.
func ApplyPrivateSurface(ctx context.Context, input DatasetArtifact, transformer PrivateSurfaceTransformer, validator PrivateSurfaceValidator, additionalProtected ...[]string) (DatasetArtifact, error) {
	fail := func(reason string) (DatasetArtifact, error) {
		return DatasetArtifact{}, fmt.Errorf("private surface: %s", reason)
	}
	if input.BenchVersion != protocol.BenchVersionV13 || input.SurfaceSalt == 0 {
		return fail("requires a salted v13 artifact")
	}
	if transformer == nil || validator == nil {
		return fail("transformer and semantic validator required")
	}
	// Deep-copy maps and slices too: neither errors nor successful output may
	// mutate the caller's base artifact or its provenance.
	raw, err := json.Marshal(input)
	if err != nil {
		return fail("cannot encode base artifact")
	}
	var output DatasetArtifact
	if err := json.Unmarshal(raw, &output); err != nil {
		return fail("cannot decode base artifact")
	}
	protected := v13GlobalProtected(&input)
	for _, extra := range additionalProtected {
		protected = append(protected, extra...)
	}
	type cached struct{ before, after string }
	cache := map[string]cached{}
	changed := false
	transform := func(location string, target *string) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		before := *target
		if old, ok := cache[location]; ok {
			if old.before != before {
				return errors.New("conflicting repeated surface")
			}
			*target = old.after
			return nil
		}
		request := PrivateSurfaceRequest{Location: location, Text: before}
		for _, value := range protected {
			if strings.Contains(before, value) {
				request.Protected = append(request.Protected, value)
			}
		}
		// Give each callback its own slice so a provider cannot alter the
		// validator's protection metadata through a shared backing array.
		providerRequest := request
		providerRequest.Protected = append([]string(nil), request.Protected...)
		after, err := transformer.Transform(ctx, providerRequest)
		if err != nil {
			// Provider errors can contain prompts, answers, or credentials.
			return errors.New("transform failed")
		}
		if !utf8.ValidString(after) || strings.TrimSpace(after) == "" || len(after) > 4*len(before)+1024 {
			return errors.New("invalid transformed text")
		}
		for _, value := range protected {
			// Includes absent values: adding a previously hidden answer to a
			// question is also forbidden. Conservative substring counts may
			// reject valid paraphrases; they must never silently approve one.
			if strings.Count(before, value) != strings.Count(after, value) {
				return errors.New("protected value changed or introduced")
			}
		}
		if err := validator.Validate(ctx, request, after); err != nil {
			return errors.New("semantic validation failed")
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		cache[location] = cached{before, after}
		changed = changed || before != after
		*target = after
		return nil
	}
	if err := visitPrivateSurfaces(&output, transform); err != nil {
		return fail(err.Error())
	}
	if !changed {
		return fail("unchanged artifact is not a private transformation")
	}
	return output, nil
}

// PrivateSurfaceRequests plans unique provider inputs without running any
// transformer or approving any output. Repeated graph-local records share one
// request. The returned strings/slices do not alias mutable input state.
func PrivateSurfaceRequests(input DatasetArtifact, additionalProtected ...[]string) ([]PrivateSurfaceRequest, error) {
	if input.BenchVersion != protocol.BenchVersionV13 || input.SurfaceSalt == 0 {
		return nil, errors.New("private surface: requires a salted v13 artifact")
	}
	protected := v13GlobalProtected(&input)
	for _, extra := range additionalProtected {
		protected = append(protected, extra...)
	}
	seen := map[string]string{}
	var requests []PrivateSurfaceRequest
	err := visitPrivateSurfaces(&input, func(location string, text *string) error {
		if old, ok := seen[location]; ok {
			if old != *text {
				return errors.New("private surface: conflicting repeated surface")
			}
			return nil
		}
		seen[location] = *text
		request := PrivateSurfaceRequest{Location: location, Text: *text}
		for _, value := range protected {
			if strings.Contains(*text, value) {
				request.Protected = append(request.Protected, value)
			}
		}
		requests = append(requests, request)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return requests, nil
}

func visitPrivateSurfaces(output *DatasetArtifact, transform func(string, *string) error) error {
	pair := func(user string, p *protocol.MemoryPair) error {
		if user == "" {
			user = PrimaryUser
		}
		// JSON tuple encoding avoids delimiter collisions and scopes repeated
		// pair IDs to their user graph. Wave/tool copies reuse exactly one draw.
		key, _ := json.Marshal([]string{"pair", user, p.PairID})
		if err := transform(string(key)+":prompt", &p.Prompt); err != nil {
			return err
		}
		return transform(string(key)+":response", &p.Response)
	}
	for i := range output.ToolCases {
		c := &output.ToolCases[i]
		if err := transform("tool:"+c.ID, &c.Prompt); err != nil {
			return err
		}
		for j := range c.PrerequisitePairs {
			if err := pair(PrimaryUser, &c.PrerequisitePairs[j]); err != nil {
				return err
			}
		}
	}
	for i := range output.MemoryCases {
		c := &output.MemoryCases[i]
		if err := transform("case:"+c.ID, &c.Question); err != nil {
			return err
		}
	}
	for i := range output.MemoryWaves {
		w := &output.MemoryWaves[i]
		for j := range w.Pairs {
			if err := pair(w.UserID, &w.Pairs[j]); err != nil {
				return err
			}
		}
	}
	return nil
}
