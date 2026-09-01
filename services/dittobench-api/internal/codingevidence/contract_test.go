package codingevidence

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type vectors struct {
	Schema                string          `json:"schema"`
	CodingContractVersion int             `json:"coding_contract_version"`
	WeightEligible        bool            `json:"weight_eligible"`
	Policies              []policy        `json:"policies"`
	Capability            json.RawMessage `json:"capability"`
	Finalization          json.RawMessage `json:"finalization"`
}

type policy struct {
	EvidenceKind     Kind  `json:"evidence_kind"`
	MaximumSizeBytes int64 `json:"maximum_size_bytes"`
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_sealed_evidence_upload_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var value vectors
	if err := json.Unmarshal(body, &value); err != nil {
		t.Fatal(err)
	}
	if value.Schema != "dittobench-coding-sealed-evidence-upload-vector-v1" ||
		value.CodingContractVersion != 1 || value.WeightEligible {
		t.Fatal("shared sealed evidence vector authority is invalid")
	}
	return value
}

func TestGoAcceptsSharedSealedEvidenceVector(t *testing.T) {
	value := loadVectors(t)
	wire, err := DecodeWireUploadCapability(value.Capability)
	if err != nil {
		t.Fatal(err)
	}
	capability, err := wire.ToCapability()
	if err != nil {
		t.Fatal(err)
	}
	if capability.EvidenceKind != KindAuthoringTranscript || capability.SizeBytes != 4096 ||
		capability.URL != wire.URL {
		t.Fatal("wire capability conversion drifted")
	}
	if encoded, err := json.Marshal(capability); err == nil || strings.Contains(string(encoded), "synthetic-evidence") {
		t.Fatalf("internal capability serialized: %q, err=%v", encoded, err)
	}
	finalization, err := DecodeWireFinalization(value.Finalization)
	if err != nil {
		t.Fatal(err)
	}
	if !finalization.Accepted || finalization.Idempotent || finalization.UploadID != wire.UploadID {
		t.Fatal("finalization authority drifted")
	}
}

func TestGoPoliciesMatchSharedVector(t *testing.T) {
	value := loadVectors(t)
	if len(value.Policies) != len(maximumSize) {
		t.Fatalf("policy count = %d", len(value.Policies))
	}
	seen := make(map[Kind]bool)
	for _, policy := range value.Policies {
		maximum, present := maximumSize[policy.EvidenceKind]
		if !present || maximum != policy.MaximumSizeBytes || seen[policy.EvidenceKind] {
			t.Fatalf("policy for %q drifted", policy.EvidenceKind)
		}
		seen[policy.EvidenceKind] = true
	}
	for kind := range maximumSize {
		if !seen[kind] {
			t.Fatalf("missing policy for %q", kind)
		}
	}
}

func TestDecoderIgnoresUnknownAndRejectsKnownFieldDrift(t *testing.T) {
	raw := loadVectors(t).Capability
	var extended map[string]any
	if err := json.Unmarshal(raw, &extended); err != nil {
		t.Fatal(err)
	}
	extended["future_transport_hint"] = map[string]any{"ignored": true}
	body, err := json.Marshal(extended)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeWireUploadCapability(body); err != nil {
		t.Fatalf("unknown field was not ignored: %v", err)
	}

	duplicate := bytes.Replace(
		raw,
		[]byte(`"claim_generation": 7,`),
		[]byte(`"claim_generation": 7, "claim_generation": 7,`),
		1,
	)
	if _, err := DecodeWireUploadCapability(duplicate); err == nil {
		t.Fatal("duplicate known field was accepted")
	}

	for name, mutate := range map[string]func(*WireUploadCapability){
		"zero ticket": func(value *WireUploadCapability) {
			value.TicketID = "00000000-0000-0000-0000-000000000000"
		},
		"zero claim":   func(value *WireUploadCapability) { value.ClaimGeneration = 0 },
		"unknown kind": func(value *WireUploadCapability) { value.EvidenceKind = "unknown" },
		"checksum": func(value *WireUploadCapability) {
			value.ChecksumSHA256B64 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
		},
		"oversized": func(value *WireUploadCapability) { value.SizeBytes = (512 << 20) + 1 },
		"wrong path": func(value *WireUploadCapability) {
			value.URL = strings.Replace(value.URL, "authoring-transcript", "frozen-submission", 1)
		},
	} {
		t.Run(name, func(t *testing.T) {
			wire, err := DecodeWireUploadCapability(raw)
			if err != nil {
				t.Fatal(err)
			}
			mutate(&wire)
			body, err := json.Marshal(wire)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := DecodeWireUploadCapability(body); err == nil {
				t.Fatal("drifted capability was accepted")
			}
		})
	}
}

func TestWireFormattingRedactsBearerURL(t *testing.T) {
	wire, err := DecodeWireUploadCapability(loadVectors(t).Capability)
	if err != nil {
		t.Fatal(err)
	}
	for _, rendered := range []string{fmt.Sprint(wire), fmt.Sprintf("%+v", wire), fmt.Sprintf("%#v", wire)} {
		if strings.Contains(rendered, "synthetic-evidence") || strings.Contains(rendered, wire.URL) {
			t.Fatalf("wire capability leaked: %s", rendered)
		}
	}
	var structured bytes.Buffer
	slog.New(slog.NewJSONHandler(&structured, nil)).Info("wire", "value", wire)
	if strings.Contains(structured.String(), "synthetic-evidence") || strings.Contains(structured.String(), wire.URL) {
		t.Fatalf("structured log leaked: %s", structured.String())
	}
}
