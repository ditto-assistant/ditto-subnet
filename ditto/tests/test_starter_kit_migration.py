import re
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


def test_starter_does_not_synthesize_wire_abstention_from_model_prose() -> None:
    # I4/W8 requires optional scorer fields to remain absent unless authored
    # by the model. Guard the served response constructor, not a test helper.
    baseline = (KIT / "src" / "baseline.rs").read_text()
    served_response = baseline.split("let final_text = result.result.text;", 1)[
        1
    ].split("#[cfg(test)]", 1)[0]
    assert re.search(r"abstain:\s*None\b", served_response)
    assert not re.search(r"abstain:\s*\w+\s*\(\s*&?final_text", served_response)
