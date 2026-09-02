"""Real-Docker core-v6 coverage for build, isolated startup, and health."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import textwrap
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ditto_screener.config import ScreenerConfig
from ditto_screener.gate import BuildGate, BuiltImageArtifact
from ditto_screener.policy import (
    CORE_ONLY_MANIFEST,
    BehavioralChallengePackModule,
    BehavioralOracleModule,
    PolicyEngine,
    PolicyManifest,
    ReviewJournal,
    ScreeningOutcome,
    SourceFingerprintTriageModule,
    load_policy_engine,
)


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, contents in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(contents))
    return buffer.getvalue()


def _provider_selector_harness(expected_provider: str) -> bytes:
    server = textwrap.dedent(
        f"""
        import json
        import os
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        EXPECTED_PROVIDER = {expected_provider!r}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self.send_json(200, {{"status": "ok"}})
                else:
                    self.send_json(404, {{"error": "not found"}})

            def do_POST(self):
                if self.path != "/run":
                    self.send_json(404, {{"error": "not found"}})
                    return
                if os.environ.get("DITTOBENCH_PROVIDER") != EXPECTED_PROVIDER:
                    self.send_json(503, {{"error": "provider selector unsupported"}})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                base_key = (
                    "DITTOBENCH_INFERENCE_BASE_URL"
                    if EXPECTED_PROVIDER == "platform"
                    else "CHUTES_BASE_URL"
                )
                upstream = urllib.request.Request(
                    os.environ[base_key].rstrip("/") + "/chat/completions",
                    data=json.dumps({{
                        "model": os.environ["DITTOBENCH_MODEL"],
                        "messages": [
                            {{"role": "system", "content": request["system_prompt"]}},
                            {{"role": "user", "content": request["user_input"]}},
                        ],
                    }}).encode(),
                    headers={{"Content-Type": "application/json"}},
                )
                response = json.loads(
                    urllib.request.urlopen(upstream, timeout=10).read()
                )
                self.send_json(200, {{
                    "final_text": response["choices"][0]["message"]["content"],
                    "tool_calls": [],
                    "prompt_tokens": 1,
                    "output_tokens": 1,
                    "latency_ms": 1,
                }})

        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
        """
    ).encode()
    dockerfile = (
        b"FROM python:3.12-alpine@sha256:"
        b"6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df\n"
        b"WORKDIR /app\n"
        b"COPY server.py /app/server.py\n"
        b"USER 65532:65532\n"
        b'ENTRYPOINT ["python", "/app/server.py"]\n'
    )
    return _tar_gz({"Dockerfile": dockerfile, "server.py": server})


async def _screen_provider_selector_harness(
    make_config: Any, expected_provider: str
) -> tuple[ScreeningOutcome, int]:
    tarball = _provider_selector_harness(expected_provider)

    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/provider.tar.gz")
        return httpx.Response(200, content=tarball)

    oracle = BehavioralOracleModule(
        module_id="v8-behavioral-oracle", timeout_seconds=30.0
    )
    manifest = PolicyManifest(
        rotation_id="integration-provider-parity",
        module_specs=({"kind": "behavioral_oracle"},),
    )
    config: ScreenerConfig = make_config(
        build_timeout_seconds=600.0,
        run_timeout_seconds=60.0,
        max_tarball_bytes=20 * 1024 * 1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=PolicyEngine(manifest, (oracle,)),
        journal=ReviewJournal(None),
    )
    restart_count = 0
    real_restart = gate._restart_harness_for_compatibility

    async def observed_restart(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal restart_count
        restart_count += 1
        return await real_restart(*args, **kwargs)

    gate._restart_harness_for_compatibility = observed_restart  # type: ignore[method-assign]
    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            bench_version=12,
            miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/provider.tar.gz",
        )
    return result.outcome, restart_count


@pytest.mark.integration
async def test_current_starter_kit_builds_and_health_checks_without_run(
    make_config: Any, tmp_path: Path
) -> None:
    archive_raw = os.environ.get("DITTO_STARTER_KIT_ARCHIVE")
    starter_dir_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    if archive_raw:
        source_archive = Path(archive_raw).resolve()
        tarball = source_archive.read_bytes()
    else:
        if not starter_dir_raw:
            pytest.skip("set DITTO_STARTER_KIT_DIR to a current canonical checkout")
        starter_dir = Path(starter_dir_raw).resolve()
        archive = tmp_path / "dittobench-starter-kit.tar.gz"
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(starter_dir), "archive", "--format=tar.gz", "HEAD"],
                check=True,
                stdout=output,
            )
        source_archive = archive
        tarball = archive.read_bytes()

    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/starter-kit.tar.gz")
        return httpx.Response(200, content=tarball)

    config: ScreenerConfig = make_config(
        build_timeout_seconds=1200.0,
        run_timeout_seconds=120.0,
        max_tarball_bytes=20 * 1024 * 1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=PolicyEngine(CORE_ONLY_MANIFEST),
        journal=ReviewJournal(None),
    )
    challenge_calls = 0
    real_challenge = gate._run_private_challenge

    async def observed_challenge(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal challenge_calls
        challenge_calls += 1
        return await real_challenge(*args, **kwargs)

    gate._run_private_challenge = observed_challenge  # type: ignore[method-assign]
    published: BuiltImageArtifact | None = None

    async def verify_export(image: BuiltImageArtifact) -> None:
        nonlocal published
        archive = Path(image.path)
        assert archive.stat().st_size == image.size_bytes
        with archive.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == image.sha256
        loaded = subprocess.run(
            ["docker", "image", "load", "--input", image.path],
            check=True,
            capture_output=True,
            text=True,
        )
        # The portable archive is intentionally untagged. A classic daemon
        # reports the signed config ID; a containerd-backed diagnostic daemon
        # may report its derived manifest ID. The validator fleet pins the
        # classic store, while the scorer verifies either identity from these
        # exact archive bytes before it invokes Docker.
        match = re.search(r"Loaded image ID: (sha256:[0-9a-f]{64})", loaded.stdout)
        assert match is not None, loaded.stdout
        loaded_id = match.group(1)
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", loaded_id],
            check=True,
            capture_output=True,
            text=True,
        )
        assert inspected.stdout.strip() == loaded_id
        dittobench_dir = os.environ.get("DITTOBENCH_API_DIR")
        if dittobench_dir:
            env = {
                **os.environ,
                "DITTOBENCH_SCREENED_IMAGE_ARCHIVE": image.path,
                "DITTOBENCH_SCREENED_SOURCE_ARCHIVE": str(source_archive),
                "DITTOBENCH_SCREENED_IMAGE_REF": image.image_ref,
                "DITTOBENCH_SCREENED_IMAGE_ID": image.image_id,
            }
            subprocess.run(
                [
                    "go",
                    "test",
                    "./internal/sandbox",
                    "-run",
                    "^TestScreenerArchiveLoadsWithoutRebuild$",
                    "-count=1",
                    "-v",
                ],
                cwd=Path(dittobench_dir).resolve(),
                env=env,
                check=True,
            )
        published = image

    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            bench_version=12,
            miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/starter-kit.tar.gz",
            publish_image=verify_export,
        )

    assert result.passed, result.detail
    assert challenge_calls == 0
    assert published is not None
    assert not Path(published.path).exists()


@pytest.mark.integration
async def test_current_starter_kit_clears_model_binding_audit(
    make_config: Any, tmp_path: Path
) -> None:
    starter_dir_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    if not starter_dir_raw:
        pytest.skip("set DITTO_STARTER_KIT_DIR to a current canonical checkout")
    starter_dir = Path(starter_dir_raw).resolve()
    archive = tmp_path / "dittobench-starter-kit-audit.tar.gz"
    with archive.open("wb") as output:
        subprocess.run(
            ["git", "-C", str(starter_dir), "archive", "--format=tar.gz", "HEAD"],
            check=True,
            stdout=output,
        )
    tarball = archive.read_bytes()
    pack = tmp_path / "private-control-pack.json"
    pack.write_text(
        json.dumps(
            {
                "challenges": [
                    {
                        "id": "rotating-private-control",
                        "request": {
                            "case_id": "private-control",
                            "system_prompt": "Answer the user concisely.",
                            "user_input": "Return a short acknowledgement.",
                            "tools": [],
                        },
                        "timeout_seconds": 60,
                        "required_response_keys": ["final_text", "tool_calls"],
                        "require_model_call": True,
                        "require_gateway_token": True,
                    }
                ]
            }
        )
    )
    selector = SourceFingerprintTriageModule(
        module_id="starter-control-selector",
        suspicious_path_suffixes=("src/baseline.rs",),
    )
    challenge = BehavioralChallengePackModule(
        module_id="model-binding-control", pack_path=pack
    )
    manifest = PolicyManifest(
        rotation_id="integration-control",
        module_specs=(
            {"kind": "source_fingerprint"},
            {"kind": "behavioral_challenge_pack"},
        ),
    )

    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/starter-kit.tar.gz")
        return httpx.Response(200, content=tarball)

    config: ScreenerConfig = make_config(
        build_timeout_seconds=1200.0,
        run_timeout_seconds=120.0,
        max_tarball_bytes=20 * 1024 * 1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=PolicyEngine(manifest, (selector, challenge)),
        journal=ReviewJournal(None),
    )
    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            bench_version=12,
            miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/starter-kit.tar.gz",
        )

    assert result.passed, result.detail
    assert any(item.code == "challenge-observed" for item in result.evidence)


@pytest.mark.integration
async def test_current_starter_kit_passes_behavioral_oracle(
    make_config: Any, tmp_path: Path
) -> None:
    """The v8 always-on oracle must pass against a REAL starter-kit harness.

    This is the request-contract seam the unit suite cannot cover: the oracle
    payload must deserialize as the starter kit's axum ``RunRequest`` (its
    required fields have no serde default, so a malformed payload is a 422 →
    ``challenge-http-failure`` → INCONCLUSIVE for every honest submission,
    which is exactly how policy v8 shipped broken).
    """
    archive_raw = os.environ.get("DITTO_STARTER_KIT_ARCHIVE")
    starter_dir_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    if archive_raw:
        tarball = Path(archive_raw).resolve().read_bytes()
    else:
        if not starter_dir_raw:
            pytest.skip("set DITTO_STARTER_KIT_ARCHIVE or DITTO_STARTER_KIT_DIR")
        starter_dir = Path(starter_dir_raw).resolve()
        archive = tmp_path / "dittobench-starter-kit-oracle.tar.gz"
        with archive.open("wb") as output:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(starter_dir),
                    "archive",
                    "--format=tar.gz",
                    "HEAD",
                ],
                check=True,
                stdout=output,
            )
        tarball = archive.read_bytes()
    # Generous timeout: this asserts the request CONTRACT, not prod timing
    # (the module default of 20s assumes prod-class hardware).
    oracle = BehavioralOracleModule(
        module_id="v8-behavioral-oracle", timeout_seconds=60.0
    )
    manifest = PolicyManifest(
        rotation_id="integration-oracle",
        module_specs=({"kind": "behavioral_oracle"},),
    )

    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/starter-kit.tar.gz")
        return httpx.Response(200, content=tarball)

    config: ScreenerConfig = make_config(
        build_timeout_seconds=1200.0,
        run_timeout_seconds=120.0,
        max_tarball_bytes=20 * 1024 * 1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=PolicyEngine(manifest, (oracle,)),
        journal=ReviewJournal(None),
    )
    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            bench_version=12,
            miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/starter-kit.tar.gz",
        )

    assert result.outcome != ScreeningOutcome.INCONCLUSIVE, (
        "oracle went inconclusive against an honest starter kit — the "
        f"RunRequest contract is likely broken again: {result.evidence}"
    )
    assert result.passed, result.detail


@pytest.mark.integration
async def test_platform_only_harness_clears_without_compatibility_restart(
    make_config: Any,
) -> None:
    """Screening starts with the same provider selector as current scoring."""
    outcome, restart_count = await _screen_provider_selector_harness(
        make_config, "platform"
    )

    assert outcome == ScreeningOutcome.PASS
    assert restart_count == 0


@pytest.mark.integration
async def test_chutes_only_harness_gets_one_compatibility_restart(
    make_config: Any,
) -> None:
    """A zero-call primary probe receives the scorer's one legacy restart."""
    outcome, restart_count = await _screen_provider_selector_harness(
        make_config, "chutes"
    )

    assert outcome == ScreeningOutcome.PASS
    assert restart_count == 1


