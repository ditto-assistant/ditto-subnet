from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]

# The revision the fake checkout resolves to, and the (older) revision the
# fake running process reports. Keeping them distinct is the point of most of
# the tests below: "checked out" and "in service" are different facts.
TARGET_SHA = "1111111111111111111111111111111111111111"
RUNNING_SHA = "0000000000000000000000000000000000000000"

MIGRATION = """\
revision: str = "{revision}"
down_revision: str | None = {down!r}
"""


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -eu\n{source}")
    path.chmod(0o755)


def _write_migrations(repo: Path, *, diverged: bool = False) -> None:
    """Lay down an alembic history for the deploy's single-head preflight.

    ``diverged`` reproduces the 2026-07-25 shape: two revisions that each
    extend the same parent and were merged independently, which is what makes
    ``alembic upgrade head`` refuse to run.
    """
    versions = repo / "alembic" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "2026_07_01_root.py").write_text(
        MIGRATION.format(revision="root", down=None)
    )
    (versions / "2026_07_02_first.py").write_text(
        MIGRATION.format(revision="e7b4c02a5d18", down="root")
    )
    if diverged:
        (versions / "2026_07_02_second.py").write_text(
            MIGRATION.format(revision="e5b8c31d47af", down="root")
        )


@contextmanager
def _health_server(status: int = 200, commit: str | None = TARGET_SHA) -> Iterator[int]:
    """Serve ``status`` on any path so update.sh's post-deploy probe can pass.

    The probe is deliberately the one thing update.sh will not fake: it requires
    a real HTTP answer on the API port. Tests therefore need a real listener.
    ``commit`` is what the *running process* claims to be, which update.sh
    compares against the revision it checked out.
    """
    body = (
        '{"status":"ok"}'
        if commit is None
        else f'{{"status":"ok","commit":"{commit}"}}'
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _jlist(
    repo: Path,
    *,
    api_status: str = "online",
    script: str | None = None,
    relay_status: str = "online",
    relay_status_2: str = "online",
) -> str:
    """A ``pm2 jlist`` payload whose launch identity matches ecosystem.config.js.

    The relay entries deliberately remain in PM2's saved state. update.sh must
    ignore them because the separate release job rolls them one at a time.
    """
    exec_path = script or str(repo / ".venv" / "bin" / "python")
    common = {
        "pm_exec_path": exec_path,
        "exec_interpreter": "none",
        "exec_mode": "fork_mode",
        "pm_cwd": str(repo),
        "restart_time": 0,
    }
    return json.dumps(
        [
            {
                "name": "ditto-api",
                "pid": 4242,
                "pm2_env": {**common, "status": api_status},
            },
            {
                "name": "ditto-api-relay-1",
                "pid": 4243,
                "pm2_env": {**common, "status": relay_status},
            },
            {
                "name": "ditto-api-relay-2",
                "pid": 4244,
                "pm2_env": {**common, "status": relay_status_2},
            },
            {
                "name": "ditto-screened-image-cleanup",
                "pid": 0,
                "pm2_env": {**common, "status": "stopped"},
            },
        ]
    )


def _run_update(
    tmp_path: Path,
    *,
    gcloud_source: str,
    initial_env: str = "BASE_SETTING=kept\n",
    initial_deploy_env: str | None = None,
    deploy_env_vars: dict[str, str] | None = None,
    jlist: str | None = None,
    health_status: int = 200,
    health_commit: str | None = TARGET_SHA,
    health_timeout: str = "15",
    uv_source: str = ":\n",
    npm_source: str | None = None,
    diverged_migrations: bool = False,
    last_deploy_record: str | None = None,
    control: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, str, int]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(ROOT / "scripts" / "update.sh", scripts / "update.sh")
    # The deploy plan and the app definition it diffs against are part of the
    # start/reload path, so the fake repo needs both.
    shutil.copy2(
        ROOT / "scripts" / "pm2_deploy_plan.js", scripts / "pm2_deploy_plan.js"
    )
    shutil.copy2(
        ROOT / "scripts" / "ecosystem.config.js", scripts / "ecosystem.config.js"
    )
    # The single-head preflight runs this with the system python3, before
    # `uv sync`, so it needs the real script and a real migration tree.
    shutil.copy2(
        ROOT / "scripts" / "check_migration_order.py",
        scripts / "check_migration_order.py",
    )
    _write_migrations(repo, diverged=diverged_migrations)
    (repo / "logs").mkdir()
    if last_deploy_record is not None:
        (repo / "logs" / "last-deploy.json").write_text(last_deploy_record)
    (repo / ".env").write_text(initial_env)
    if initial_deploy_env is not None:
        (repo / ".env.deploy").write_text(initial_deploy_env)

    (repo / "jlist.json").write_text(jlist if jlist is not None else _jlist(repo))
    # Behaviour switches for the recording sudo/systemctl/docker fakes below.
    for name, body in (control or {}).items():
        (repo / name).write_text(body)

    # `git rev-parse HEAD` reads a file the fake `git reset --hard <sha>`
    # rewrites, so a test can observe update.sh rolling the checkout back.
    (repo / "git-head").write_text(f"{TARGET_SHA}\n")
    _write_executable(
        fake_bin / "git",
        f'printf "%s\\n" "git $*" >> "{repo}/git-actions.log"\n'
        'case "${1:-}" in\n'
        "  rev-parse)\n"
        '    if [ "${2:-}" = "--abbrev-ref" ]; then printf "main\\n";\n'
        f'    else cat "{repo}/git-head"; fi\n'
        "    ;;\n"
        "  reset)\n"
        '    case "${3:-}" in\n'
        "      origin/*|'') : ;;\n"
        f'      *) printf "%s\\n" "$3" > "{repo}/git-head" ;;\n'
        "    esac\n"
        "    ;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "uv", uv_source)
    # The dashboard build stage is exercised only when a test opts in with
    # npm_source: the fake repo then grows the dashboard/package.json the
    # stage keys on. Without it the stage skips, like a checkout without the
    # dashboard, so every other test stays focused on what it asserts.
    if npm_source is not None:
        (repo / "dashboard").mkdir()
        (repo / "dashboard" / "package.json").write_text("{}\n")
        _write_executable(fake_bin / "npm", npm_source)
    _write_executable(
        fake_bin / "docker",
        f'printf "%s\\n" "docker $*" >> "{repo}/docker-actions.log"\n',
    )
    # sudo records its argument vector and, for the release installer, the
    # request it would read on stdin. It never runs anything.
    _write_executable(
        fake_bin / "sudo",
        f'printf "%s\\n" "sudo $*" >> "{repo}/sudo-actions.log"\n'
        f'printf "%s\\n" "sudo $*" >> "{repo}/timeline.log"\n'
        'case "${3:-}" in\n'
        f'  install|activate) cat > "{repo}/sudo-stdin-$3.log" ;;\n'
        "esac\n"
        'case "${3:-}:${2:-}" in\n'
        '  logs:*|*:/usr/bin/journalctl) echo "synthetic journal line" ;;\n'
        "esac\n"
        f'if [ -e "{repo}/sudo-${{3:-}}-exit" ]; then\n'
        f'  exit "$(cat "{repo}/sudo-${{3:-}}-exit")"\n'
        "fi\n"
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        f'printf "%s\\n" "systemctl $*" >> "{repo}/systemctl-actions.log"\n'
        'case "${1:-}" in\n'
        f'  show) cat "{repo}/systemctl-show" 2>/dev/null ||'
        " printf 'ActiveState=active\\nMainPID=5151\\nNRestarts=0\\n' ;;\n"
        f'  is-enabled) cat "{repo}/systemctl-is-enabled" 2>/dev/null ||'
        " echo disabled ;;\n"
        f'  is-active) cat "{repo}/systemctl-is-active" 2>/dev/null ||'
        " echo inactive ;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "pm2",
        f'if [ "${{1:-}}" = "jlist" ]; then cat "{repo}/jlist.json"; fi\n'
        f'printf "%s\\n" "pm2 $*" >> "{repo}/pm2-actions.log"\n'
        f'printf "%s\\n" "pm2 $*" >> "{repo}/timeline.log"\n',
    )
    # The deploy-side Docker-access check: records its arguments and exits with
    # control file docker-probe-exit (default 0).
    (tmp_path / "docker-probe.py").write_text(
        "import pathlib, sys\n"
        f"repo = pathlib.Path({str(repo)!r})\n"
        "with open(repo / 'docker-probe.log', 'a') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "code = repo / 'docker-probe-exit'\n"
        "if code.exists():\n"
        "    print('synthetic: deploy still holds the docker group')\n"
        "    sys.exit(int(code.read_text()))\n"
    )
    _write_executable(fake_bin / "gcloud", gcloud_source)
    _write_executable(fake_bin / "timeout", 'shift\nexec "$@"\n')

    # update.sh promotes selected DITTO_* variables from its own environment
    # into .env.deploy, so an ambient value silently overrides what a test
    # asked for. That made these tests pass only on a machine whose shell had
    # never sourced .env -- true of CI until the integration suite started
    # needing DITTO_UPLOAD_PAYMENT_ADDRESS, and false for anyone running
    # `make test-integration` locally. Start from an environment with every
    # variable the script consumes stripped, so the only values in play are
    # the ones the caller passes in.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DITTO_CODING_HOSTED_CONTROL_ENABLED",
            "DITTO_CODING_HOSTED_SIGNER_HOTKEY",
            "DITTO_CODING_HOSTED_SIGNER_SEED_FILE",
            "DITTO_COMPOSE_SERVICES",
            "DITTO_DASHBOARD_WANDB_URL",
            "DITTO_DEPLOY_BRANCH",
            "DITTO_DEPLOY_COMMIT",
            "DITTO_HEALTH_TIMEOUT",
            "DITTO_PLATFORM_API_SUPERVISOR",
            "DITTO_PLATFORM_API_UNIT_FILE",
            "DITTO_DEPLOY_DOCKER_PROBE",
            "DITTO_DEPLOY_PROC_ROOT",
            "DITTO_PLATFORM_PYLON_UNIT",
            "DITTO_TAOSTATS_API_KEY",
            "DITTO_TAOSTATS_SECRET_ID",
            "DITTO_TAOSTATS_SECRET_PROJECT",
            "DITTO_TAOSTATS_VALIDATOR_NAMES_URL",
            "DITTO_UPLOAD_PAYMENT_ADDRESS",
            "SUBTENSOR_ARCHIVE_RPC_API_KEY",
            "SUBTENSOR_ARCHIVE_RPC_AUTH_MODE",
            "SUBTENSOR_ARCHIVE_RPC_SECRET_ID",
            "SUBTENSOR_ARCHIVE_RPC_SECRET_PROJECT",
            "SUBTENSOR_ARCHIVE_RPC_URL",
        }
    }
    env.update(deploy_env_vars or {})
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["DITTO_HEALTH_TIMEOUT"] = health_timeout
    # A unit file left by an earlier dedicated-identity deploy; absent unless a
    # test creates it.
    env["DITTO_PLATFORM_API_UNIT_FILE"] = str(repo / "ditto-platform-api.service")
    env["DITTO_DEPLOY_DOCKER_PROBE"] = str(tmp_path / "docker-probe.py")
    env["DITTO_DEPLOY_PROC_ROOT"] = str(tmp_path / "proc")

    with ExitStack() as stack:
        port = stack.enter_context(_health_server(health_status, health_commit))
        env["API_PORT"] = str(port)
        result = subprocess.run(
            [str(scripts / "update.sh")],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    # A preflight failure exits before .env.deploy is written at all, which is
    # itself the point: nothing on the host had been touched yet.
    deploy_env = repo / ".env.deploy"
    if not deploy_env.exists():
        return result, (repo / ".env").read_text(), "", 0
    return (
        result,
        (repo / ".env").read_text(),
        deploy_env.read_text(),
        deploy_env.stat().st_mode & 0o777,
    )


def _head(tmp_path: Path) -> str:
    return (tmp_path / "repo" / "git-head").read_text().strip()


def _git_actions(tmp_path: Path) -> str:
    return (tmp_path / "repo" / "git-actions.log").read_text()


def _deploy_record(tmp_path: Path) -> dict[str, str]:
    return json.loads((tmp_path / "repo" / "logs" / "last-deploy.json").read_text())


def test_deploy_reinstalls_embedded_protocol_before_migrations() -> None:
    updater = (ROOT / "scripts" / "update.sh").read_text()

    command = "uv sync --reinstall-package ditto-screening-protocol"
    assert updater.count(command) == 2
    assert updater.index(command) < updater.index("uv run alembic upgrade head")


def test_update_loads_taostats_key_without_logging_value(tmp_path: Path) -> None:
    api_key = "tao-test:example"
    result, base_env, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source=f'printf "%s\\n" "{api_key}"\n',
    )

    assert result.returncode == 0, result.stderr
    assert base_env == "BASE_SETTING=kept\n"
    assert deploy_mode == 0o600
    assert f"DITTO_TAOSTATS_API_KEY={api_key}" in deploy_env
    assert (
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL="
        "https://api.taostats.io/api/dtao/validator/available/v1?netuid=118"
    ) in deploy_env
    assert api_key not in result.stdout
    assert api_key not in result.stderr


