package main

import (
	"errors"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestValidateScreenedImage(t *testing.T) {
	valid := submitRequest{
		TarballURL:          "https://example.com/source.tar.gz",
		ScreenedImageURL:    "https://example.com/image.tar.gz",
		ScreenedImageSHA256: "ab" + repeat("0", 62),
		ScreenedImageID:     "sha256:" + repeat("1", 64),
		ScreenedImageRef:    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest",
		ScreenedImageSize:   123,
	}
	if msg := validateScreenedImage(valid); msg != "" {
		t.Fatalf("valid screened image rejected: %s", msg)
	}
	for _, tc := range []struct {
		name   string
		mutate func(*submitRequest)
	}{
		{"uppercase image id", func(req *submitRequest) { req.ScreenedImageID = "sha256:" + repeat("A", 64) }},
		{"empty ref id", func(req *submitRequest) { req.ScreenedImageRef = "ditto-screen/:latest" }},
		{"nested ref", func(req *submitRequest) {
			req.ScreenedImageRef = "ditto-screen/nested/550e8400-e29b-41d4-a716-446655440000:latest"
		}},
		{"noncanonical uuid", func(req *submitRequest) {
			req.ScreenedImageRef = "ditto-screen/550E8400-E29B-41D4-A716-446655440000:latest"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := valid
			tc.mutate(&req)
			if msg := validateScreenedImage(req); msg == "" {
				t.Fatal("malformed screened image metadata accepted")
			}
		})
	}
	valid.TarballURL = ""
	if msg := validateScreenedImage(valid); msg == "" {
		t.Fatal("screened image without source tarball was accepted")
	}
}

func TestValidateScreenedImageAccess(t *testing.T) {
	req := submitRequest{ScreenedImageURL: "https://example.com/image.tar"}
	if msg := validateScreenedImageAccess(req, false); msg == "" {
		t.Fatal("public practice API accepted the screened-image bypass")
	}
	if msg := validateScreenedImageAccess(req, true); msg != "" {
		t.Fatalf("validator sandbox rejected screened image: %s", msg)
	}
	if msg := validateScreenedImageAccess(submitRequest{}, false); msg != "" {
		t.Fatalf("request without a screened-image bypass rejected: %s", msg)
	}
}

func TestValidateBenchmarkImageContract(t *testing.T) {
	for _, benchVersion := range []int{protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10} {
		if msg := validateBenchmarkImageContract(submitRequest{BenchVersion: benchVersion, TarballURL: "https://example.com/source.tgz"}); msg == "" {
			t.Fatalf("benchmark v%d allowed an untrusted source build", benchVersion)
		}
		if msg := validateBenchmarkImageContract(submitRequest{BenchVersion: benchVersion, ScreenedImageURL: "https://example.com/image.tar"}); msg != "" {
			t.Fatalf("benchmark v%d screened image rejected: %s", benchVersion, msg)
		}
	}
	if msg := validateBenchmarkImageContract(submitRequest{BenchVersion: protocol.BenchVersionV7, TarballURL: "https://example.com/source.tgz"}); msg != "" {
		t.Fatalf("benchmark v7 source-build compatibility rejected: %s", msg)
	}
}

func TestSandboxStartInfraFailure(t *testing.T) {
	// A missing sandbox egress network is the validator's own fault, not the
	// agent's: it must be retryable validator_infrastructure so the worker backs
	// off rather than blaming the miner and re-leasing in a tight loop.
	netErr := errors.New("container start failed: docker run failed: ... failed to set up container networking: network ditto-sandbox not found")
	failure := sandboxStartInfraFailure(netErr)
	if failure == nil {
		t.Fatal("missing sandbox network was not classified as infrastructure")
	}
	if failure.Kind != "validator_infrastructure" || !failure.Retryable || failure.Code != "sandbox_network_unavailable" {
		t.Fatalf("unexpected infra classification: %+v", failure)
	}
	// The scorer creates dittobench-sub tags itself. If one disappears before a
	// compatibility restart, that is likewise validator infrastructure and must
	// never consume the miner's retry budget as a scoring error.
	imageErr := errors.New("docker run failed: Unable to find image 'dittobench-sub:agent-image-123' locally; pull access denied for dittobench-sub")
	failure = sandboxStartInfraFailure(imageErr)
	if failure == nil {
		t.Fatal("missing request-scoped image was not classified as infrastructure")
	}
	if failure.Kind != "validator_infrastructure" || !failure.Retryable || failure.Code != "sandbox_image_unavailable" {
		t.Fatalf("unexpected missing-image classification: %+v", failure)
	}
	// An ordinary harness crash stays a submission failure (nil classifier).
	if got := sandboxStartInfraFailure(errors.New("exit status 1: panic in harness")); got != nil {
		t.Fatalf("harness crash misclassified as infrastructure: %+v", got)
	}
	if got := sandboxStartInfraFailure(nil); got != nil {
		t.Fatalf("nil error produced a failure: %+v", got)
	}
}

func repeat(value string, count int) string {
	result := ""
	for range count {
		result += value
	}
	return result
}
