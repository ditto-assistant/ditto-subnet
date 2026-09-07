"""Qualification-role tests; no host firewall, account or daemon is modified."""

import importlib.util
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted"
spec = importlib.util.spec_from_file_location(
    "hosted_host_policy", ROLE / "files/host-policy.py"
)
assert spec is not None and spec.loader is not None
POLICY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(POLICY)


def good_info():
    return {
        "OSType": "linux",
        "Architecture": "x86_64",
        "DockerRootDir": "/var/lib/ditto-coding-hosted/docker",
        "SecurityOptions": ["name=rootless", "name=seccomp,profile=builtin"],
        "Labels": ["io.heyditto.dittobench.isolated=true"],
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
        "Containers": 0,
        "Images": 0,
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
    }


def test_accepts_empty_rootless_cgroup_daemon():
    POLICY.validate_info(good_info())


def fixture_identity(monkeypatch):
    user = SimpleNamespace(
        pw_name=POLICY.USER,
        pw_uid=1001,
        pw_gid=1001,
        pw_dir=str(POLICY.DAEMON_HOME),
        pw_shell="/usr/sbin/nologin",
    )
    group = SimpleNamespace(gr_gid=1001, gr_mem=[])
    monkeypatch.setattr(POLICY.pwd, "getpwnam", lambda _name: user)
    monkeypatch.setattr(POLICY.pwd, "getpwall", lambda: [user])
    monkeypatch.setattr(POLICY.grp, "getgrnam", lambda _name: group)
    monkeypatch.setattr(POLICY.grp, "getgrall", lambda: [group])
    monkeypatch.setattr(POLICY.os, "getgrouplist", lambda _name, _gid: [1001])
    monkeypatch.setattr(
        POLICY.Path, "read_text", lambda _path: f"{POLICY.USER}:100000:65536\n"
    )
    return user, group


def test_identity_accepts_only_dedicated_unprivileged_account(monkeypatch):
    user, _group = fixture_identity(monkeypatch)
    assert POLICY.identity() is user


@pytest.mark.parametrize(
    "field,value",
    [
        ("pw_uid", 0),
        ("pw_gid", 0),
        ("pw_dir", "/root"),
        ("pw_shell", "/bin/bash"),
    ],
)
def test_identity_rejects_wrong_host_account(monkeypatch, field, value):
    user, _group = fixture_identity(monkeypatch)
    setattr(user, field, value)
    with pytest.raises(ValueError):
        POLICY.identity()


def test_identity_rejects_supplementary_group(monkeypatch):
    fixture_identity(monkeypatch)
    monkeypatch.setattr(POLICY.os, "getgrouplist", lambda _name, _gid: [1001, 27])
    with pytest.raises(ValueError):
        POLICY.identity()


def test_identity_rejects_other_primary_group_member(monkeypatch):
    _user, group = fixture_identity(monkeypatch)
    group.gr_mem = ["unexpected"]
    with pytest.raises(ValueError):
        POLICY.identity()


def test_identity_rejects_numeric_alias_mapping(monkeypatch):
    fixture_identity(monkeypatch)
    monkeypatch.setattr(
        POLICY.Path,
        "read_text",
        lambda _path: f"{POLICY.USER}:100000:65536\n1001:200000:65536\n",
    )
    with pytest.raises(ValueError):
        POLICY.identity()


@pytest.mark.parametrize(
    "field,value",
    [
        ("OSType", "windows"),
        ("Architecture", "aarch64"),
        ("DockerRootDir", "/var/lib/docker"),
        ("SecurityOptions", ["name=not-rootless"]),
        ("SecurityOptions", []),
        ("Labels", ["io.heyditto.dittobench.isolated=false"]),
        ("Labels", "io.heyditto.dittobench.isolated=true"),
        ("CgroupDriver", "none"),
        ("CgroupVersion", "1"),
        ("DriverStatus", []),
        ("Containers", 1),
        ("Images", 1),
        ("Containers", False),
        ("MemoryLimit", False),
        ("SwapLimit", False),
        ("CpuCfsQuota", False),
        ("PidsLimit", False),
        ("PidsLimit", 1),
    ],
)
def test_rejects_unsafe_daemon_info(field, value):
    info = good_info()
    info[field] = value
    with pytest.raises(ValueError):
        POLICY.validate_info(info)


def test_subordinate_range_is_nonoverlapping():
    assert POLICY.mapping(
        "owner:100000:65536\nother:165536:65536", "owner", [0, 1000]
    ) == (100000, 165536)


@pytest.mark.parametrize(
    "source,ids",
    [
        ("", [1000]),
        ("owner:100000:1", []),
        ("owner:0:65536", []),
        ("owner:100000:65536\nowner:200000:65536", []),
        ("owner:100000:65536\nother:165535:65536", []),
        ("owner:100000:65536\nother:99999:2", []),
        ("owner:100000:65536", [100000]),
        ("owner:100000:65536", [165535]),
        ("owner:4294967295:65536", []),
        ("owner:-1:65536", []),
    ],
)
def test_rejects_missing_short_ambiguous_or_overlapping_ranges(source, ids):
    with pytest.raises(ValueError):
        POLICY.mapping(source, "owner", ids)