@pytest.mark.integration
async def test_hardcoded_openrouter_https_reaches_isolated_gateway(
    make_config: Any, tmp_path: Path
) -> None:
    """A valid direct-OpenRouter harness must see the same shim as scoring."""
    server = textwrap.dedent(
        """
        import http.client
        import json
        import ssl
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self.send_json(200, {"status": "ok"})
                else:
                    self.send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/run":
                    self.send_json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                body = json.dumps({
                    "model": "openai/gpt-oss-20b",
                    "messages": [
                        {"role": "system", "content": request["system_prompt"]},
                        {"role": "user", "content": request["user_input"]},
                    ],
                })
                connection = http.client.HTTPSConnection(
                    "openrouter.ai",
                    443,
                    context=ssl.create_default_context(),
                    timeout=10,
                )
                connection.request(
                    "POST",
                    "/api/v1/chat/completions",
                    body=body,
                    headers={
                        "Authorization": "Bearer local",
                        "Content-Type": "application/json",
                    },
                )
                response = json.loads(connection.getresponse().read())
                connection.close()
                self.send_json(200, {
                    "final_text": response["choices"][0]["message"]["content"],
                    "tool_calls": [],
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 0,
                })

        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
        """
    ).encode()
    dockerfile = (
        b"FROM python:3.12-alpine@sha256:"
        b"6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df\n"
        b"WORKDIR /app\n"
        b"COPY server.py /app/server.py\n"
        b"USER 65532:65532\n"
        b'ENTRYPOINT ["python", "/app/server.py"]\n'
    )
    tarball = _tar_gz({"Dockerfile": dockerfile, "server.py": server})
    pack = tmp_path / "hardcoded-openrouter-pack.json"
    pack.write_text(
        json.dumps(
            {
                "challenges": [
                    {
                        "id": "hardcoded-openrouter-control",
                        "request": {
                            "case_id": "hardcoded-openrouter-control",
                            "system_prompt": "Answer concisely.",
                            "user_input": "Return a short acknowledgement.",
                            "tools": [],
                        },
                        "timeout_seconds": 30,
                        "required_response_keys": ["final_text", "tool_calls"],
                        "require_model_call": True,
                        "require_gateway_token": True,
                    }
                ]
            }
        )
    )
    selector = SourceFingerprintTriageModule(
        module_id="hardcoded-openrouter-selector",
        suspicious_path_suffixes=("server.py",),
    )
    challenge = BehavioralChallengePackModule(
        module_id="hardcoded-openrouter-control", pack_path=pack
    )
    manifest = PolicyManifest(
        rotation_id="integration-hardcoded-openrouter",
        module_specs=(
            {"kind": "source_fingerprint"},
            {"kind": "behavioral_challenge_pack"},
        ),
    )

    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/hardcoded.tar.gz")
        return httpx.Response(200, content=tarball)

    config: ScreenerConfig = make_config(
        build_timeout_seconds=600.0,
        run_timeout_seconds=60.0,
        max_tarball_bytes=20 * 1024 * 1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=PolicyEngine(manifest, (selector, challenge)),
        journal=ReviewJournal(None),
    )
    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            bench_version=12,
            miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/hardcoded.tar.gz",
        )

    assert result.passed, result.detail
    assert any(item.code == "challenge-observed" for item in result.evidence)


@pytest.mark.integration
async def test_current_starter_kit_passes_real_default_v7_luna_review(
    make_config: Any, tmp_path: Path
) -> None:
    starter_dir_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    key_file = os.environ.get("SCREENER_SOURCE_REVIEW_API_KEY_FILE")
    if not starter_dir_raw or not key_file:
        pytest.skip("set starter-kit directory and protected source-review key")
    archive = tmp_path / "dittobench-starter-kit-v7.tar.gz"
    with archive.open("wb") as output:
        subprocess.run(
            [
                "git",
                "-C",
                str(Path(starter_dir_raw).resolve()),
                "archive",
                "--format=tar.gz",
                "HEAD",
            ],
            check=True,
            stdout=output,
        )
    tarball = archive.read_bytes()

    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/starter-kit.tar.gz")
        return httpx.Response(200, content=tarball)

    config: ScreenerConfig = make_config(
        build_timeout_seconds=1200.0,
        run_timeout_seconds=120.0,
        max_tarball_bytes=20 * 1024 * 1024,
        source_review_api_key_file=key_file,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=load_policy_engine(None),
        journal=ReviewJournal(None),
    )
    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            bench_version=12,
            miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/starter-kit.tar.gz",
        )

    assert result.passed, result.detail
