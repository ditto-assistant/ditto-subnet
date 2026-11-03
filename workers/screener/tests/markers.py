"""Preconditions shared by the screener worker tests."""

from __future__ import annotations

import os

import pytest

# IsolatedCodingHarness.run refuses uid 0 on its first line, so every test that
# reaches it needs a non-root worker. Containerised development usually runs as
# root; skipping there reports the precondition instead of failing on it, and CI
# runners are non-root so the coverage is unchanged. This marker is shared
# rather than defined per module because the second module to need it did not
# have it, which left eight tests failing and one spinning forever.
non_root_only = pytest.mark.skipif(
    os.getuid() == 0,
    reason="the L2 analyzer harness refuses to run from a root worker",
)