@pytest.mark.parametrize("uid", [0, 999, -1, True, "1001", "1001; accept", 2**32])
def test_nft_policy_rejects_untrusted_uid(uid):
    with pytest.raises(ValueError):
        POLICY.nft_policy(uid)


def test_deny_policy_is_dual_stack_single_transaction_without_exceptions():
    rules = POLICY.nft_policy(1001)
    assert "table inet ditto_coding_hosted" in rules
    assert "meta skuid 1001 counter reject" in rules
    assert "hook output priority -310" in rules
    assert "icmpx type admin-prohibited" in rules
    assert "flush ruleset" not in rules
    assert "dport" not in rules and "daddr" not in rules
    assert "accept" not in rules.splitlines()[-1]
    assert len(rules.splitlines()) == 4


def test_execute_has_no_ambient_credentials(monkeypatch):
    monkeypatch.setenv("HOST_POLICY_SYNTHETIC_SECRET", "must-not-propagate")
    captured = {}

    def run(args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(POLICY.subprocess, "run", run)
    POLICY.execute(["/usr/bin/docker", "info"], uid=1001)
    assert set(captured["env"]) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
    }
    assert (
        captured["env"]["DOCKER_HOST"] == "unix:///run/ditto-coding-hosted/docker.sock"
    )
    assert captured["timeout"] == 30 and captured["check"] is True
    POLICY.execute(["/usr/sbin/nft", "-f", "-"], payload=POLICY.nft_policy(1001))
    assert set(captured["env"]) == {"PATH", "LANG", "LC_ALL"}
    assert captured["input"] == POLICY.nft_policy(1001)


def test_guard_checks_root_before_firewall_mutation(monkeypatch):
    monkeypatch.setattr(POLICY, "identity", lambda: SimpleNamespace(pw_uid=1001))
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(
        POLICY, "execute", lambda *_a, **_kw: pytest.fail("firewall called")
    )
    with pytest.raises(ValueError):
        POLICY.main(["guard"])


def test_guard_uses_only_fixed_nft_transaction(monkeypatch):
    monkeypatch.setattr(POLICY, "identity", lambda: SimpleNamespace(pw_uid=1001))
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 0)
    calls = []
    monkeypatch.setattr(POLICY, "execute", lambda *a, **kw: calls.append((a, kw)))
    POLICY.main(["guard"])
    assert calls == [
        ((["/usr/sbin/nft", "-f", "-"],), {"payload": POLICY.nft_policy(1001)})
    ]


