from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
CORE_E2E = ROOT / ".github/workflows/screener-core-e2e.yml"


def test_core_e2e_uses_the_monorepo_starter_kit() -> None:
    text = CORE_E2E.read_text()
    workflow = yaml.safe_load(text)
    steps = workflow["jobs"]["screener-core-e2e"]["steps"]
    smoke = next(step for step in steps if step.get("name", "").startswith("Run real"))

    assert "repository: ditto-assistant/dittobench-starter-kit" not in text
    assert smoke["env"]["DITTO_STARTER_KIT_DIR"] == (
        "${{ github.workspace }}/miners/dittobench-starter-kit"
    )
    assert 'test -f "$DITTO_STARTER_KIT_DIR/Cargo.toml"' in smoke["run"]
