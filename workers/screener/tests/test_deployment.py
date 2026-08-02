import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONOREPO_ROOT = Path(__file__).resolve().parents[3]


def workflow_text(name: str) -> str:
    return (MONOREPO_ROOT / ".github" / "workflows" / name).read_text()


def test_embedded_protocol_version_matches_root_pin_and_lock() -> None:
    root = tomllib.loads((MONOREPO_ROOT / "pyproject.toml").read_text())
    protocol = tomllib.loads(
        (
            MONOREPO_ROOT / "packages" / "ditto-screening-protocol" / "pyproject.toml"
        ).read_text()
    )
    version = protocol["project"]["version"]
    assert f"ditto-screening-protocol=={version}" in root["project"]["dependencies"]
    assert (
        f'name = "ditto-screening-protocol"\nversion = "{version}"'
        in (MONOREPO_ROOT / "uv.lock").read_text()
    )


def test_installed_signing_contract_probe_exercises_reviewer_binding() -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify-installed-signing-contract.py"),
        ],
        check=True,
    )


def test_deploy_reinstalls_and_probes_embedded_protocol() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()
    bootstrap = (ROOT / "scripts" / "bootstrap-screener.sh").read_text()

    assert updater.count("--reinstall-package ditto-screening-protocol") == 3
    assert "verify_installed_signing_contract" in updater
    assert updater.index(
        "verify_installed_signing_contract\nensure_l2_analyzer"
    ) < updater.index('systemctl restart "$SCREENER_UNIT"')
    assert "--reinstall-package ditto-screening-protocol" in bootstrap
    assert "verify-installed-signing-contract.py" in bootstrap


def test_deploy_workflow_discovers_screeners_by_label_not_a_fixed_vm() -> None:
    workflow = workflow_text("screener-deploy.yml")

    # The pet VM name/zone are no longer hardcoded: discovery is label-driven.
    assert "SCREENER_VM: ditto-screener-prod" not in workflow
    assert "GCP_ZONE: us-central1-c" not in workflow
    assert "labels.env=prod" in workflow
    assert "labels.role=screener" in workflow
    assert "labels.role=screener-fleet" in workflow
    # Zone projection is normalized to a bare name for --zone.
    assert "zone.basename()" in workflow


def test_deploy_workflow_fans_out_over_the_fleet_in_parallel() -> None:
    workflow = workflow_text("screener-deploy.yml")

    # Discovery feeds a matrix so hosts deploy concurrently (bounded), instead of
    # a sequential loop that could exceed the job timeout on a growing fleet.
    assert "matrix: ${{ fromJson(needs.discover.outputs.matrix) }}" in workflow
    assert "fail-fast: false" in workflow
    assert "max-parallel:" in workflow
    # Each host receives the exact GitHub release commit resolved once by the
    # discovery job, never whatever happens to be current on main.
    assert '"$name" "$zone" \'${{ needs.discover.outputs.revision }}\'' in workflow


def test_deploy_workflow_enables_numpy_before_iap_transport() -> None:
    workflow = workflow_text("screener-deploy.yml")
    deploy_job = workflow.split("\n  deploy:\n", 1)[1]

    # IAP checks for NumPy in gcloud's own interpreter and automatically uses
    # the accelerated websocket path when the import succeeds.
    setup = deploy_job.index("google-github-actions/setup-gcloud@")
    python = deploy_job.index("gcloud info --format='value(basic.python_location)'")
    install = deploy_job.index('"$gcloud_python" -m pip install')
    verify = deploy_job.index('import numpy; print(f"NumPy {numpy.__version__}')
    transport = deploy_job.index("deploy-screener-via-ssh.sh")
    assert setup < python < install < verify < transport


def test_deploy_streams_updater_over_one_ssh_session() -> None:
    workflow = workflow_text("screener-deploy.yml")
    transport = (ROOT / "scripts" / "deploy-screener-via-ssh.sh").read_text()

    assert "gcloud compute scp" not in workflow
    assert "deploy-screener-via-ssh.sh" in workflow
    assert transport.count("gcloud compute ssh") == 1
    assert '<"$updater"' in transport
    assert "/tmp/update-screener.sh" not in transport
    assert "SCREENER_EXPECTED_SHA=$expected_sha /bin/bash -s" in transport
    assert "exec gcloud" in transport
    assert "retry" not in transport.split("exec gcloud", 1)[1]