def test_update_keeps_existing_enrichment_when_secret_is_unavailable(
    tmp_path: Path,
) -> None:
    initial_deploy_env = (
        "DITTO_TAOSTATS_API_KEY=existing-key\n"
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL=https://example.invalid/names\n"
    )
    result, base_env, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_deploy_env=initial_deploy_env,
    )

    assert result.returncode == 0, result.stderr
    assert base_env == "BASE_SETTING=kept\n"
    assert deploy_env == initial_deploy_env
    assert deploy_mode == 0o600
    assert "Taostats key unavailable" in result.stderr


def test_update_loads_optional_archive_key_without_logging_value(
    tmp_path: Path,
) -> None:
    api_key = "archive-test:key-must-stay-secret"
    result, base_env, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source=(
            'case "$*" in\n'
            "  *platform-subtensor-archive-rpc-api-key*) "
            f'printf "%s\\n" "{api_key}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        ),
        deploy_env_vars={
            "SUBTENSOR_ARCHIVE_RPC_URL": "wss://paid.example/archive",
            "SUBTENSOR_ARCHIVE_RPC_AUTH_MODE": "query",
        },
    )

    assert result.returncode == 0, result.stderr
    assert base_env == "BASE_SETTING=kept\n"
    assert deploy_mode == 0o600
    assert f"SUBTENSOR_ARCHIVE_RPC_API_KEY={api_key}" in deploy_env
    assert "SUBTENSOR_ARCHIVE_RPC_URL=wss://paid.example/archive" in deploy_env
    assert "SUBTENSOR_ARCHIVE_RPC_AUTH_MODE=query" in deploy_env
    assert api_key not in result.stdout
    assert api_key not in result.stderr


