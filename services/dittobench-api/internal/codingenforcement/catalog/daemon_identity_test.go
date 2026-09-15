package catalog

import (
	"errors"
	"maps"
	"os"
	"testing"
)

// daemonIdentityVectorFile is shared with native.py, the host preflight and the
// offline evidence tool's Python tests.
const daemonIdentityVectorFile = "testdata/daemon-identity-vector-v1.json"

func daemonIdentityVector(t *testing.T) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(daemonIdentityVectorFile)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	vector := decoded.(map[string]any)
	if vector["schema"] != "dittobench-coding-native-daemon-identity-vectors-v1" {
		t.Fatalf("schema = %v", vector["schema"])
	}
	return vector
}

func TestDaemonIdentityVectorAgreesWithPython(t *testing.T) {
	vector := daemonIdentityVector(t)
	info := vector["info"].(map[string]any)
	socket := vector["socket_path"].(string)
	identity, err := DaemonIdentityFromInfo(info, socket)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := identity.Canonical()
	if err != nil {
		t.Fatal(err)
	}
	if string(canonical) != vector["identity_canonical"] {
		t.Fatalf("canonical identity = %s", canonical)
	}
	expected, err := Canonical(vector["identity"])
	if err != nil || string(expected) != string(canonical) {
		t.Fatalf("identity object differs from its canonical form: %v", err)
	}
	digest, err := identity.SHA256()
	if err != nil || digest != vector["identity_sha256"] {
		t.Fatalf("identity digest = %s, %v", digest, err)
	}
	if digest != digestHex(canonical) {
		t.Fatal("digest is not the sha256 of the canonical bytes")
	}

	for key, value := range vector["volatile"].(map[string]any) {
		changed := maps.Clone(info)
		changed[key] = value
		other, err := DaemonIdentityFromInfo(changed, socket)
		if err != nil {
			t.Fatalf("volatile %s refused: %v", key, err)
		}
		if got, _ := other.SHA256(); got != digest {
			t.Errorf("volatile %s changed the identity", key)
		}
	}
	for key, value := range vector["binding"].(map[string]any) {
		changed := maps.Clone(info)
		changed[key] = value
		other, err := DaemonIdentityFromInfo(changed, socket)
		if err != nil {
			t.Fatalf("binding %s refused: %v", key, err)
		}
		if got, _ := other.SHA256(); got == digest {
			t.Errorf("binding %s did not change the identity", key)
		}
	}
	for key, value := range vector["refused"].(map[string]any) {
		changed := maps.Clone(info)
		changed[key] = value
		if _, err := DaemonIdentityFromInfo(changed, socket); !errors.Is(err, ErrDaemonIdentity) {
			t.Errorf("refused %s accepted: %v", key, err)
		}
		delete(changed, key)
		if _, err := DaemonIdentityFromInfo(changed, socket); !errors.Is(err, ErrDaemonIdentity) {
			t.Errorf("missing %s accepted: %v", key, err)
		}
	}
	other, err := DaemonIdentityFromInfo(info, "/run/other/docker.sock")
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := other.SHA256(); got == digest {
		t.Error("a different socket kept the identity")
	}
	for _, socket := range []string{"", "relative.sock", "/run/../run/docker.sock", "/run/with space.sock"} {
		if _, err := DaemonIdentityFromInfo(info, socket); !errors.Is(err, ErrDaemonIdentity) {
			t.Errorf("socket %q accepted", socket)
		}
	}
	duplicated := maps.Clone(info)
	duplicated["SecurityOptions"] = []any{"name=rootless", "name=rootless"}
	if _, err := DaemonIdentityFromInfo(duplicated, socket); !errors.Is(err, ErrDaemonIdentity) {
		t.Error("repeated security option accepted")
	}
}
