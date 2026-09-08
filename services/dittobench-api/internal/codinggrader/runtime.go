package codinggrader

import "errors"

// RuntimeEvidence is Platform-private completed-driver evidence. It contains
// commitments only, never source, assertions, expected values or diagnostics.
// The surrounding execution receipt binds it into the sealed evidence chain.
type RuntimeEvidence struct {
	Schema              string `json:"schema"`
	AuthoritySHA256     string `json:"authority_sha256"`
	InputsSHA256        string `json:"inputs_sha256"`
	ImageSHA256         string `json:"image_sha256"`
	ProgramSHA256       string `json:"program_sha256"`
	CompilerSHA256      string `json:"compiler_sha256"`
	BridgeLibrarySHA256 string `json:"bridge_library_sha256"`
	Outcome             string `json:"outcome"`
	BuildSHA256         string `json:"build_sha256,omitempty"`
	ArtifactSHA256      string `json:"artifact_sha256,omitempty"`
}

func (value *RuntimeEvidence) Clone() *RuntimeEvidence {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func (value *RuntimeEvidence) Validate() error {
	if value == nil || value.Schema != "dittobench-coding-rust-runtime-v1" {
		return errors.New("coding runtime evidence schema is invalid")
	}
	for _, digest := range []string{value.AuthoritySHA256, value.InputsSHA256, value.ImageSHA256, value.ProgramSHA256, value.CompilerSHA256, value.BridgeLibrarySHA256} {
		if !lowerSHA256(digest) {
			return errors.New("coding runtime evidence digest is invalid")
		}
	}
	switch value.Outcome {
	case "compile_failed":
		if value.BuildSHA256 != "" || value.ArtifactSHA256 != "" {
			return errors.New("failed compilation claimed an artifact")
		}
	case "evaluated":
		if !lowerSHA256(value.BuildSHA256) || !lowerSHA256(value.ArtifactSHA256) {
			return errors.New("completed runtime lacks artifact commitments")
		}
	default:
		return errors.New("coding runtime evidence outcome is invalid")
	}
	return nil
}