def test_single_ssh_transport_preserves_bytes_and_exit_status(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    captured_stdin = tmp_path / "stdin"
    captured_args = tmp_path / "args"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" >"$CAPTURED_ARGS"\n'
        'cat >"$CAPTURED_STDIN"\n'
        "exit 23\n"
    )
    fake_gcloud.chmod(0o755)

    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "GCP_PROJECT": "test-project",
        "CAPTURED_STDIN": str(captured_stdin),
        "CAPTURED_ARGS": str(captured_args),
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "deploy-screener-via-ssh.sh"),
            "screener-1",
            "us-central1-a",
            "a" * 40,
        ],
        env=env,
        check=False,
    )

    assert result.returncode == 23
    assert (
        captured_stdin.read_bytes()
        == (ROOT / "scripts" / "update-screener.sh").read_bytes()
    )
    args = captured_args.read_text().splitlines()
    assert args[:3] == ["compute", "ssh", "screener-1"]
    assert args.count("ssh") == 1
    assert "--tunnel-through-iap" in args
    remote_command = "sudo -n env SCREENER_EXPECTED_SHA=" + "a" * 40 + " /bin/bash -s"
    assert remote_command in args


def test_pull_request_ci_keeps_fast_safety_gates() -> None:
    workflow = workflow_text("screener-ci.yml")

    assert "pull_request:" in workflow
    assert 'uv run pytest -m "not integration"' in workflow
    assert "uv run ruff format --check ." in workflow
    assert "uv run ruff check ." in workflow
    assert "uv run mypy ditto_screener" in workflow
    assert "docker build -f workers/screener/Dockerfile" in workflow
    assert "tests/test_gate_docker_integration.py" not in workflow
    assert "screener-core-e2e" not in workflow


def test_core_e2e_is_daily_and_manually_dispatchable() -> None:
    workflow = workflow_text("screener-core-e2e.yml")

    assert "schedule:" in workflow
    assert 'cron: "17 8 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "tests/test_gate_docker_integration.py" in workflow
    assert "tests/test_l2_review.py" in workflow
    assert "DITTO_STARTER_KIT_DIR" in workflow
    assert "if: always()" in workflow


def test_screener_is_a_monorepo_component_with_release_scoped_deploy() -> None:
    components = tomllib.loads(
        (MONOREPO_ROOT / "release" / "components.toml").read_text()
    )["components"]
    deploy_workflow = workflow_text("screener-deploy.yml")

    assert components["screener"]["paths"] == ["workers/screener/**"]
    assert components["screener"]["depends_on"] == ["screening_protocol"]
    assert "push:\n    branches: [main]" not in deploy_workflow
    assert "gh release view" in deploy_workflow
    assert "schedule:" in deploy_workflow
    assert "workflow_dispatch:" in deploy_workflow


def test_updater_enables_the_unit_so_it_survives_a_reboot() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    # First boot restarts the unit but a reboot then short-circuits on the
    # bootstrap marker; the unit must be ENABLED to come back.
    assert "ensure_enabled" in updater
    assert 'systemctl enable "$SCREENER_UNIT"' in updater


def test_updater_reports_running_sha_from_a_marker_not_git_head() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    # The fast path must gate on the health-verified deployed-SHA marker, which
    # is written only AFTER a healthy restart — never on bare git HEAD, which a
    # run interrupted between reset and restart leaves at a not-yet-running SHA.
    assert "deployed_marker=" in updater
    assert "record_deployed_sha" in updater
    marker_write = updater.index('record_deployed_sha "$actual_sha"')
    health_check = updater.index("if ! wait_for_health")
    assert health_check < marker_write


