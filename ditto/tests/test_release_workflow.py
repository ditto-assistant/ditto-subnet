from pathlib import Path

import yaml

RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"


def test_release_build_authenticates_only_the_private_screener_dependency() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    steps = workflow["jobs"]["release"]["steps"]
    setup = next(
        step
        for step in steps
        if step.get("name") == "Configure private dependency access"
    )
    release = next(
        step
        for step in steps
        if step.get("name") == "Version, tag, and create the GitHub release"
    )
    cleanup = next(
        step for step in steps if step.get("name") == "Remove private dependency key"
    )

    assert setup["env"]["DITTO_SCREENER_PROTOCOL_READ_KEY"] == (
        "${{ secrets.DITTO_SCREENER_PROTOCOL_READ_KEY }}"
    )
    assert "$RUNNER_TEMP/ditto-screener-read-key" in setup["run"]
    assert release["env"]["GIT_CONFIG_COUNT"] == "1"
    assert release["env"]["GIT_CONFIG_KEY_0"] == (
        "url.git@github.com:ditto-assistant/ditto-screener.git.insteadOf"
    )
    assert release["env"]["GIT_CONFIG_VALUE_0"] == (
        "https://github.com/ditto-assistant/ditto-screener.git"
    )
    assert (
        "/github/runner_temp/ditto-screener-read-key"
        in release["env"]["GIT_SSH_COMMAND"]
    )
    assert cleanup["if"] == "always()"
    assert cleanup["run"] == 'rm -f "$RUNNER_TEMP/ditto-screener-read-key"'
