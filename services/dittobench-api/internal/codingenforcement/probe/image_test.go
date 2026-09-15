package probe

import (
	"context"
	"errors"
	"strings"
	"testing"
)

type fakeDocker struct {
	responses map[string][]byte
	errs      map[string]error
	calls     []string
}

func (f *fakeDocker) Output(_ context.Context, args ...string) ([]byte, error) {
	key := strings.Join(args, " ")
	f.calls = append(f.calls, key)
	if err, ok := f.errs[key]; ok {
		return f.responses[key], err
	}
	if body, ok := f.responses[key]; ok {
		return body, nil
	}
	return nil, errors.New("unexpected docker call: " + key)
}

const approvedRef = "registry.example/coding-python@sha256:" + sixtyFour

const sixtyFour = "1111111111111111111111111111111111111111111111111111111111111111"

func approvedInspection(id string) string {
	return `[{"Id":"sha256:` + id + `","RepoDigests":["registry.example/coding-python@sha256:` + sixtyFour +
		`"],"Os":"linux","Architecture":"amd64","Config":{"Volumes":null,"Env":[],"Labels":{"io.heyditto.dittobench.coding-supervisor-contract":"1"}}}]`
}

func TestResolveApprovedImageMatchesRepoDigestWithoutPulling(t *testing.T) {
	id := "2222222222222222222222222222222222222222222222222222222222222222"
	docker := &fakeDocker{responses: map[string][]byte{
		"image inspect " + approvedRef: []byte(approvedInspection(id)),
	}}
	resolved, err := ResolveApprovedImage(context.Background(), docker, approvedRef)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ID != "sha256:"+id {
		t.Fatalf("resolved id = %s", resolved.ID)
	}
	for _, call := range docker.calls {
		if strings.HasPrefix(call, "pull") || strings.Contains(call, " pull ") {
			t.Fatalf("resolver pulled an image: %q", call)
		}
	}
}

func TestResolveApprovedImageRefusesAMissingImageInsteadOfPulling(t *testing.T) {
	docker := &fakeDocker{
		responses: map[string][]byte{"image inspect " + approvedRef: []byte("Error: No such image")},
		errs:      map[string]error{"image inspect " + approvedRef: errors.New("exit status 1")},
	}
	_, err := ResolveApprovedImage(context.Background(), docker, approvedRef)
	if err == nil || !strings.Contains(err.Error(), "refusing to pull") {
		t.Fatalf("missing image must be refused without a pull, got %v", err)
	}
}

func TestResolveApprovedImageRefusesADigestMismatch(t *testing.T) {
	other := `[{"Id":"sha256:` + sixtyFour + `","RepoDigests":["registry.example/coding-python@sha256:` +
		"3333333333333333333333333333333333333333333333333333333333333333" +
		`"],"Os":"linux","Architecture":"amd64","Config":{"Labels":{"io.heyditto.dittobench.coding-supervisor-contract":"1"}}}]`
	docker := &fakeDocker{responses: map[string][]byte{"image inspect " + approvedRef: []byte(other)}}
	_, err := ResolveApprovedImage(context.Background(), docker, approvedRef)
	if err == nil || !strings.Contains(err.Error(), "approved repository digest") {
		t.Fatalf("a digest mismatch must be refused, got %v", err)
	}
}

func TestDaemonPostureRefusesRootfulOrUnlabelledDaemons(t *testing.T) {
	for name, docker := range map[string]*fakeDocker{
		"rootful": {responses: map[string][]byte{
			"info --format {{json .SecurityOptions}}": []byte(`["name=seccomp"]`),
		}},
		"unlabelled": {responses: map[string][]byte{
			"info --format {{json .SecurityOptions}}": []byte(`["name=rootless"]`),
			"info --format {{json .Labels}}":          []byte(`[]`),
		}},
	} {
		if err := requireRootlessIsolatedDaemon(context.Background(), docker); err == nil {
			t.Fatalf("%s daemon was accepted", name)
		}
	}
}
