from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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
    _write_executable(fake_bin / "docker", ":\n")
    _write_executable(
        fake_bin / "pm2",
        f'if [ "${{1:-}}" = "jlist" ]; then cat "{repo}/jlist.json"; fi\n'
        f'printf "%s\\n" "pm2 $*" >> "{repo}/pm2-actions.log"\n',
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
            "DITTO_COMPOSE_SERVICES",
            "DITTO_DASHBOARD_WANDB_URL",
            "DITTO_DEPLOY_BRANCH",
            "DITTO_DEPLOY_COMMIT",
            "DITTO_HEALTH_TIMEOUT",
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
