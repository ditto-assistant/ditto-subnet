package main

import "testing"

func TestGitSubdirRequiresGitSource(t *testing.T) {
	if msg := validateGitSourceOptions(submitRequest{
		TarballURL: "https://example.test/source.tgz",
		GitSubdir:  "miners/dittobench-starter-kit",
	}); msg != "git_subdir requires git_url" {
		t.Fatalf("unexpected validation result %q", msg)
	}
	if msg := validateGitSourceOptions(submitRequest{
		GitURL:    "https://github.com/ditto-assistant/ditto-subnet",
		GitSubdir: "miners/dittobench-starter-kit",
	}); msg != "" {
		t.Fatalf("valid git source rejected: %s", msg)
	}
}

func TestSourceFromReqPreservesGitSubdir(t *testing.T) {
	source := sourceFromReq(submitRequest{
		GitURL:    "https://github.com/ditto-assistant/ditto-subnet",
		GitRef:    "main",
		GitSubdir: "miners/dittobench-starter-kit",
	})
	if source.GitSubdir != "miners/dittobench-starter-kit" {
		t.Fatalf("git subdir = %q", source.GitSubdir)
	}
}