def test_updater_and_bootstrap_serialize_on_a_shared_deploy_lock() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()
    bootstrap = (ROOT / "scripts" / "bootstrap-screener.sh").read_text()

    assert "flock" in updater
    assert "flock" in bootstrap
    # Bootstrap holds the lock and tells the updater it already holds it so the
    # nested updater invocation does not deadlock re-acquiring.
    assert "SCREENER_DEPLOY_LOCK_HELD=1" in bootstrap
    assert "SCREENER_DEPLOY_LOCK_HELD:-" in updater


def test_bootstrap_and_updater_keep_runtime_state_writable_by_worker() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()
    bootstrap = (ROOT / "scripts" / "bootstrap-screener.sh").read_text()

    expected = 'install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0750 '
    assert expected + '"$STATE_DIR"' in bootstrap
    assert expected + '"$gc_state_dir"' in updater
    # Ownership repair must run before the updater's healthy fast-path exit.
    repair = updater.index("\nensure_state_dir\n")
    fast_path = updater.index('echo "healthy: $SCREENER_UNIT already at')
    assert repair < fast_path


def test_bootstrap_blocks_metadata_and_mounts_no_build_credential() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap-screener.sh").read_text()
    gate = (ROOT / "ditto_screener" / "gate.py").read_text()

    # IMDS guard: metadata IP dropped from Docker's FORWARD (DOCKER-USER) path.
    assert "169.254.169.254" in bootstrap
    assert "DOCKER-USER" in bootstrap
    # The reusable GH token is no longer fetched or handed to untrusted builds.
    # (The retryable-error deny-list markers for "secret gh_token" stay; it is
    # the BuildKit --secret MOUNT that must be gone from the build args.)
    assert "SCREENER_GH_TOKEN_SECRET" not in bootstrap
    assert "id=gh_token,src=" not in gate
    assert "--secret" not in gate


def test_rootless_executor_is_separate_from_worker_and_denies_private_egress() -> None:
    installer = (ROOT / "scripts" / "install-rootless-docker.sh").read_text()
    bootstrap = (ROOT / "scripts" / "bootstrap-screener.sh").read_text()
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()
    egress = (ROOT / "deploy" / "executor-egress-guard.sh").read_text()
    executor_unit = (
        ROOT / "deploy" / "ditto-screener-egress-guard.service"
    ).read_text()

    assert 'EXECUTOR_USER="${SCREENER_EXECUTOR_USER:-ditto-builder}"' in installer
    assert 'user_unit_dir="$EXECUTOR_HOME/.config/systemd/user"' in installer
    assert "systemctl --user" in installer
    assert 'systemctl start "user@${uid}.service"' in installer
    assert "User=${EXECUTOR_USER}" not in installer
    assert "User=${SCREENER_USER}" not in installer
    assert "Type=notify\n# dockerd runs inside RootlessKit" in installer
    assert "NotifyAccess=all" in installer
    assert "Environment=XDG_RUNTIME_DIR=${user_runtime_dir}" in installer
    assert (
        "Environment=DBUS_SESSION_BUS_ADDRESS="
        "unix:path=${user_runtime_dir}/bus" in installer
    )
    assert "ExecStartPre=/usr/bin/systemctl is-active --quiet " in installer
    assert "RuntimeDirectory=ditto-screener-docker" not in installer
    assert (
        "ExecStartPost=/bin/chgrp ${EXECUTOR_GROUP} "
        "${runtime_dir}/docker.sock" in installer
    )
    assert "ExecStartPost=/bin/chmod 0660 ${runtime_dir}/docker.sock" in installer
    assert 'gpasswd -d "$SCREENER_USER" docker' in installer
    assert "SCREENER_REQUIRE_ROOTLESS_DOCKER=1" in bootstrap
    assert "DITTO-EXEC-EGRESS" in egress
    assert "10.0.0.0/8" in egress
    assert "127.0.0.0/8" in egress
    assert "169.254.0.0/16" in egress
    assert "192.168.0.0/16" in egress
    assert "Before=ditto-screener-docker.service" not in executor_unit
    guard_start = installer.index(
        "systemctl enable --now ditto-screener-egress-guard.service"
    )
    user_daemon_start = installer.index(
        '"${user_systemctl[@]}" enable --now "$SCREENER_ROOTLESS_UNIT"'
    )
    assert guard_start < user_daemon_start
    assert 'daemon_root="$EXECUTOR_HOME/docker"' in installer
    assert "SCREENER_EXECUTOR_HOME=/var/lib/ditto-screener-docker" in bootstrap
    assert 'executor_home="$(env_value SCREENER_EXECUTOR_HOME)"' in updater
    assert 'target="$executor_home/docker/daemon.json"' in updater
    assert 'owner="$(env_value SCREENER_EXECUTOR_USER)"' in updater
    assert 'group="$(env_value SCREENER_EXECUTOR_GROUP)"' in updater
    assert "$SCREENER_ROOT/docker/daemon.json" not in updater


