"""Which Dockerfile edits move the build surface for a lineage-scoped review."""

from __future__ import annotations

import pytest

from ditto.api_server.review_lineage import (
    dockerfile_build_surface_changed,
    is_build_surface,
    is_dockerfile,
)

_BASE = (
    "FROM python:3.12-slim\n"
    "ENV PYTHONUNBUFFERED=1\n"
    "# switches\n"
    "ENV HARNESS_KG=1 HARNESS_CARD=1 \\\n"
    "    HARNESS_EDGES=1\n"
    "WORKDIR /srv\n"
    "COPY requirements.txt ./\n"
    "RUN pip install -r requirements.txt\n"
    "COPY app ./app\n"
    'CMD ["uvicorn", "app.main:app"]\n'
)


def test_dockerfile_names() -> None:
    assert is_dockerfile("Dockerfile")
    assert is_dockerfile("build/agent.dockerfile")
    assert is_dockerfile("Containerfile")
    assert not is_dockerfile("requirements.txt")
    assert is_build_surface("Dockerfile")
    assert is_build_surface("requirements.txt")


@pytest.mark.parametrize(
    "candidate",
    [
        # a new switch on a continued ENV line
        _BASE.replace("    HARNESS_EDGES=1\n", "    HARNESS_EDGES=1 HARNESS_VIEW=1\n"),
        # a switch flipped and a comment reworded
        _BASE.replace("HARNESS_KG=1", "HARNESS_KG=0").replace("# switches", "# parts"),
        # metadata only
        _BASE + 'LABEL org.opencontainers.image.title="agent"\n',
        # whitespace and blank lines only
        _BASE.replace("WORKDIR /srv\n", "\nWORKDIR   /srv\n"),
    ],
)
def test_runtime_only_edits_keep_the_diff_scope(candidate: str) -> None:
    assert not dockerfile_build_surface_changed(_BASE, candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        _BASE.replace("python:3.12-slim", "python:3.13-slim"),
        _BASE.replace(
            "RUN pip install -r requirements.txt", "RUN pip install -r req.txt"
        ),
        _BASE.replace('CMD ["uvicorn", "app.main:app"]', 'CMD ["python", "-m", "app"]'),
        _BASE.replace("COPY app ./app", "COPY . ./app"),
        _BASE.replace(
            "FROM python:3.12-slim", "ARG BASE=python:3.12-slim\nFROM ${BASE}"
        ),
        # an ENV that steers what RUN installs
        _BASE.replace(
            "ENV PYTHONUNBUFFERED=1", "ENV PYTHONUNBUFFERED=1 PIP_INDEX_URL=https://x/"
        ),
        _BASE.replace("ENV PYTHONUNBUFFERED=1", "ENV PATH=/opt/bin:$PATH"),
        _BASE + "ENV HTTPS_PROXY=http://proxy:3128\n",
    ],
)
def test_build_edits_force_a_full_review(candidate: str) -> None:
    assert dockerfile_build_surface_changed(_BASE, candidate)


def test_added_or_removed_dockerfile_is_build_surface() -> None:
    assert dockerfile_build_surface_changed(None, _BASE)
    assert dockerfile_build_surface_changed(_BASE, None)
    assert not dockerfile_build_surface_changed(_BASE, _BASE)


def test_legacy_env_form_is_read_by_name() -> None:
    assert not dockerfile_build_surface_changed(
        _BASE, _BASE + "ENV HARNESS_LEGACY on\n"
    )
    assert dockerfile_build_surface_changed(_BASE, _BASE + "ENV PIP_NO_CACHE_DIR 1\n")
