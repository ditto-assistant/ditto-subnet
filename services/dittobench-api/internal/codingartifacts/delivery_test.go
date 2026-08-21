package codingartifacts

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type deliveryVectors struct {
	Schema                string            `json:"schema"`
	CodingContractVersion int               `json:"coding_contract_version"`
	WeightEligible        bool              `json:"weight_eligible"`
	Policies              []deliveryPolicy  `json:"policies"`
	Capabilities          []json.RawMessage `json:"capabilities"`
}

type deliveryPolicy struct {
	ArtifactKind     Kind            `json:"artifact_kind"`
	Audience         Audience        `json:"audience"`
	MaximumSizeBytes int64           `json:"maximum_size_bytes"`
	DeliveryPhases   []DeliveryPhase `json:"delivery_phases"`
}

func loadDeliveryVectors(t *testing.T) deliveryVectors {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_artifact_capability_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vectors deliveryVectors
	if err := json.Unmarshal(body, &vectors); err != nil {
		t.Fatal(err)
	}
	if vectors.Schema != "dittobench-coding-artifact-capability-vector-v1" ||
		vectors.CodingContractVersion != 1 || vectors.WeightEligible {
		t.Fatal("delivery vector authority is invalid")
	}
	return vectors
}

func TestGoAcceptsEverySharedCapabilityVector(t *testing.T) {
	vectors := loadDeliveryVectors(t)
	observed := make(map[string]bool)
	for _, raw := range vectors.Capabilities {
		wire, err := DecodeWireCapability(raw)
		if err != nil {
			t.Fatal(err)
		}
		internal, err := wire.ToCapability()
		if err != nil {
			t.Fatal(err)
		}
		observed[string(wire.DeliveryPhase)+"/"+string(wire.ArtifactKind)] = true
		if internal.Phase != wire.DeliveryPhase || internal.Kind != wire.ArtifactKind ||
			internal.URL != wire.URL || internal.TicketID != wire.TicketID {
			t.Fatal("wire conversion drifted")
		}
		if _, err := json.Marshal(wire); err != nil {
			t.Fatalf("wire capability cannot serialize: %v", err)
		}
		if encoded, err := json.Marshal(internal); err == nil || strings.Contains(string(encoded), "synthetic-") {
			t.Fatalf("internal capability serialized: %q, err = %v", encoded, err)
		}
	}
	for _, identity := range []string{
		"authoring/visible-bundle", "authoring/memory-bundle", "authoring/resource-profile",
		"grading/visible-bundle", "grading/resource-profile", "grading/grader-bundle",
	} {
		if !observed[identity] {
			t.Fatalf("shared vector lacks %s", identity)
		}
	}
}

func TestGoPoliciesMatchSharedVector(t *testing.T) {
	vectors := loadDeliveryVectors(t)
	if len(vectors.Policies) != 4 {
		t.Fatalf("policy count = %d", len(vectors.Policies))
	}
	seen := make(map[Kind]bool)
	for _, policy := range vectors.Policies {
		if seen[policy.ArtifactKind] {
			t.Fatalf("duplicate policy for %q", policy.ArtifactKind)
		}
		seen[policy.ArtifactKind] = true
		maximum, audience, ok := kindPolicy(policy.ArtifactKind)
		if !ok || maximum != policy.MaximumSizeBytes || audience != policy.Audience {
			t.Fatalf("policy for %q drifted", policy.ArtifactKind)
		}
		phases := make(map[DeliveryPhase]bool)
		for _, phase := range policy.DeliveryPhases {
			phases[phase] = true
		}
		for _, phase := range []DeliveryPhase{PhaseAuthoring, PhaseGrading} {
			if phaseAllows(phase, policy.ArtifactKind) != phases[phase] {
				t.Fatalf("phase policy for %q/%q drifted", phase, policy.ArtifactKind)
			}
		}
	}
	for _, kind := range []Kind{KindVisibleBundle, KindMemoryBundle, KindResourceProfile, KindGraderBundle} {
		if !seen[kind] {
			t.Fatalf("missing policy for %q", kind)
		}
	}
}

