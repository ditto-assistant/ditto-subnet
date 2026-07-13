"""Regression checks for production Compose invariants."""

from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).parents[2] / "docker-compose.yml"


def test_ollama_is_pinned_with_functional_embedding_healthcheck() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    ollama = compose["services"]["ollama"]

    assert ollama["image"] == (
        "docker.io/ollama/ollama:0.11.10@"
        "sha256:a5409cb903d30f9cd67e9f430dd336ddc9274e16fd78f75b675c42065991b4fd"
    )
    probe = " ".join(ollama["healthcheck"]["test"])
    assert "/api/embed" in probe
    assert "embeddinggemma" in probe
    assert " 200 " in probe