def test_update_missing_archive_secret_keeps_free_fallback_unconfigured(
    tmp_path: Path,
) -> None:
    result, _base_env, deploy_env, _deploy_mode = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
    )

    assert result.returncode == 0, result.stderr
    assert "SUBTENSOR_ARCHIVE_RPC_API_KEY=" not in deploy_env
    assert "SUBTENSOR_ARCHIVE_RPC_URL=" not in deploy_env
    assert "free archive fallback remains enabled" in result.stderr


def test_update_migrates_legacy_deploy_values_before_ansible_rewrites_base(
    tmp_path: Path,
) -> None:
    legacy_key = "legacy-key-must-not-be-logged"
    initial_env = (
        "BASE_SETTING=kept\n"
        f"DITTO_TAOSTATS_API_KEY={legacy_key}\n"
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL=https://example.invalid/names\n"
    )
    result, base_env, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=initial_env,
    )

    assert result.returncode == 0, result.stderr
    assert base_env == initial_env
    assert f"DITTO_TAOSTATS_API_KEY={legacy_key}" in deploy_env
    assert (
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL=https://example.invalid/names" in deploy_env
    )
    assert deploy_mode == 0o600
    assert legacy_key not in result.stdout
    assert legacy_key not in result.stderr


def test_update_keeps_ansible_env_immutable_and_deploy_values_override(
    tmp_path: Path,
) -> None:
    payment = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"
    base_env = (
        "BASE_SETTING=kept\nDITTO_UPLOAD_PAYMENT_ADDRESS=base-must-not-be-edited\n"
    )
    result, observed_base, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=base_env,
        deploy_env_vars={"DITTO_UPLOAD_PAYMENT_ADDRESS": payment},
    )

    assert result.returncode == 0, result.stderr
    assert observed_base == base_env
    assert f"DITTO_UPLOAD_PAYMENT_ADDRESS={payment}" in deploy_env
    assert deploy_mode == 0o600


def test_update_repairs_no_final_newline_before_adding_another_key(
    tmp_path: Path,
) -> None:
    payment = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"
    wandb_url = "https://wandb.ai/ditto/dev"
    result, _, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_deploy_env=f"DITTO_UPLOAD_PAYMENT_ADDRESS={payment}",
        deploy_env_vars={"DITTO_DASHBOARD_WANDB_URL": wandb_url},
    )

    assert result.returncode == 0, result.stderr
    assert deploy_env.splitlines() == [
        f"DITTO_UPLOAD_PAYMENT_ADDRESS={payment}",
        f"DITTO_DASHBOARD_WANDB_URL={wandb_url}",
    ]
    assert deploy_mode == 0o600


