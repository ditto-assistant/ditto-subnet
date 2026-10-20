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
		GitRef:    "2d4c8c53c18be3926dfc954c1ea534e327e84f2b",
		GitSubdir: "miners/dittobench-starter-kit",
	}); msg != "" {
		t.Fatalf("valid git source rejected: %s", msg)
	}
}

func TestSourceFromReqPreservesGitSubdir(t *testing.T) {
	source := sourceFromReq(submitRequest{
		GitURL:    "https://github.com/ditto-assistant/ditto-subnet",
		GitRef:    "2d4c8c53c18be3926dfc954c1ea534e327e84f2b",
		GitSubdir: "miners/dittobench-starter-kit",
	})
	if source.GitSubdir != "miners/dittobench-starter-kit" {
		t.Fatalf("git subdir = %q", source.GitSubdir)
	}
}

func TestGitSourceRequiresImmutableCommit(t *testing.T) {
	for _, ref := range []string{"", "main", "v1.0.0", "2d4c8c53", "2D4C8C53C18BE3926DFC954C1EA534E327E84F2B", "0000000000000000000000000000000000000000"} {
		if msg := validateGitSourceOptions(submitRequest{GitURL: "https://example.com/miner.git", GitRef: ref}); msg != "git_ref must be a full 40-character lowercase commit SHA" {
			t.Fatalf("ref %q: unexpected validation result %q", ref, msg)
		}
	}
	if msg := validateGitSourceOptions(submitRequest{GitRef: "2d4c8c53c18be3926dfc954c1ea534e327e84f2b"}); msg != "git_ref requires git_url" {
		t.Fatalf("orphan git_ref: %q", msg)
	}
}
