# Repository guidance

## Git-backed Docker build contexts

- Never pin a Git build context with the legacy URL-fragment form
  `https://github.com/OWNER/REPO.git#<commit-sha>`. A clean Docker builder may
  fail because the commit SHA is not an advertised branch or tag.
- For a commit on `main`, use BuildKit's structured query syntax with both an
  advertised ref and the full 40-character checksum:
  `https://github.com/OWNER/REPO.git?ref=refs/heads/main&checksum=<commit-sha>`.
  The ref makes the repository fetchable, and the checksum makes the build
  fail closed if the ref does not resolve to the expected commit.
- Apply the same rule to other branches or tags: use their fully qualified ref
  plus a full checksum. Do not replace an immutable checksum with a floating
  ref alone.
- Verify every changed remote Git context with a fresh or empty BuildKit
  builder. A successful build against a warm local cache is not sufficient.

