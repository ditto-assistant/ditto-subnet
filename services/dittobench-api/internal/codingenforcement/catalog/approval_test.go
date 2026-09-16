package catalog

import (
	"bytes"
	"errors"
	"os"
	"testing"
)

// Pinned identically in ditto/tests/test_coding_native_matrix.py.
const approvalVectorSHA256 = "c5d49e2f49dede8b95ebe6fd8e9253dd86b1be80e401ac68887cdeff8c2f7ed7"

func TestApprovalVectorMatchesPythonBytes(t *testing.T) {
	raw, err := os.ReadFile("testdata/approval-vector-v3.json")
	if err != nil {
		t.Fatal(err)
	}
	if got := digestHex(raw); got != approvalVectorSHA256 {
		t.Fatalf("approval vector digest = %s", got)
	}
	decoded, err := ParseCanonical(raw)
	if err != nil {
		t.Fatal(err)
	}
	again, err := Canonical(decoded)
	if err != nil || !bytes.Equal(again, raw) {
		t.Fatalf("Go re-encoding differs from the Python approval bytes: %v", err)
	}
	got, err := ApprovalDaemonIdentitySHA256(raw)
	if err != nil {
		t.Fatal(err)
	}
	vector := daemonIdentityVector(t)
	if got != vector["identity_sha256"] {
		t.Fatalf("approval daemon identity digest = %s", got)
	}

	// The signature covers exact stored bytes, so non-canonical whitespace is
	// accepted and names the same daemon.
	spaced := append(bytes.ReplaceAll(raw, []byte(`,"`), []byte(`, "`)), '\n')
	if got, err := ApprovalDaemonIdentitySHA256(spaced); err != nil || got != vector["identity_sha256"] {
		t.Fatalf("non-canonical approval refused: %s %v", got, err)
	}

	for name, mutated := range map[string][]byte{
		"trailing data": append(append([]byte{}, raw...), []byte(`{}`)...),
		"duplicate key": append(append([]byte{}, raw[:len(raw)-1]...), []byte(`,"max_jobs":2}`)...),
		"wrong schema":  bytes.Replace(raw, []byte(ApprovalSchema), []byte("dittobench-coding-native-controls-approval-v2"), 1),
		"extra key":     append(append([]byte{}, raw[:len(raw)-1]...), []byte(`,"zz":1}`)...),
		"missing key":   bytes.Replace(raw, []byte(`"max_jobs":2,`), nil, 1),
	} {
		if _, err := ApprovalDaemonIdentitySHA256(mutated); !errors.Is(err, ErrApproval) {
			t.Errorf("%s accepted: %v", name, err)
		}
	}
}
