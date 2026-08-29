from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
KIT = ROOT / "miners" / "dittobench-starter-kit"


def test_starter_kit_is_a_first_class_monorepo_component() -> None:
    assert (KIT / "Cargo.toml").is_file()
    assert (KIT / "Dockerfile").is_file()
    assert (KIT / "src" / "baseline.rs").is_file()
    assert not (KIT / ".github" / "workflows" / "ci.yml").exists()

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "starter-kit-ci.yml").read_text()
    )
    assert workflow["defaults"]["run"]["working-directory"] == (
        "miners/dittobench-starter-kit"
    )
    steps = workflow["jobs"]["build-and-test"]["steps"]
    assert any(step.get("run") == "cargo test --locked --verbose" for step in steps)


def test_starter_runtime_is_reproducible_and_non_root() -> None:
    dockerfile = (KIT / "Dockerfile").read_text()
    assert "FROM rust:1-trixie@sha256:" in dockerfile
    assert "FROM debian:trixie-slim@sha256:" in dockerfile
    assert "dittobench:x:65532:65532:" in dockerfile
    assert "USER dittobench:dittobench" in dockerfile
    assert "ENV DITTOBENCH_DB=/tmp/dittobench.db" in dockerfile


def test_starter_default_crate_target_uses_the_monorepo_subdirectory() -> None:
    env = (KIT / ".env.example").read_text()
    submit = (KIT / "src" / "playground" / "submit.rs").read_text()
    for text in (env, submit):
        assert "https://github.com/ditto-assistant/ditto-subnet" in text
        assert "miners/dittobench-starter-kit" in text
