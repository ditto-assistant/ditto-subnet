#!/usr/bin/env python3
"""Every fetch of Docker's apt signing key must be integrity-pinned.

The key installed at /usr/share/keyrings/docker.asc becomes an apt trust root:
whatever it signs installs as root. A substituted key file must therefore fail
closed rather than be trusted, so each fetch has to check the exact bytes, and
every pinned copy of the hash must agree.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
KEY_URL = re.compile(r"https://download\.docker\.com/linux/[a-z]+/gpg")
# Fingerprint 9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88.
CANONICAL_SHA256 = "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"
PIN_DEFINITION = re.compile(r'^\s*docker_apt_key_sha256:\s*"([0-9a-f]*)"\s*$', re.M)
# coding_hosted pins through its own operator-supplied variable and asserts it.
SEPARATELY_PINNED = {"infra/ansible/roles/coding_hosted/tasks/packages.yml"}


def _key_fetch_sites() -> list[Path]:
    sites = []
    for root in ("infra", "workers", "scripts", "services"):
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or path.suffix not in {".yml", ".yaml", ".sh"}:
                continue
            if "tests" in path.relative_to(REPO).parts:
                continue
            fetches = [
                line
                for line in path.read_text(errors="ignore").splitlines()
                if KEY_URL.search(line) and not line.lstrip().startswith("#")
            ]
            if fetches:
                sites.append(path)
    return sites


def _task_block(text: str, url_offset: int) -> str:
    """The Ansible task containing the URL: from its `- name:` to the next one."""
    start = text.rfind("- name:", 0, url_offset)
    end = text.find("- name:", url_offset)
    return text[start : end if end != -1 else len(text)]


class DockerAptKeyPinTest(unittest.TestCase):
    def test_every_key_fetch_is_pinned(self) -> None:
        sites = _key_fetch_sites()
        self.assertGreaterEqual(len(sites), 6, sites)
        for path in sites:
            relative = str(path.relative_to(REPO))
            text = path.read_text()
            for match in KEY_URL.finditer(text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                if text[line_start : match.start()].lstrip().startswith("#"):
                    continue  # a comment naming the URL, not a fetch
                if path.suffix == ".sh":
                    window = text[match.end() : match.end() + 400]
                    self.assertIn(CANONICAL_SHA256, window, relative)
                    self.assertIn("sha256sum --check", window, relative)
                    self.assertNotRegex(
                        text[match.start() - 200 : match.end()],
                        r"curl[^\n]*gpg\s*>\s*/usr/share/keyrings",
                        f"{relative} writes the key before verifying it",
                    )
                    continue
                block = _task_block(text, match.start())
                if relative in SEPARATELY_PINNED:
                    self.assertIn("checksum:", block, relative)
                    continue
                self.assertIn(
                    'checksum: "sha256:{{ docker_apt_key_sha256 }}"', block, relative
                )

    def test_every_pinned_copy_agrees(self) -> None:
        copies = {}
        for path in (REPO / "infra" / "ansible").rglob("*.yml"):
            for value in PIN_DEFINITION.findall(path.read_text()):
                copies[str(path.relative_to(REPO))] = value
        self.assertGreaterEqual(len(copies), 5, copies)
        for location, value in copies.items():
            self.assertEqual(value, CANONICAL_SHA256, location)


if __name__ == "__main__":
    unittest.main()