def test_rootless_executor_disables_single_file_log_compression() -> None:
    daemon_config = json.loads((ROOT / "deploy" / "rootless-daemon.json").read_text())

    assert daemon_config["log-driver"] == "local"
    assert daemon_config["log-opts"] == {
        "compress": "false",
        "max-file": "1",
        "max-size": "8m",
    }


def test_updater_ensures_the_metadata_guard_on_every_deploy() -> None:
    # The pet VM was hand-provisioned and never ran bootstrap, so the guard has
    # to be (re)installed by the updater — the one path that runs on both the pet
    # and every fleet instance — or the pet keeps running exposed to metadata
    # exfil. It must run before the fast-path early-exit so a no-op deploy still
    # protects the host.
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    assert "169.254.169.254" in updater
    assert "DOCKER-USER" in updater
    assert "ensure_imds_guard" in updater

    guard_call = updater.index("\nensure_imds_guard\n")
    fast_path = updater.index('echo "healthy: $SCREENER_UNIT already at')
    assert guard_call < fast_path


def test_imds_guard_preserves_gce_dns_before_dropping_metadata() -> None:
    """The metadata IP is also the GCE VM's DNS resolver.

    A broad DOCKER-USER drop caused every clean build to lose DNS. Both the
    golden-image bootstrap and the pet/fleet updater must install the same
    ordered policy: DNS first, all other metadata-server traffic second.
    """
    for script_name in ("bootstrap-screener.sh", "update-screener.sh"):
        script = (ROOT / "scripts" / script_name).read_text()
        guard_start = script.index("iptables -N DOCKER-USER")
        guard_end = script.index("\nGUARD", guard_start)
        guard = script[guard_start:guard_end]

        udp_dns = guard.index(
            '-A "$guard_tmp" -p udp -d 169.254.169.254/32 --dport 53 -j ACCEPT'
        )
        tcp_dns = guard.index(
            '-A "$guard_tmp" -p tcp -d 169.254.169.254/32 --dport 53 -j ACCEPT'
        )
        metadata_drop = guard.index('-A "$guard_tmp" -d 169.254.169.254/32 -j DROP')

        assert udp_dns < metadata_drop
        assert tcp_dns < metadata_drop
        replacement_jump = guard.index('-I DOCKER-USER 1 -j "$guard_tmp"')
        old_jump_removal = guard.index("-D DOCKER-USER -j DITTO-IMDS-GUARD")
        assert metadata_drop < replacement_jump < old_jump_removal
        assert "-D DOCKER-USER -d 169.254.169.254/32 -j DROP" in guard
        assert '-E "$guard_tmp" DITTO-IMDS-GUARD' in guard


def test_updater_restarts_changed_imds_guard_unit() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    assert (
        """if [[ "$changed" -eq 1 ]]; then
    systemctl daemon-reload
    systemctl restart ditto-imds-guard.service
  else
    systemctl start ditto-imds-guard.service
  fi"""
        in updater
    )


def test_updater_probes_dns_through_a_fresh_container_after_guarding_imds() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    assert "probe_docker_dns" in updater
    assert "getent hosts github.com" in updater
    guard_call = updater.index("\nensure_imds_guard\n")
    probe_call = updater.index("\nprobe_docker_dns\n")
    fast_path = updater.index('echo "healthy: $SCREENER_UNIT already at')
    assert guard_call < probe_call < fast_path


