package codingcertifier

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestCertificationReceiptMatchesSharedGoPythonPlatformVector(t *testing.T) {
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_certification_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		Receipt  json.RawMessage `json:"receipt"`
		Expected struct {
			CertificationSHA256 string `json:"certification_sha256"`
		} `json:"expected"`
	}
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	receipt, err := ParseReceipt(vector.Receipt)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.CertificationSHA256 != vector.Expected.CertificationSHA256 {
		t.Fatalf("receipt digest=%s", receipt.CertificationSHA256)
	}
}