def test_update_discards_truncated_fragment_and_retries_canonically(
    tmp_path: Path,
) -> None:
    payment = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"
    result, _, deploy_env, deploy_mode = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_deploy_env="DITTO_UPLOAD_PAYMENT_ADD",
        deploy_env_vars={"DITTO_UPLOAD_PAYMENT_ADDRESS": payment},
    )

    assert result.returncode == 0, result.stderr
    assert deploy_env == f"DITTO_UPLOAD_PAYMENT_ADDRESS={payment}\n"
    assert deploy_mode == 0o600


def _actions(tmp_path: Path) -> str:
    return (tmp_path / "repo" / "pm2-actions.log").read_text()


def test_update_reloads_in_place_when_launch_identity_matches(tmp_path: Path) -> None:
    """The ordinary code-only deploy keeps using graceful reload."""
    result, _, _, _ = _run_update(tmp_path, gcloud_source="exit 1\n")

    assert result.returncode == 0, result.stderr
    actions = _actions(tmp_path)
    assert "pm2 reload scripts/ecosystem.config.js" in actions
    assert "pm2 delete" not in actions
    assert "ditto-api: reload" in result.stdout
    assert "--only ditto-api-relay-" not in actions
    assert "managed by the rolling relay release" in result.stdout


def test_update_recreates_the_app_when_the_script_path_drifted(tmp_path: Path) -> None:
    """The outage case: pm2 reload silently keeps the old `script`.

    pm2 is running `uv` while ecosystem.config.js now resolves to the venv
    interpreter, so the deploy must delete and start rather than reload.
    """
    repo = tmp_path / "repo"
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        jlist=_jlist(repo, script="/usr/local/bin/uv"),
    )

    assert result.returncode == 0, result.stderr
    actions = _actions(tmp_path)
    assert "pm2 delete ditto-api" in actions
    assert "pm2 start scripts/ecosystem.config.js" in actions
    assert "pm2 reload" not in actions
    assert "recreate (script:" in result.stdout


def test_update_fails_when_the_api_never_comes_up(tmp_path: Path) -> None:
    """A deploy that leaves the API dead must exit non-zero, not report success."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        jlist=_jlist(tmp_path / "repo", api_status="waiting restart"),
        health_timeout="4",
    )

    assert result.returncode != 0
    assert "deploy failed" in result.stderr
    assert "ditto-api" in result.stderr


def test_update_fails_when_the_api_serves_a_degraded_health_response(
    tmp_path: Path,
) -> None:
    """Online but /health non-200 is still a failed deploy, reported distinctly."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        health_status=503,
        health_timeout="4",
    )

    assert result.returncode != 0
    assert "returned HTTP 503" in result.stderr


def test_update_accepts_the_stopped_one_shot_cleanup_job(tmp_path: Path) -> None:
    """`stopped` is the cron-driven cleanup job's correct terminal state."""
    result, _, _, _ = _run_update(tmp_path, gcloud_source="exit 1\n")

    assert result.returncode == 0, result.stderr
    assert "ditto-screened-image-cleanup: stopped (one-shot" in result.stdout


# ---------------------------------------------------------------------------
def test_update_does_not_touch_or_gate_on_the_separately_released_relay(
    tmp_path: Path,
) -> None:
    """A normal API deploy must leave both relay slots serving throughout."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        jlist=_jlist(tmp_path / "repo", relay_status="waiting restart"),
    )

    assert result.returncode == 0, result.stderr
    assert "--only ditto-api-relay-" not in _actions(tmp_path)
    assert "managed by the rolling relay release" in result.stdout


# ---------------------------------------------------------------------------
# Divergent migration heads, and what a failed deploy leaves behind.
#
# The 2026-07-25 near-outage: origin/main carried two alembic heads, `alembic
# upgrade head` refused to run, and the deploy stopped with the new revision
# checked out and the old process still serving. Every git-layer signal said
# the deploy had landed.


def test_update_refuses_to_deploy_divergent_migration_heads(tmp_path: Path) -> None:
    """Two heads stop the deploy in preflight, naming both and the remedy."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        diverged_migrations=True,
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert "2 head revisions are present" in result.stderr
    # The revisions and the exact fix, not alembic's bare "Multiple head
    # revisions are present" from the middle of the sequence.
    assert "e5b8c31d47af" in result.stderr
    assert "e7b4c02a5d18" in result.stderr
    assert "alembic merge" in result.stderr
    # Preflight runs before pm2 is planned or touched.
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()


def test_update_rolls_the_checkout_back_when_the_dashboard_build_fails(
    tmp_path: Path,
) -> None:
    """A broken dashboard build must abort before pm2 is touched.

    The build sits with the other pre-pm2 stages, so its failure follows the
    same rules as any preflight: the checkout goes back to the revision the
    running process reports, pm2 is never asked to do anything, and the old
    process — with the dist/ it was already serving — stays up.
    """
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        npm_source='echo "npm ERR! build exploded" >&2\nexit 1\n',
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert f"git reset --hard {RUNNING_SHA}" in _git_actions(tmp_path)
    assert _head(tmp_path) == RUNNING_SHA
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()

    record = _deploy_record(tmp_path)
    assert record["result"] == "failed"
    assert record["stage"] == "dashboard-build"