func TestGoDeliveryDecoderIgnoresUnknownAndRejectsDuplicateMissingFields(t *testing.T) {
	raw := loadDeliveryVectors(t).Capabilities[0]
	var extended map[string]any
	if err := json.Unmarshal(raw, &extended); err != nil {
		t.Fatal(err)
	}
	extended["future_transport_hint"] = map[string]any{"ignored": true}
	body, err := json.Marshal(extended)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeWireCapability(body); err != nil {
		t.Fatalf("unknown field was not ignored: %v", err)
	}
	extended["ticket_deadline"] = 1_787_317_200
	body, err = json.Marshal(extended)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeWireCapability(body); err == nil {
		t.Fatal("numeric ticket deadline was accepted")
	}
	extended["ticket_deadline"] = "2026-08-21T13:00:00.123456789Z"
	body, err = json.Marshal(extended)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeWireCapability(body); err == nil {
		t.Fatal("nanosecond ticket deadline was accepted")
	}
	extended["ticket_deadline"] = "2026-08-21T13:00:00Z"
	duplicate := bytes.Replace(
		raw,
		[]byte(`"coding_contract_version": 1,`),
		[]byte(`"coding_contract_version": 1, "coding_contract_version": 1,`),
		1,
	)
	if _, err := DecodeWireCapability(duplicate); err == nil {
		t.Fatal("duplicate known field was accepted")
	}
	delete(extended, "weight_eligible")
	body, err = json.Marshal(extended)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeWireCapability(body); err == nil {
		t.Fatal("missing weight_eligible was accepted")
	}
}

func TestGoDeliveryDecoderRejectsPhaseAndAuthorityDrift(t *testing.T) {
	vectors := loadDeliveryVectors(t)
	var visible WireCapability
	if err := json.Unmarshal(vectors.Capabilities[0], &visible); err != nil {
		t.Fatal(err)
	}
	var memory WireCapability
	if err := json.Unmarshal(vectors.Capabilities[1], &memory); err != nil {
		t.Fatal(err)
	}
	tests := map[string]WireCapability{
		"weighted": func() WireCapability { value := visible; value.WeightEligible = true; return value }(),
		"zero ticket": func() WireCapability {
			value := visible
			value.TicketID = "00000000-0000-0000-0000-000000000000"
			return value
		}(),
		"audience":       func() WireCapability { value := visible; value.Audience = AudienceProtectedGrader; return value }(),
		"memory grading": func() WireCapability { value := memory; value.DeliveryPhase = PhaseGrading; return value }(),
		"grader authoring": func() WireCapability {
			value := visible
			value.ArtifactKind, value.Audience = KindGraderBundle, AudienceProtectedGrader
			return value
		}(),
		"oversized": func() WireCapability { value := visible; value.SizeBytes = (2 << 30) + 1; return value }(),
		"signed duration": func() WireCapability {
			value := visible
			value.URL = strings.Replace(value.URL, "X-Amz-Expires=300", "X-Amz-Expires=%2B300", 1)
			return value
		}(),
	}
	for name, value := range tests {
		t.Run(name, func(t *testing.T) {
			body, err := json.Marshal(value)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := DecodeWireCapability(body); err == nil {
				t.Fatal("drifted capability was accepted")
			}
		})
	}
}

func TestWireCapabilityFormattingRedactsBearerURL(t *testing.T) {
	wire, err := DecodeWireCapability(loadDeliveryVectors(t).Capabilities[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, rendered := range []string{fmt.Sprint(wire), fmt.Sprintf("%+v", wire), fmt.Sprintf("%#v", wire)} {
		if strings.Contains(rendered, "synthetic-visible") || strings.Contains(rendered, wire.URL) {
			t.Fatalf("wire capability leaked: %s", rendered)
		}
	}
	var structured bytes.Buffer
	slog.New(slog.NewJSONHandler(&structured, nil)).Info("wire", "value", wire)
	if strings.Contains(structured.String(), "synthetic-visible") || strings.Contains(structured.String(), wire.URL) {
		t.Fatalf("structured log leaked: %s", structured.String())
	}
}

func TestValidateJSONDocumentRejectsTransportBoundaries(t *testing.T) {
	if err := codingcontract.ValidateJSONDocument([]byte(`{"ok":true}`), 64); err != nil {
		t.Fatal(err)
	}
	for _, body := range [][]byte{
		nil,
		bytes.Repeat([]byte(" "), 65),
		[]byte(`{"a":1,"a":2}`),
		[]byte("{\"x\":\"\\ud800\"}"),
		append([]byte(`{"ok":true}`), []byte(` {}`)...),
	} {
		if err := codingcontract.ValidateJSONDocument(body, 64); err == nil {
			t.Fatalf("invalid document accepted: %q", body)
		}
	}
}
