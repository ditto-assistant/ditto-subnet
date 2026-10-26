"""Which Dockerfile edits move the build surface for a lineage-scoped review."""

from __future__ import annotations

from uuid import uuid4

import pytest

from ditto.api_server.review_lineage import (
    ReviewLineageContext,
    _ArtifactRef,
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


_RUN_READS_ENV = _BASE.replace(
    "RUN pip install -r requirements.txt",
    "ENV EXTRAS=\nRUN pip install -r requirements.txt ${EXTRAS}",
)


def test_an_env_a_run_step_reads_is_build_surface() -> None:
    # The RUN line is unchanged, but what it installs now depends on the value.
    assert dockerfile_build_surface_changed(
        _RUN_READS_ENV, _RUN_READS_ENV.replace("ENV EXTRAS=\n", "ENV EXTRAS=dev\n")
    )
    # an unrelated switch in the same file still keeps the diff scope
    assert not dockerfile_build_surface_changed(
        _RUN_READS_ENV, _RUN_READS_ENV.replace("HARNESS_KG=1", "HARNESS_KG=0")
    )


def test_run_reference_matches_whole_names_only() -> None:
    # RUN reads $EXTRAS; a changed ENV EXTRA (a prefix of it) is not read.
    base = _RUN_READS_ENV.replace("ENV EXTRAS=\n", "ENV EXTRAS=\nENV EXTRA=\n")
    assert not dockerfile_build_surface_changed(
        base, base.replace("ENV EXTRA=\n", "ENV EXTRA=dev\n")
    )
    braces = _RUN_READS_ENV.replace("${EXTRAS}", "${EXTRAS:-none}")
    assert dockerfile_build_surface_changed(
        braces, braces.replace("ENV EXTRAS=\n", "ENV EXTRAS=dev\n")
    )


_AGENT, _ATTEMPT, _PARENT = uuid4(), uuid4(), uuid4()


def _context(rulings: list[dict[str, object]]) -> ReviewLineageContext:
    ref = _ArtifactRef(_PARENT, "ab" * 32)
    return ReviewLineageContext(
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        artifact_sha256="de" * 32,
        policy_version=13,
        candidate=ref,
        parent=ref,
        parent_row={"agent_id": ref.agent_id, "version": 1},
        history=[{"agent_id": ref.agent_id, "version": 1}],
        rulings=rulings,
        findings=[],
    )


def test_history_digest_moves_with_the_rulings() -> None:
    ruling: dict[str, object] = {
        "court": "quarantine",
        "resolution": "release",
        "decided_at": "t1",
    }
    assert _context([ruling]).history_digest() == _context([ruling]).history_digest()
    assert _context([ruling]).history_digest() != _context([]).history_digest()
