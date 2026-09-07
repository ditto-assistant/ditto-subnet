"""Synthetic policy and lifecycle tests; never modify the host firewall."""

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_connectivity"
spec = importlib.util.spec_from_file_location(
    "connectivity", ROLE / "files/connectivity-policy.py"
)
assert spec is not None and spec.loader is not None
POLICY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(POLICY)


def profile():
    return {
        "schema": "dittobench-coding-hosted-connectivity-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "trusted_loopback_tcp": True,
        "issued_at_unix": 2000000000,
        "expires_at_unix": 2000000600,
        "trusted_tcp": [
            {"address": "10.20.0.7", "port": 5432},
            {"address": "1.1.1.1", "port": 443},
        ],
        "trusted_dns": [{"address": "127.0.0.53", "port": 53}],
        "candidate_tcp": [{"address": "10.30.0.4", "port": 18080}],
    }


def test_worker_and_daemon_have_distinct_expiring_socket_authority():
    text = POLICY.policy(profile(), 1001, 2000000000)
    assert "type cgroupsv2; flags timeout;" in text
    assert '"system.slice/ditto-coding-hosted-worker.service" timeout 600s' in text
    assert '"user.slice/user-1001.slice/user@1001.service" timeout 600s' in text
    rules = text.splitlines()
    accepts = [line for line in rules if line.endswith("counter accept")]
    assert len(accepts) == 8
    for line in accepts:
        assert "meta skuid 1001 meta time >= 2000000000 meta time < 2000000600" in line
        assert "socket cgroupv2 level" in line
        if "tcp dport 443" in line or "tcp dport 5432" in line or "dport 53" in line:
            assert "level 2 @worker" in line
        if "tcp dport 18080" in line:
            assert "level 3 @daemon" in line
    assert "priority -150" in text  # Connection tracking is available here.
    assert "related" not in text and "flush ruleset" not in text
    assert rules[-1].endswith(
        "meta skuid 1001 counter reject with icmpx type admin-prohibited"
    )
    assert "ip6 daddr" not in text and "ip6 saddr" not in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("weight_eligible", True),
        ("shadow_only", False),
        ("issued_at_unix", 1999999000),
        ("issued_at_unix", 2000000001),
        ("expires_at_unix", 2000000000),
        ("expires_at_unix", 2000086401),
        ("expires_at_unix", True),
        ("candidate_tcp", []),
        ("trusted_tcp", []),
    ],
)
def test_invalid_authority_is_refused(field, value):
    config = profile()
    config[field] = value
    with pytest.raises(ValueError):
        POLICY.policy(config, 1001, 2000000000)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "169.254.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "10.0.0.0/8",
        "example.org",
        "1.1.1.1; accept",
        "01.1.1.1",
    ],
)
def test_no_metadata_ipv6_wildcards_names_or_injection(address):
    config = profile()
    config["trusted_tcp"][0]["address"] = address
    with pytest.raises(ValueError):
        POLICY.policy(config, 1001, 2000000000)


@pytest.mark.parametrize(
    "address,port", [("1.1.1.1", 443), ("127.0.0.1", 8080), ("10.0.0.1", 53)]
)
def test_candidate_cannot_gain_public_loopback_or_dns_authority(address, port):
    config = profile()
    config["candidate_tcp"] = [{"address": address, "port": port}]
    with pytest.raises(ValueError):
        POLICY.policy(config, 1001, 2000000000)


def test_endpoint_duplicates_and_boolean_ports_are_rejected():
    for mutate in (
        lambda c: c["trusted_tcp"].append(deepcopy(c["trusted_tcp"][0])),
        lambda c: c["trusted_tcp"][0].update(port=True),
        lambda c: c["trusted_dns"][0].update(port=443),
        lambda c: c.update(worker_cgroup="user.slice"),
    ):
        config = profile()
        mutate(config)
        with pytest.raises(ValueError):
            POLICY.policy(config, 1001, 2000000000)
    with pytest.raises(ValueError):
        POLICY.unique([("shadow_only", True), ("shadow_only", False)])


def lifecycle(monkeypatch):
    commands = []
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 0)
    monkeypatch.setattr(POLICY, "root_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        POLICY.runpy,
        "run_path",
        lambda _path: {
            "identity": lambda: SimpleNamespace(pw_uid=1001),
            "nft_policy": lambda _uid: "synthetic-deny",
        },
    )
    monkeypatch.setattr(POLICY, "worker_cgroup", lambda: 123)
    monkeypatch.setattr(POLICY, "configuration", profile)
    monkeypatch.setattr(POLICY.time, "time", lambda: 2000000000)
    monkeypatch.setattr(
        POLICY, "execute", lambda body, **kw: commands.append((body, kw))
    )
    return commands