def test_update_builds_the_dashboard_before_touching_pm2(tmp_path: Path) -> None:
    """When the checkout has a dashboard, the deploy builds it (ci + build)."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        npm_source=f'printf "%s\\n" "npm $*" >> "{tmp_path}/repo/npm-actions.log"\n',
    )

    assert result.returncode == 0, result.stderr
    npm_actions = (tmp_path / "repo" / "npm-actions.log").read_text()
    assert "npm ci" in npm_actions
    assert "npm run build" in npm_actions


def test_update_rolls_the_checkout_back_when_a_migration_fails(
    tmp_path: Path,
) -> None:
    """A migration failure must not leave new code checked out and unserved.

    This is the exact shape of the incident: the checkout moved, the process
    did not, and nothing at the git layer said so. The checkout is put back to
    the revision the running process reports, so the host stops claiming a
    deploy that never took effect.
    """
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        # Succeed for `uv sync`, fail for `uv run alembic upgrade head`.
        uv_source='if [ "${1:-}" = "run" ]; then\n'
        '  echo "Multiple head revisions are present" >&2\n'
        "  exit 1\n"
        "fi\n",
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert f"git reset --hard {RUNNING_SHA}" in _git_actions(tmp_path)
    assert _head(tmp_path) == RUNNING_SHA
    # pm2 is never reached, so the old process keeps serving code that once
    # again matches the checkout.
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()

    record = _deploy_record(tmp_path)
    assert record["result"] == "failed"
    assert record["stage"] == "migrate"
    assert record["target_commit"] == TARGET_SHA
    assert record["rolled_back"] == "yes"


def test_update_leaves_the_checkout_in_place_once_pm2_has_restarted(
    tmp_path: Path,
) -> None:
    """After the restart, rewinding the checkout would be the opposite lie.

    The new build is what pm2 is now supervising. Going back is a deploy of
    the previous revision, not a `git reset` behind a running process.
    """
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        jlist=_jlist(tmp_path / "repo", api_status="waiting restart"),
        health_timeout="4",
    )

    assert result.returncode != 0
    assert "git reset --hard" not in _git_actions(tmp_path).replace(
        "git reset --hard origin/main", ""
    )
    assert _head(tmp_path) == TARGET_SHA
    assert "pm2 was already restarted" in result.stderr
    assert _deploy_record(tmp_path)["rolled_back"] == "no"


def test_update_does_not_roll_back_to_a_revision_from_a_failed_deploy(
    tmp_path: Path,
) -> None:
    """A failed run's target was never in service, so it is not a rollback target."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        diverged_migrations=True,
        health_commit=None,
        last_deploy_record=json.dumps(
            {"result": "failed", "target_commit": "deadbeefdeadbeef"}
        ),
    )

    assert result.returncode != 0
    assert "deadbeefdeadbeef" not in _git_actions(tmp_path)
    assert _head(tmp_path) == TARGET_SHA
    assert "Could not determine the revision in service" in result.stderr


def test_update_falls_back_to_the_last_successful_deploy_record(
    tmp_path: Path,
) -> None:
    """When the API cannot answer, the script's own record names the rollback."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        diverged_migrations=True,
        health_commit=None,
        last_deploy_record=json.dumps({"result": "ok", "target_commit": RUNNING_SHA}),
    )

    assert result.returncode != 0
    assert f"git reset --hard {RUNNING_SHA}" in _git_actions(tmp_path)
    assert _head(tmp_path) == RUNNING_SHA


# ---------------------------------------------------------------------------
# Checked out is not the same as in service.


def test_update_fails_when_the_api_serves_a_different_commit(tmp_path: Path) -> None:
    """200 from an old build is a failed deploy, not a passed one.

    pm2 online plus HTTP 200 was still not enough: the process can be serving
    code from before the checkout. The gate asks the process what it is
    running and compares it to what was checked out.
    """
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        health_commit=RUNNING_SHA,
        health_timeout="4",
    )

    assert result.returncode != 0
    assert f"is serving commit {RUNNING_SHA}" in result.stderr
    assert f"checked out {TARGET_SHA}" in result.stderr
    assert "never restarted into this build" in result.stderr


def test_update_warns_but_passes_when_the_api_reports_no_commit(
    tmp_path: Path,
) -> None:
    """A checkout without git history must not fail every deploy on that host."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        health_commit=None,
    )

    assert result.returncode == 0, result.stderr
    assert "does not report a commit" in result.stderr


def test_update_records_and_announces_the_deployed_commit(tmp_path: Path) -> None:
    """The workflow reads the last line; the next deploy reads the record."""
    result, _, _, _ = _run_update(tmp_path, gcloud_source="exit 1\n")

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith(f"deployed-commit={TARGET_SHA}")

    record = _deploy_record(tmp_path)
    assert record["result"] == "ok"
    assert record["stage"] == "done"
    assert record["target_commit"] == TARGET_SHA
    deployed_source = tmp_path / "repo" / "logs" / "deployed-source.sha"
    assert deployed_source.read_text() == f"{TARGET_SHA}\n"
    assert deployed_source.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Process supervision and the hosted-v2 control signer.
#
# Default: ditto-api under this user's pm2 and Pylon through its docker compose,
# with no sudo at all. DITTO_PLATFORM_API_SUPERVISOR=systemd runs ditto-api as
# the dedicated ditto-api user from a sealed release, reached only through the
# root installer's four exact sudo commands. The deploy user never checks,
# stats or opens the control-signer seed in either mode.

INSTALLER = "/usr/local/sbin/ditto-platform-api-release"
SYSTEMD_ENV = "BASE_SETTING=kept\nDITTO_PLATFORM_API_SUPERVISOR=systemd\n"


def _log(tmp_path: Path, name: str) -> list[str]:
    path = tmp_path / "repo" / f"{name}-actions.log"
    return path.read_text().splitlines() if path.exists() else []


def _recording_uv(tmp_path: Path) -> str:
    log = tmp_path / "repo" / "uv-actions.log"
    return f'printf "%s\\n" "$*" >> "{log}"\n'


def _uv_actions(tmp_path: Path) -> list[str]:
    path = tmp_path / "repo" / "uv-actions.log"
    return path.read_text().splitlines() if path.exists() else []