def test_bootstrap_bake_mode_provisions_before_any_secret() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap-screener.sh").read_text()

    assert "SCREENER_BAKE_ONLY" in bootstrap
    # Bake must exit before fetching any secret, so nothing sensitive is baked.
    bake_exit = bootstrap.index(
        'if [[ "$SCREENER_BAKE_ONLY" == "1" ]]; then\n  runuser'
    )
    first_secret = bootstrap.index('read_secret "$SCREENER_MNEMONIC_SECRET"')
    assert bake_exit < first_secret
    # The deploy key is installed only outside bake mode (never baked in).
    assert (
        'if [[ "$SCREENER_BAKE_ONLY" != "1" ]]; then\n  install -o "$SCREENER_USER"'
        in bootstrap
    )


def test_golden_image_bake_pipeline_exists() -> None:
    packer = (ROOT / "packer" / "screener-fleet.pkr.hcl").read_text()
    workflow = workflow_text("screener-bake.yml")

    assert "image_family      = var.image_family" in packer
    assert "ditto-screener-fleet" in packer
    # Bakes via the same bootstrap script in bake mode; stores no secret.
    assert "SCREENER_BAKE_ONLY=1" in packer
    assert "environment: prod" in workflow
    assert "GCP_SCREENER_BAKE_SA" in workflow


def test_systemd_unit_runs_the_extracted_screener_entrypoint() -> None:
    unit = (ROOT / "deploy" / "ditto-screener.service").read_text()

    assert (
        "ExecStart=/opt/ditto/screener/src/workers/screener/.venv/bin/ditto-screener"
        in unit
    )
    assert "ditto.screener" not in unit
    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=35min" in unit


def test_updater_installs_and_rolls_back_the_repository_owned_unit() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    assert 'unit_source="$source_dir/deploy/ditto-screener.service"' in updater
    assert 'install -o root -g root -m 0644 "$unit_source" "$unit_file"' in updater
    assert 'install -o root -g root -m 0644 "$unit_backup" "$unit_file"' in updater
    assert "consecutive_healthy" in updater
    assert "validator-openrouter-key" in updater
    assert "ditto-app-dev" in updater
    assert 'install -o "$SCREENER_USER" -g ditto -m 0400' in updater
    assert "SCREENER_SOURCE_REVIEW_API_KEY_FILE" in updater
    assert "required_policy_version" in updater
    assert "SCREENING_POLICY_VERSION" in updater


def test_updater_keeps_the_trusted_l2_analyzer_ready_for_dynamic_settings() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()
    dockerfile = (ROOT / "deploy" / "l2-analyzer.Dockerfile").read_text()

    assert "ensure_l2_analyzer" in updater
    function = updater.split("ensure_l2_analyzer() {", 1)[1].split(
        "\n}\n\nwait_for_health", 1
    )[0]
    assert 'mode="$(l2_mode)"' not in function
    assert '[[ "$mode"' not in function
    assert "ai.heyditto.screener.sha=$sha" in updater
    assert 'ensure_l2_analyzer "$current_sha"' in updater
    assert '"$l2_analyzer_image" build_structure' in updater
    assert "--network none --read-only --cap-drop ALL" in updater
    assert "deployed-l2-mode" in updater
    assert 'deployed_l2_mode" == "$requested_l2_mode' in updater
    assert 'record_l2_mode "$requested_l2_mode"' in updater
    assert "USER 65532:65532" in dockerfile
    assert "ENTRYPOINT" in dockerfile


def test_updater_drops_the_stale_pre_extraction_namespace() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    # git reset --hard leaves the untracked ``ditto/`` namespace behind, which
    # keeps shadowing the ``ditto_screener`` import path.
    assert "workers/screener/ditto" in updater


def test_updater_defers_daemon_restart_during_an_active_build() -> None:
    updater = (ROOT / "scripts" / "update-screener.sh").read_text()

    assert "build_in_flight" in updater
    assert 'pgrep -f "build -t ditto-screen"' in updater
    # The guard must sit before the disruptive docker restart.
    guard = updater.index("deferring daemon.json apply")
    restart = updater.index('systemctl restart "$daemon_unit"')
    assert guard < restart