def test_install_denies_first_then_checks_and_commits(monkeypatch):
    commands = lifecycle(monkeypatch)
    POLICY.main(["install"])
    assert commands[0] == ("synthetic-deny", {})
    assert commands[1][1] == {"check": True} and commands[2][1] == {}
    assert commands[1][0] == commands[2][0]


def test_validation_never_changes_firewall_or_requires_worker_cgroup(monkeypatch):
    commands = lifecycle(monkeypatch)

    def forbidden():
        pytest.fail("validation inspected worker cgroup")

    monkeypatch.setattr(POLICY, "worker_cgroup", forbidden)
    POLICY.main(["validate"])
    assert commands == []


def test_loopback_is_explicit_and_only_granted_to_worker():
    config = profile()
    config["trusted_loopback_tcp"] = False
    text = POLICY.policy(config, 1001, 2000000000)
    assert "ip daddr 127.0.0.1 meta l4proto tcp counter accept" not in text
    assert "ip daddr 127.0.0.1 meta l4proto tcp ct direction reply" in text
    config["trusted_loopback_tcp"] = "yes"
    with pytest.raises(ValueError):
        POLICY.policy(config, 1001, 2000000000)


@pytest.mark.parametrize("failure", ["config", "check", "commit", "inode"])
def test_every_failure_restores_deny(monkeypatch, failure):
    commands = lifecycle(monkeypatch)

    def execute(body, **kwargs):
        commands.append((body, kwargs))
        if failure == "check" and kwargs.get("check"):
            raise OSError("synthetic unsupported kernel")
        if failure == "commit" and len(commands) == 3:
            raise OSError("synthetic failed transaction")

    monkeypatch.setattr(POLICY, "execute", execute)
    if failure == "config":
        monkeypatch.setattr(POLICY, "configuration", lambda: {})
    if failure == "inode":
        values = iter([123, 456])
        monkeypatch.setattr(POLICY, "worker_cgroup", lambda: next(values))
    with pytest.raises((ValueError, OSError)):
        POLICY.main(["install"])
    assert commands[0][0] == commands[-1][0] == "synthetic-deny"


def test_revoke_needs_no_config_or_live_worker_cgroup(monkeypatch):
    commands = lifecycle(monkeypatch)

    def forbidden():
        pytest.fail("revoke accessed per-attempt authority")

    monkeypatch.setattr(POLICY, "configuration", forbidden)
    monkeypatch.setattr(POLICY, "worker_cgroup", forbidden)
    POLICY.main(["revoke"])
    assert commands == [("synthetic-deny", {})]


def test_nonroot_and_wrong_cgroup_cannot_install(monkeypatch):
    commands = lifecycle(monkeypatch)
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 1001)
    with pytest.raises(ValueError):
        POLICY.main(["install"])
    assert commands == []
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 0)

    def wrong_cgroup():
        raise ValueError("synthetic wrong cgroup")

    monkeypatch.setattr(POLICY, "worker_cgroup", wrong_cgroup)
    with pytest.raises(ValueError):
        POLICY.main(["install"])
    assert commands == []


def test_role_and_service_are_manual_default_off_and_nondelegated():
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["coding_hosted_connectivity_enabled"] is False
    assert defaults["coding_hosted_connectivity_profile"] == {}
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    assert tasks[1]["when"] == "coding_hosted_connectivity_enabled | bool"
    assert all(
        task.get("ansible.builtin.systemd_service", {}) == {"daemon_reload": True}
        for task in tasks[1]["block"]
        if "ansible.builtin.systemd_service" in task
    )
    unit = (ROLE / "templates/worker.service.j2").read_text()
    assert "Delegate=no" in unit and "Slice=system.slice" in unit
    assert "ExecStopPost=+" in unit and "connectivity-policy.py revoke" in unit
    assert "--private-shadow-once" in unit and "Restart=no" in unit
    assert "ProtectControlGroups=yes" in unit and "TimeoutStopSec=35min" in unit
    assert "[Install]" not in unit
    assert (
        "playbooks/gcp-coding-hosted-connectivity.yml"
        in (ROOT / ".github/workflows/infra-ci.yml").read_text()
    )