def test_default_supervision_uses_pm2_and_compose_without_sudo(
    tmp_path: Path,
) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env="BASE_SETTING=kept\nDITTO_PLATFORM_API_SUPERVISOR=pm2\n"
        "DITTO_PLATFORM_PYLON_UNIT=\n",
        uv_source=_recording_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert _log(tmp_path, "sudo") == []
    assert _log(tmp_path, "systemctl") == []
    assert _log(tmp_path, "docker") == [
        "docker compose up -d --wait postgres minio pylon"
    ]
    assert "pm2 reload scripts/ecosystem.config.js --only ditto-api" in _actions(
        tmp_path
    )
    assert not any("coding_hosted_signer" in action for action in _uv_actions(tmp_path))


@pytest.mark.parametrize("value", ["true", "1", "TRUE", ""])
def test_update_refuses_an_enabled_signer_while_ditto_api_would_run_as_deploy(
    tmp_path: Path, value: str
) -> None:
    """Only ditto-api may read the seed: pm2 would run it as this user."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=(
            "DITTO_CODING_HOSTED_CONTROL_ENABLED=false\n"
            f"DITTO_CODING_HOSTED_CONTROL_ENABLED={value}\n"
        ),
        uv_source=_recording_uv(tmp_path),
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert "ditto-api would run under pm2" in result.stderr
    assert "platform_api_service_identity_enabled" in result.stderr
    assert not any(action.startswith("run ") for action in _uv_actions(tmp_path))
    assert _log(tmp_path, "sudo") == []
    assert _log(tmp_path, "docker") == []
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()
    assert f"git reset --hard {RUNNING_SHA}" in _git_actions(tmp_path)
    record = _deploy_record(tmp_path)
    assert (record["result"], record["stage"], record["rolled_back"]) == (
        "failed",
        "signer-preflight",
        "yes",
    )


@pytest.mark.parametrize(
    "initial_env",
    [
        "BASE_SETTING=kept\n",
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=false\n",
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=true\n"
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=0\n",
    ],
)
def test_a_disabled_signer_deploys_under_pm2_as_before(
    tmp_path: Path, initial_env: str
) -> None:
    result, _, _, _ = _run_update(
        tmp_path, gcloud_source="exit 1\n", initial_env=initial_env
    )

    assert result.returncode == 0, result.stderr
    assert "hosted-v2 control signer" not in result.stdout
    assert _log(tmp_path, "sudo") == []


@pytest.mark.parametrize("signer", ["false", "true"])
def test_dedicated_identity_installs_migrates_then_activates_the_sealed_release(
    tmp_path: Path, signer: str
) -> None:
    payment = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source=(
            'case "$*" in\n'
            '  *platform-taostats-api-key*) printf "%s\\n" "tao-test:example" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        ),
        initial_env=(
            f"{SYSTEMD_ENV}DITTO_CODING_HOSTED_CONTROL_ENABLED={signer}\n"
            "DITTO_ADMIN_API_TOKEN=base-secret-never-forwarded\n"
        ),
        deploy_env_vars={"DITTO_UPLOAD_PAYMENT_ADDRESS": payment},
        uv_source=_recording_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert _log(tmp_path, "sudo") == [
        f"sudo -n {INSTALLER} install",
        f"sudo -n {INSTALLER} activate",
    ]
    repo = tmp_path / "repo"
    # Only the revision and the deploy-owned keys cross into root, on stdin.
    install_request = (repo / "sudo-stdin-install.log").read_text()
    assert install_request.splitlines() == [
        f"revision={TARGET_SHA}",
        f"DITTO_UPLOAD_PAYMENT_ADDRESS={payment}",
        "DITTO_TAOSTATS_API_KEY=tao-test:example",
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL="
        "https://api.taostats.io/api/dtao/validator/available/v1?netuid=118",
    ]
    assert "base-secret" not in install_request
    assert (repo / "sudo-stdin-activate.log").read_text() == f"revision={TARGET_SHA}\n"
    assert "tao-test:example" not in result.stdout + result.stderr

    # The install precedes Pylon and migrations; activation follows both.
    order = result.stdout
    assert (
        order.index("==> installing the sealed ditto-api release")
        < order.index("==> ensuring infra")
        < order.index("==> applying migrations")
        < order.index("==> activating ditto-api release")
    )
    # pm2 keeps only the cleanup job; its old ditto-api copy is retired.
    actions = _actions(tmp_path)
    assert "--only ditto-api," not in actions and "--only ditto-api " not in actions
    assert (
        "pm2 reload scripts/ecosystem.config.js --only ditto-screened-image-cleanup"
        in (actions)
    )
    assert "pm2 delete ditto-api" in actions
    assert "managed by ditto-platform-api.service" in result.stdout
    assert "ditto-api: online and serving 200" in result.stdout
    assert "systemctl show --property=ActiveState" in "\n".join(
        _log(tmp_path, "systemctl")
    )
    # The deploy user never runs the seed preflight itself.
    assert not any("coding_hosted_signer" in action for action in _uv_actions(tmp_path))
    assert result.stdout.rstrip().endswith(f"deployed-commit={TARGET_SHA}")


def test_dedicated_identity_does_not_retire_a_pm2_app_it_never_had(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    jlist = json.loads(_jlist(repo))
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=SYSTEMD_ENV,
        jlist=json.dumps([app for app in jlist if app["name"] != "ditto-api"]),
    )

    assert result.returncode == 0, result.stderr
    assert "pm2 delete ditto-api" not in _actions(tmp_path)


def test_a_failed_release_install_rolls_back_before_anything_serves(
    tmp_path: Path,
) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=f"{SYSTEMD_ENV}DITTO_CODING_HOSTED_CONTROL_ENABLED=true\n",
        uv_source=_recording_uv(tmp_path),
        control={"sudo-install-exit": "1"},
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert "sealed ditto-api release could not be" in result.stderr
    assert _log(tmp_path, "sudo") == [f"sudo -n {INSTALLER} install"]
    assert _log(tmp_path, "docker") == []
    assert not any(action.startswith("run alembic") for action in _uv_actions(tmp_path))
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()
    assert f"git reset --hard {RUNNING_SHA}" in _git_actions(tmp_path)
    record = _deploy_record(tmp_path)
    assert (record["stage"], record["rolled_back"]) == ("api-release", "yes")


def test_a_unit_that_fails_to_start_fails_the_deploy_with_its_journal(
    tmp_path: Path,
) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=SYSTEMD_ENV,
        control={"systemctl-show": "ActiveState=failed\nMainPID=0\nNRestarts=10\n"},
        health_timeout="4",
    )

    assert result.returncode != 0
    assert "ditto-api is in ditto-platform-api.service state 'errored'" in result.stderr
    assert "ditto-platform-api.service status/pid/restarts: errored\t0\t10" in (
        result.stderr
    )
    assert "synthetic journal line" in result.stderr
    assert _log(tmp_path, "sudo")[-1] == f"sudo -n {INSTALLER} logs"
    # The serving process was replaced, so the checkout stays put.
    assert _deploy_record(tmp_path)["rolled_back"] == "no"


def test_returning_to_pm2_stops_the_dedicated_unit_first(tmp_path: Path) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        control={
            "ditto-platform-api.service": "[Unit]\n",
            "systemctl-is-enabled": "enabled\n",
            "systemctl-is-active": "active\n",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _log(tmp_path, "sudo") == [f"sudo -n {INSTALLER} stop"]
    stop = result.stdout.index("==> stopping ditto-platform-api.service")
    assert stop < result.stdout.index("==> reloading:")
    assert _deploy_record(tmp_path)["result"] == "ok"


def test_a_leftover_disabled_unit_is_left_alone(tmp_path: Path) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        control={"ditto-platform-api.service": "[Unit]\n"},
    )

    assert result.returncode == 0, result.stderr
    assert _log(tmp_path, "sudo") == []


def test_the_pylon_unit_replaces_deploy_docker_access(tmp_path: Path) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env="BASE_SETTING=kept\nDITTO_PLATFORM_PYLON_UNIT=ditto-platform-pylon.service\n",
    )

    assert result.returncode == 0, result.stderr
    assert _log(tmp_path, "docker") == []
    assert _log(tmp_path, "sudo") == [
        "sudo -n /usr/bin/systemctl restart ditto-platform-pylon.service"
    ]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "DITTO_PLATFORM_PYLON_UNIT",
            "evil.service",
            "DITTO_PLATFORM_PYLON_UNIT must be",
        ),
        (
            "DITTO_PLATFORM_API_SUPERVISOR",
            "root",
            "DITTO_PLATFORM_API_SUPERVISOR must be",
        ),
    ],
)
def test_unknown_supervision_values_stop_the_deploy(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=f"{key}={value}\n",
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert _log(tmp_path, "sudo") == []
    assert _log(tmp_path, "docker") == []
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()


def _timeline(tmp_path: Path) -> list[str]:
    return (tmp_path / "repo" / "timeline.log").read_text().splitlines()


def test_an_enabled_signer_refuses_while_this_user_reaches_docker(
    tmp_path: Path,
) -> None:
    """A switch in .env proves nothing about the running pm2 daemon's groups."""
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=f"{SYSTEMD_ENV}DITTO_CODING_HOSTED_CONTROL_ENABLED=true\n",
        control={"docker-probe-exit": "1"},
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert "can still reach the Docker daemon" in result.stderr
    assert "synthetic: deploy still holds the docker group" in result.stderr
    (probe,) = (tmp_path / "repo" / "docker-probe.log").read_text().splitlines()
    assert probe.split() == [
        f"--user={pwd.getpwuid(os.getuid()).pw_name}",
        "--group=docker",
        f"--proc={tmp_path / 'proc'}",
        "--socket=/run/docker.sock",
    ]
    assert _log(tmp_path, "sudo") == []
    assert _log(tmp_path, "docker") == []
    assert not (tmp_path / "repo" / "pm2-actions.log").exists()
    record = _deploy_record(tmp_path)
    assert (record["stage"], record["rolled_back"]) == ("signer-preflight", "yes")