def test_private_socket_requires_exact_owner_mode_and_no_symlink(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "docker.sock"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
        path.chmod(0o600)
        POLICY.private_path(path, os.getuid(), 0o600, socket=True)
        path.chmod(0o660)
        with pytest.raises(ValueError):
            POLICY.private_path(path, os.getuid(), 0o600, socket=True)
        path.chmod(0o600)
        with pytest.raises(ValueError):
            POLICY.private_path(path, os.getuid() + 1, 0o600, socket=True)
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        with pytest.raises(ValueError):
            POLICY.private_path(alias, os.getuid(), 0o600, socket=True)


def test_role_is_default_off_and_has_no_legacy_or_worker_activation():
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == {
        "coding_hosted_daemon_enabled": False,
        "coding_hosted_docker_version": "",
        "coding_hosted_packages_enabled": False,
        "coding_hosted_containerd_version": "",
        "coding_hosted_docker_key_sha256": "",
    }
    source = (ROLE / "tasks/main.yml").read_text()
    tasks = yaml.safe_load(source)
    assert len(tasks) == 2
    assert tasks[1]["when"] == "coding_hosted_daemon_enabled | bool"
    assert "role_coding_hosted" in source and "ditto-coding-hosted-v2" in source
    assert source.index("Refuse an existing native account") < source.index(
        "Install fixed policy files"
    )
    assert source.index("Refuse existing home contents") < source.index(
        "Create the dedicated empty primary group"
    )
    assert source.index("Commit deny policy") < source.index("Enable persistence")
    assert "create_home: false" in source
    assert source.index("Refuse existing home contents") < source.index(
        "Bootstrap reviewed packages"
    )
    assert source.index("Bootstrap reviewed packages") < source.index(
        "Verify preinstalled Docker"
    )
    assert "masked: true" in source and "enabled: false" in source
    assert source.index("Refuse an active rootful") < source.index(
        "Mask inactive rootful"
    )
    for forbidden in (
        "coding_executor",
        "docker run",
        "docker pull",
        "private-shadow-once",
        "ansible.builtin.apt:",
    ):
        assert forbidden not in source
    play = yaml.safe_load(
        (ROOT / "infra/ansible/playbooks/gcp-coding-hosted.yml").read_text()
    )
    assert play[0]["hosts"] == "role_coding_hosted" and play[0]["roles"] == [
        "coding_hosted"
    ]
    assert (
        "playbooks/gcp-coding-hosted.yml"
        in (ROOT / ".github/workflows/infra-ci.yml").read_text()
    )


def test_service_has_private_socket_clean_environment_and_fail_closed_lifecycle():
    daemon = (ROLE / "templates/daemon.service.j2").read_text()
    manager = (ROLE / "templates/manager.conf.j2").read_text()
    guard = (ROLE / "templates/egress.service.j2").read_text()
    assert "UMask=0077" in daemon and "chmod 0600" in daemon
    assert "ExecStart=/usr/bin/env -i " in daemon
    assert "Type=notify" in daemon and "NOTIFY_SOCKET=${NOTIFY_SOCKET}" in daemon
    assert "Restart=no" in daemon and "TimeoutStopSec=1800" in daemon
    assert "Delegate=cpu cpuset io memory pids" in daemon
    assert "BindsTo=ditto-coding-hosted-egress.service" in manager
    assert "Requires=ditto-coding-hosted-egress.service" in manager
    assert "\nExecStop=" not in guard
    policy = json.loads((ROLE / "files/daemon-policy.json").read_text())
    assert policy["log-driver"] == "none" and policy["no-new-privileges"] is True
    assert policy["live-restore"] is False
    assert policy["features"] == {"containerd-snapshotter": True}


def test_bootstrap_is_separately_gated_after_fresh_host_checks():
    source = (ROLE / "tasks/main.yml").read_text()
    tasks = yaml.safe_load(source)[1]["block"]
    bootstrap = next(
        t for t in tasks if t.get("ansible.builtin.include_tasks") == "packages.yml"
    )
    assert bootstrap["when"] == "coding_hosted_packages_enabled | bool"
    assert source.index("Refuse existing home contents") < source.index(
        bootstrap["name"]
    )
    package_tasks = yaml.safe_load((ROLE / "tasks/packages.yml").read_text())
    guards = package_tasks[0]["ansible.builtin.assert"]["that"]
    assert "coding_hosted_daemon_enabled | bool" in guards
    assert "coding_hosted_packages_enabled | bool" in guards
    assert any("docker_key_sha256" in g for g in guards)
    assert any("containerd_version" in g for g in guards)


def test_bootstrap_masks_before_apt_and_never_replaces_installed_runtimes():
    tasks = yaml.safe_load((ROLE / "tasks/packages.yml").read_text())
    mask_index = next(
        i
        for i, t in enumerate(tasks)
        if t["name"] == "Mask runtime units before any apt operation"
    )
    assert set(tasks[mask_index]["loop"]) == {
        "docker.service",
        "docker.socket",
        "containerd.service",
    }
    mask = tasks[mask_index]["ansible.builtin.systemd_service"]
    assert mask["masked"] is True and "state" not in mask and "enabled" not in mask
    reject_index = next(
        i
        for i, t in enumerate(tasks)
        if t["name"].startswith("Refuse an existing container")
    )
    assert reject_index < mask_index
    assert "docker.io" in tasks[reject_index]["loop"]
    installs = [
        (i, t["ansible.builtin.apt"])
        for i, t in enumerate(tasks)
        if "ansible.builtin.apt" in t
    ]
    assert len(installs) == 2
    for index, apt in installs:
        assert mask_index < index
        assert apt["policy_rc_d"] == 101
        assert apt["allow_unauthenticated"] is False
        assert apt["allow_downgrade"] is False
        assert apt["install_recommends"] is False
        assert apt["auto_install_module_deps"] is False
        assert apt["fail_on_autoremove"] is True
        assert apt["state"] == "present"
    assert installs[1][1]["name"] == [
        "docker-ce={{ coding_hosted_docker_version }}",
        "docker-ce-cli={{ coding_hosted_docker_version }}",
        "docker-ce-rootless-extras={{ coding_hosted_docker_version }}",
        "containerd.io={{ coding_hosted_containerd_version }}",
    ]


def test_bootstrap_uses_fixed_signed_origin_and_verifies_after_install():
    tasks = yaml.safe_load((ROLE / "tasks/packages.yml").read_text())
    download = next(
        t["ansible.builtin.get_url"] for t in tasks if "ansible.builtin.get_url" in t
    )
    assert download["url"] == "https://download.docker.com/linux/debian/gpg"
    assert download["checksum"] == "sha256:{{ coding_hosted_docker_key_sha256 }}"
    assert download["validate_certs"] is True
    repo = next(
        t["ansible.builtin.copy"]
        for t in tasks
        if "ansible.builtin.copy" in t
        and t["ansible.builtin.copy"]["dest"].endswith(".sources")
    )
    assert (
        "Suites: trixie" in repo["content"]
        and "Architectures: amd64" in repo["content"]
    )
    assert "Signed-By: /etc/apt/keyrings/coding-hosted-docker.asc" in repo["content"]
    assert "trusted=yes" not in repo["content"]
    assert tasks[-1]["ansible.builtin.command"]["argv"] == [
        "systemctl",
        "is-active",
        "docker.service",
        "docker.socket",
        "containerd.service",
    ]
    assert tasks[-1]["failed_when"].endswith(".rc not in [3, 4]")
