"""Released and deployed images never float on a mutable upstream tag.

A tag such as ``alpine:3.22`` or ``distroless/static:nonroot`` can be repointed
upstream, so the next release would silently build on different bytes. The
digest makes that an explicit, reviewed change; Dependabot (``docker`` on
``/services/dittobench-api`` and ``/apps/platform/docker/embedder``) still
proposes updates for it. The frozen compat-2 relay is deliberately outside
Dependabot: its pins move only with an audited compatibility decision.

The same rule covers runtime sidecars a deployed host pulls by reference: the
Platform compose services that run outside the ``local`` profile and the
Ansible-managed validator Pylon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = (
    ROOT / "services/dittobench-api/Dockerfile",
    ROOT / "services/dittobench-api/Dockerfile.egress-proxy",
    # Published by release.yml as the compat-2 model-relay index.
    ROOT / "services/dittobench-api/compat/model-relay/Dockerfile",
    # Built and pushed for the Cloud Run code-embedder (embedder.tf).
    ROOT / "apps/platform/docker/embedder/Dockerfile",
)
PLATFORM_COMPOSE = ROOT / "apps/platform/docker-compose.yml"
ANSIBLE_ROOT = ROOT / "infra/ansible"
VALIDATOR_PYLON_DEFAULTS = ANSIBLE_ROOT / "roles/validator_pylon/defaults/main.yml"
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_LATEST_IMAGE = re.compile(r"[\w./-]+:latest\b")
_FROM = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M
)


def _external_bases(text: str) -> list[str]:
    stages: set[str] = set()
    bases: list[str] = []
    for match in _FROM.finditer(text):
        image, alias = match.group(1), match.group(2)
        if image != "scratch" and image.lower() not in stages:
            bases.append(image)
        if alias:
            stages.add(alias.lower())
    return bases


@pytest.mark.parametrize(
    "dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(ROOT))
)
def test_external_bases_are_digest_pinned(dockerfile: Path) -> None:
    bases = _external_bases(dockerfile.read_text())
    assert bases, f"{dockerfile} has no external FROM line"
    unpinned = [base for base in bases if not _DIGEST.search(base)]
    name = dockerfile.relative_to(ROOT)
    assert not unpinned, f"{name} has tag-only bases: {unpinned}"


def test_stage_references_are_not_treated_as_bases() -> None:
    text = "FROM golang:1@sha256:" + "a" * 64 + " AS build\nFROM build AS copy\n"
    assert _external_bases(text) == ["golang:1@sha256:" + "a" * 64]


def _deployable_compose_images() -> dict[str, str]:
    """Images a deployed Platform host can start (``DITTO_COMPOSE_SERVICES``).

    ``local``-profile services (Postgres, MinIO) never run on a deployed host:
    Postgres is a dedicated VM and object storage is GCS there.
    """
    services = yaml.safe_load(PLATFORM_COMPOSE.read_text())["services"]
    return {
        name: service["image"]
        for name, service in services.items()
        if "local" not in service.get("profiles", [])
    }


def test_deployable_platform_compose_images_are_digest_pinned() -> None:
    images = _deployable_compose_images()
    # Pylon runs on every host; the embedder is an opt-in deployed profile.
    assert {"pylon", "embedder"} <= images.keys()
    unpinned = {
        name: image for name, image in images.items() if not _DIGEST.search(image)
    }
    assert not unpinned, f"deployable compose services float on a tag: {unpinned}"
    floating = {name: image for name, image in images.items() if ":latest@" in image}
    assert not floating, f"pin a release tag, not latest: {floating}"


def test_ansible_never_deploys_a_latest_image() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}: {match.group(0)}"
        for path in sorted(ANSIBLE_ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".cfg", ".sh"}
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        for match in _LATEST_IMAGE.finditer(line)
        if not line.lstrip().startswith("#")
    ]
    assert not offenders, "floating :latest image refs:\n" + "\n".join(offenders)


def test_validator_pylon_matches_the_platform_sidecar_pin() -> None:
    defaults = yaml.safe_load(VALIDATOR_PYLON_DEFAULTS.read_text())
    image = defaults["validator_pylon_image"]
    assert _DIGEST.search(image), image
    assert image == _deployable_compose_images()["pylon"]


def test_latest_scan_detects_a_floating_tag() -> None:
    assert _LATEST_IMAGE.search('image: "backenddevelopersltd/bittensor-pylon:latest"')
    assert not _LATEST_IMAGE.search("ghcr.io/x/y:2.3.3@sha256:" + "a" * 64)
