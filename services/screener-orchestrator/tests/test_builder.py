import base64
import json

from screener_capacity.builder import _docker_config, _kaniko_script


def test_registry_config_uses_short_lived_oauth_token() -> None:
    encoded = _docker_config(
        "us-central1-docker.pkg.dev/p/r/screener:sha-a", "short-lived-token"
    )
    value = json.loads(base64.b64decode(encoded))
    assert (
        value["auths"]["us-central1-docker.pkg.dev"]["username"] == "oauth2accesstoken"
    )
    assert (
        value["auths"]["us-central1-docker.pkg.dev"]["password"] == "short-lived-token"
    )


def test_kaniko_job_is_bound_to_exact_monorepo_sha_and_paths() -> None:
    sha = "a" * 40
    build = {
        "source_repository": "https://github.com/ditto-assistant/ditto-subnet.git",
        "source_sha": sha,
        "dockerfile_path": "workers/screener/Dockerfile",
        "destination": "us-central1-docker.pkg.dev/p/r/screener:sha-a",
    }
    script = _kaniko_script(build)
    assert f"/archive/{sha}.tar.gz" in script
    assert f"--context=/workspace/src/ditto-subnet-{sha}" in script
    assert "--dockerfile=workers/screener/Dockerfile" in script
    assert "DITTO_BUILD_DIGEST=" in script
    assert "short-lived-token" not in script