def test_a_disabled_signer_skips_the_docker_check(tmp_path: Path) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=SYSTEMD_ENV,
        control={"docker-probe-exit": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "repo" / "docker-probe.log").exists()


def test_the_pm2_copy_is_saved_away_before_activation_and_failure_reports(
    tmp_path: Path,
) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env=SYSTEMD_ENV,
        control={
            "sudo-activate-exit": "1",
            "systemctl-show": "ActiveState=failed\nMainPID=0\nNRestarts=10\n",
        },
        health_timeout="4",
    )

    assert result.returncode != 0
    timeline = _timeline(tmp_path)
    delete = timeline.index("pm2 delete ditto-api")
    # Saved at once, so a reboot after a failed activation cannot resurrect the
    # pm2 copy next to the enabled unit.
    assert timeline[delete + 1] == "pm2 save"
    activate = timeline.index(f"sudo -n {INSTALLER} activate")
    assert delete < activate
    assert timeline[activate + 1] == f"sudo -n {INSTALLER} logs"
    assert "deploy failed -- ditto-api could not be activated on release" in (
        result.stderr
    )
    assert "ditto-platform-api.service status/pid/restarts: errored" in result.stderr
    assert "synthetic journal line" in result.stderr
    assert _deploy_record(tmp_path)["stage"] == "api-activate"
    assert _deploy_record(tmp_path)["rolled_back"] == "no"


def test_a_failed_pylon_unit_prints_its_journal(tmp_path: Path) -> None:
    result, _, _, _ = _run_update(
        tmp_path,
        gcloud_source="exit 1\n",
        initial_env="DITTO_PLATFORM_PYLON_UNIT=ditto-platform-pylon.service\n",
        control={"sudo-restart-exit": "1"},
        health_commit=RUNNING_SHA,
    )

    assert result.returncode != 0
    assert _log(tmp_path, "sudo") == [
        "sudo -n /usr/bin/systemctl restart ditto-platform-pylon.service",
        "sudo -n /usr/bin/journalctl --no-pager --quiet --output=short-iso "
        "--lines=80 --unit=ditto-platform-pylon.service",
    ]
    assert "ditto-platform-pylon.service failed; its last journal lines" in (
        result.stderr
    )
    assert "synthetic journal line" in result.stderr
    assert _deploy_record(tmp_path)["stage"] == "infra"


def test_the_final_hint_names_the_supervisor_logs(tmp_path: Path) -> None:
    result, _, _, _ = _run_update(
        tmp_path, gcloud_source="exit 1\n", initial_env=SYSTEMD_ENV
    )

    assert result.returncode == 0, result.stderr
    assert f"done. sudo -n {INSTALLER} logs" in result.stdout
    assert "pm2 logs ditto-api" not in result.stdout


def _run_script(
    tmp_path: Path, script: str, env_file: str, *, sudo_exit: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "bin"
    (repo / "scripts").mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(ROOT / "scripts" / script, repo / "scripts" / script)
    shutil.copy2(
        ROOT / "scripts" / "ecosystem.config.js",
        repo / "scripts" / "ecosystem.config.js",
    )
    (repo / ".env").write_text(env_file)
    log = repo / "actions.log"
    for name in ("uv", "docker", "pm2"):
        _write_executable(fake_bin / name, f'printf "%s\\n" "{name} $*" >> "{log}"\n')
    _write_executable(
        fake_bin / "sudo",
        f'printf "%s\\n" "sudo $*" >> "{log}"\n'
        + (f"exit {sudo_exit}\n" if sudo_exit else ""),
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DITTO_", "COMPOSE_"))
    }
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        [str(repo / "scripts" / script)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, log.read_text().splitlines() if log.exists() else []


def test_start_uses_the_pylon_unit_instead_of_docker(tmp_path: Path) -> None:
    result, actions = _run_script(
        tmp_path,
        "start.sh",
        "DITTO_COMPOSE_SERVICES=pylon\n"
        "DITTO_PLATFORM_PYLON_UNIT=ditto-platform-pylon.service\n"
        "DITTO_PLATFORM_API_SUPERVISOR=systemd\n",
    )

    assert result.returncode == 0, result.stderr
    assert "sudo -n /usr/bin/systemctl restart ditto-platform-pylon.service" in actions
    assert not any(action.startswith("docker ") for action in actions)
    (start,) = [action for action in actions if action.startswith("pm2 start")]
    assert "--only ditto-screened-image-cleanup " in f"{start} "


def test_start_keeps_compose_by_default_and_refuses_unknown_units(
    tmp_path: Path,
) -> None:
    result, actions = _run_script(
        tmp_path, "start.sh", "DITTO_COMPOSE_SERVICES=pylon\n"
    )
    assert result.returncode == 0, result.stderr
    assert "docker compose up -d --wait pylon" in actions
    assert not any(action.startswith("sudo ") for action in actions)

    refused, actions = _run_script(
        tmp_path / "second", "start.sh", "DITTO_PLATFORM_PYLON_UNIT=evil.service\n"
    )
    assert refused.returncode != 0
    assert "DITTO_PLATFORM_PYLON_UNIT must be empty" in refused.stderr
    assert not any(action.startswith(("docker ", "sudo ")) for action in actions)


@pytest.mark.parametrize(
    ("supervisor", "expected"),
    [
        ("pm2", ["pm2 stop ditto-api", "pm2 delete ditto-api"]),
        ("systemd", [f"sudo -n {INSTALLER} stop"]),
    ],
)
def test_stop_follows_the_supervisor(
    tmp_path: Path, supervisor: str, expected: list[str]
) -> None:
    result, actions = _run_script(
        tmp_path, "stop.sh", f"DITTO_PLATFORM_API_SUPERVISOR={supervisor}\n"
    )
    assert result.returncode == 0, result.stderr
    assert actions == expected


def test_update_script_fixes_every_privileged_command() -> None:
    """sudoers allows exact argument vectors; the script may not vary them."""
    updater = (ROOT / "scripts" / "update.sh").read_text()
    assert (
        "readonly api_release_command=/usr/local/sbin/ditto-platform-api-release\n"
        in (updater)
    )
    assert "readonly api_unit=ditto-platform-api.service\n" in updater
    assert "readonly pylon_unit=ditto-platform-pylon.service\n" in updater
    sudo_lines = sorted(
        line.strip()
        for line in updater.splitlines()
        if "sudo -n" in line and not line.lstrip().startswith(("#", 'echo "'))
    )
    assert sudo_lines == sorted(
        [
            'if ! api_release_request | sudo -n "$api_release_command" install; then',
            'if ! sudo -n /usr/bin/systemctl restart "$pylon_unit"; then',
            "sudo -n /usr/bin/journalctl --no-pager --quiet --output=short-iso "
            "--lines=80 \\",
            'sudo -n "$api_release_command" stop',
            "if ! printf 'revision=%s\\n' \"$deploy_target\" | "
            'sudo -n "$api_release_command" activate; then',
            'sudo -n "$api_release_command" logs >&2 || \\',
        ]
    )
    # The one journal read is the exact Pylon argv its sudoers rule allows.
    assert updater.count("journalctl") == 1
    assert "        --unit=ditto-platform-pylon.service >&2 || \\\n" in updater
    assert "coding_hosted_signer_preflight" not in updater
    assert "DITTO_CODING_HOSTED_SIGNER_SEED_FILE" not in updater
