"""Native host prerequisites; synthetic checks never change a host or start a unit."""

import errno
import importlib.util
import ipaddress
import json
import re
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_prerequisites"
PLACEHOLDER = "{{ coding_hosted_prerequisites_host_address }}"
ROUTER_PORT = "{{ coding_hosted_prerequisites_router_port }}"
PROXY_PORT = "{{ coding_hosted_prerequisites_proxy_port }}"
WORKER = "30000000-0000-4000-8000-000000000003"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPER = load("hosted_prerequisites", ROLE / "files/host-prerequisites.py")
PROXY = load("hosted_egress_proxy", ROLE / "files/egress-proxy.py")
HOST = load(
    "hosted_policy_for_prerequisites",
    ROOT / "infra/ansible/roles/coding_hosted/files/host-policy.py",
)
CONNECTIVITY = load(
    "hosted_connectivity_for_prerequisites",
    ROOT
    / "infra/ansible/roles/coding_hosted_connectivity/files/connectivity-policy.py",
)
TASKS_SOURCE = (ROLE / "tasks/main.yml").read_text()
GUARD_SOURCE = (ROLE / "tasks/guard.yml").read_text()
FILES_SOURCE = (ROLE / "tasks/files.yml").read_text()
UNIT = (ROLE / "templates/egress-proxy.service.j2").read_text()
CONSTANTS = yaml.safe_load((ROLE / "vars/main.yml").read_text())


def render(address="10.33.0.2"):
    template = (ROLE / "templates/host-prerequisites.json.j2").read_text()
    assert template.count("{{") == 4 and template.count(PLACEHOLDER) == 2
    assert template.count(ROUTER_PORT) == template.count(PROXY_PORT) == 1
    for placeholder, value in (
        (PLACEHOLDER, address),
        (ROUTER_PORT, CONSTANTS["coding_hosted_prerequisites_router_port"]),
        (PROXY_PORT, CONSTANTS["coding_hosted_prerequisites_proxy_port"]),
    ):
        template = template.replace(placeholder, str(value))
    return json.loads(template)


def block():
    return yaml.safe_load(TASKS_SOURCE)[1]["block"]


def task(name):
    return next(item for item in block() if item["name"] == name)


def test_role_is_default_off_with_exact_host_source_and_confirmation():
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == {
        "coding_hosted_prerequisites_enabled": False,
        "coding_hosted_prerequisites_confirmation": "",
        "coding_hosted_prerequisites_source_revision": "",
        "coding_hosted_prerequisites_host_address": "",
    }
    tasks = yaml.safe_load(TASKS_SOURCE)
    assert len(tasks) == 2
    assert tasks[0]["when"] == "not (coding_hosted_prerequisites_enabled | bool)"
    assert tasks[1]["when"] == "coding_hosted_prerequisites_enabled | bool"
    assert block()[0] == {
        "name": "Run the exact host and input guard",
        "ansible.builtin.import_tasks": "guard.yml",
    }
    that = yaml.safe_load(GUARD_SOURCE)[0]["ansible.builtin.assert"]["that"]
    for condition in (
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'",
        "ansible_facts['architecture'] == 'x86_64'",
        "ansible_facts['distribution'] == 'Debian'",
        "ansible_facts['distribution_major_version'] == '13'",
        "coding_hosted_prerequisites_confirmation == "
        "'CONVERGE NATIVE CODING HOST PREREQUISITES'",
        "coding_hosted_prerequisites_source_revision | length == 40",
        "coding_hosted_prerequisites_source_revision is match('^[0-9a-f]{40}$')",
        "coding_hosted_prerequisites_host_address == "
        "(ansible_facts['default_ipv4'] | default({})).get('address')",
    ):
        assert condition in that
    assert task("Refuse check mode for an enabled convergence")[
        "ansible.builtin.assert"
    ]["that"] == ["not ansible_check_mode"]


def test_ports_are_role_constants_written_once():
    assert CONSTANTS == {
        "coding_hosted_prerequisites_router_port": 18080,
        "coding_hosted_prerequisites_proxy_port": 18090,
    }
    for path in ROLE.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            text = "" if path == ROLE / "vars/main.yml" else path.read_text()
            assert "18080" not in text and "18090" not in text, path
    # The helper and proxy take ports only from the rendered record and unit.
    assert f"egress-proxy.py {PLACEHOLDER} {PROXY_PORT}" in UNIT
    assert f"SocketBindAllow=ipv4:tcp:{PROXY_PORT}" in UNIT.splitlines()
    for placeholder in (ROUTER_PORT, PROXY_PORT):
        assert placeholder.strip("{} ") in TASKS_SOURCE


def test_playbook_fixture_and_ci_never_enable_the_role():
    playbook = yaml.safe_load(
        (
            ROOT / "infra/ansible/playbooks/gcp-coding-hosted-prerequisites.yml"
        ).read_text()
    )[0]
    assert playbook["hosts"] == "role_coding_hosted"
    assert playbook["roles"] == ["coding_hosted_prerequisites"]
    fixture = yaml.safe_load(
        (ROOT / "infra/ansible/tests/coding-hosted-prerequisites.yml").read_text()
    )
    assert all(play["hosts"] == "localhost" for play in fixture)
    assert fixture[0]["roles"] == ["coding_hosted_prerequisites"]
    assert "coding_hosted_prerequisites_enabled is false" in yaml.safe_dump(fixture)
    negative = (
        ROOT / "infra/ansible/tests/coding-hosted-prerequisites-negative.yml"
    ).read_text()

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    # No fixture play, task or set_fact can turn the role on.
    assert "coding_hosted_prerequisites_enabled" not in set(
        keys([fixture, yaml.safe_load(negative)])
    )
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-prerequisites.yml" in workflow
    assert "-i localhost, tests/coding-hosted-prerequisites.yml" in workflow
    assert "coding_hosted_prerequisites_enabled" not in workflow


def test_dittobench_ci_runs_the_go_record_test_when_its_role_inputs_change():
    go_test = (
        ROOT
        / "services/dittobench-api/internal/codinghostedruntime/prerequisites_test.go"
    ).read_text()
    read = re.findall(r'"\.\./\.\./\.\./\.\./(infra/[^"]+)"', go_test)
    assert sorted(read) == [
        "infra/ansible/roles/coding_hosted_prerequisites/templates/host-prerequisites.json.j2",
        "infra/ansible/roles/coding_hosted_prerequisites/vars/main.yml",
    ]
    workflow = yaml.safe_load((ROOT / ".github/workflows/dittobench.yml").read_text())
    # PyYAML reads the bare `on` key as True.
    assert set(read) <= set(workflow[True]["pull_request"]["paths"])


def test_rendered_record_passes_helper_and_fits_connectivity_candidate_tcp():
    record = render()
    assert record == {
        "schema": "dittobench-coding-hosted-host-prerequisites-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "router_listen": "10.33.0.2:18080",
        "egress_network": "ditto-coding-restricted",
        "egress_proxy": "http://10.33.0.2:18090",
        # Every language runtime and the Rust driver require exactly 10001.
        "candidate_uid": 10001,
        "candidate_gid": 10001,
    }
    assert HELPER.settings(record) == (("10.33.0.2", 18080), ("10.33.0.2", 18090))
    endpoints = [
        {"address": "10.33.0.2", "port": 18080},
        {"address": "10.33.0.2", "port": 18090},
    ]
    for rollout in (False, True):
        assert CONNECTIVITY.pairs(endpoints, candidate=True, rollout=rollout) == [
            ("10.33.0.2", 18080),
            ("10.33.0.2", 18090),
        ]


def test_every_guard_address_is_a_valid_record_and_nothing_else_in_the_subnet():
    that = yaml.safe_load(GUARD_SOURCE)[0]["ansible.builtin.assert"]["that"]
    guard = next(
        condition[len("coding_hosted_prerequisites_host_address is match('") : -2]
        for condition in that
        if condition.startswith("coding_hosted_prerequisites_host_address is match(")
    )
    accepted = [
        str(address)
        for address in ipaddress.IPv4Network("10.33.0.0/24")
        if re.match(guard, str(address))
    ]
    assert accepted == [f"10.33.0.{last}" for last in range(2, 254)]
    for address in accepted:
        HELPER.settings(render(address))


@pytest.mark.parametrize(
    "field,value",
    [
        ("router_listen", "0.0.0.0:18080"),
        ("router_listen", "127.0.0.1:18080"),
        ("router_listen", "8.8.8.8:18080"),
        ("router_listen", "169.254.169.254:18080"),
        ("router_listen", "100.64.0.1:18080"),
        ("router_listen", "10.33.0.2:80"),
        ("router_listen", "10.33.0.2:018080"),
        ("router_listen", "10.33.0.2:99999"),
        ("router_listen", "10.33.0.02:18080"),
        ("router_listen", "[fd00::1]:18080"),
        ("router_listen", "ditto-coding-hosted-v2:18080"),
        ("router_listen", "10.33.0.2:18090"),
        ("egress_proxy", "http://secret@10.33.0.2:18090"),
        ("egress_proxy", "https://10.33.0.2:18090"),
        ("egress_proxy", "http://10.33.0.2:18090/"),
        ("egress_proxy", "http://10.33.0.2:18090?x=1"),
        ("egress_proxy", "http://10.33.0.2"),
        ("egress_proxy", "http://10.33.0.3:18090"),
        ("egress_proxy", "http://8.8.8.8:18090"),
        ("egress_proxy", "10.33.0.2:18090"),
        ("egress_network", ""),
        ("egress_network", "-restricted"),
        ("egress_network", "Restricted"),
        ("egress_network", "a" * 129),
        ("egress_network", "ditto-job-0011223344556677"),
        ("egress_network", "restricted\n"),
        ("candidate_uid", 0),
        ("candidate_gid", 0),
        ("candidate_uid", 65537),
        ("candidate_uid", True),
        ("candidate_gid", "10001"),
        ("shadow_only", False),
        ("weight_eligible", True),
        ("schema", "dittobench-coding-hosted-runtime-v2"),
    ],
)
def test_record_rejects_what_the_runtime_or_connectivity_would_refuse(field, value):
    record = render()
    record[field] = value
    with pytest.raises(ValueError):
        HELPER.settings(record)


def test_record_is_a_closed_object():
    for mutate in (
        lambda record: record.update(docker_socket="/run/docker.sock"),
        lambda record: record.pop("egress_network"),
    ):
        record = render()
        mutate(record)
        with pytest.raises(ValueError):
            HELPER.settings(record)


def host_fixture(monkeypatch, *, port_range=(32768, 60999)):
    calls = []
    monkeypatch.setattr(HELPER, "search_path", lambda: ("synthetic-unit-path",))
    monkeypatch.setattr(
        HELPER, "unit_overrides", lambda paths: calls.append(("units", paths))
    )
    subordinate = {
        "/etc/subuid": f"{HOST.USER}:100000:65536\n",
        "/etc/subgid": f"{HOST.USER}:200000:65536\n",
    }
    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        HELPER,
        "host_policy",
        lambda: {"identity": lambda: calls.append("identity"), "mapping": HOST.mapping},
    )
    monkeypatch.setattr(HELPER.Path, "read_text", lambda path: subordinate[str(path)])
    monkeypatch.setattr(HELPER, "ephemeral_range", lambda: port_range)
    monkeypatch.setattr(
        HELPER, "bindable", lambda address, port: calls.append((address, port))
    )
    return calls


def test_check_maps_candidate_identity_and_probes_both_exact_listeners(monkeypatch):
    record = render()
    calls = host_fixture(monkeypatch)
    receipt = HELPER.check(record)
    assert calls == [
        "identity",
        ("10.33.0.2", 18080),
        ("10.33.0.2", 18090),
        ("units", ("synthetic-unit-path",)),
    ]
    assert receipt == {
        "schema": "dittobench-coding-hosted-host-prerequisites-check-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "router_listen": "10.33.0.2:18080",
        "egress_proxy": "http://10.33.0.2:18090",
        "egress_network": "ditto-coding-restricted",
        "candidate_host_uid": 110000,
        "candidate_host_gid": 210000,
        "services_started": False,
        "private_execution_ready": False,
    }


def test_check_requires_root_before_reading_host_state(monkeypatch):
    def forbidden():
        raise AssertionError("host state read before root check")

    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(HELPER, "host_policy", forbidden)
    with pytest.raises(ValueError):
        HELPER.check(render())


def test_check_refuses_ephemeral_overlap_busy_or_foreign_listener(monkeypatch):
    record = render()
    host_fixture(monkeypatch, port_range=(10000, 20000))
    with pytest.raises(ValueError):
        HELPER.check(record)

    host_fixture(monkeypatch)

    def busy(_address, _port):
        raise OSError("synthetic address in use")

    monkeypatch.setattr(HELPER, "bindable", busy)
    with pytest.raises(OSError):
        HELPER.check(record)


def test_check_refuses_a_range_that_cannot_map_the_candidate(monkeypatch):
    record = render()
    host_fixture(monkeypatch)
    monkeypatch.setattr(
        HELPER.Path, "read_text", lambda _path: f"{HOST.USER}:100000:1\n"
    )
    with pytest.raises(ValueError):
        HELPER.check(record)


@pytest.mark.parametrize("size", [10000, 65535, 65537])
def test_check_refuses_a_host_policy_range_of_another_size(monkeypatch, size):
    record = render()
    calls = host_fixture(monkeypatch)
    monkeypatch.setattr(
        HELPER,
        "host_policy",
        lambda: {
            "identity": lambda: None,
            "mapping": lambda _source, _user, _ids: (100000, 100000 + size),
        },
    )
    with pytest.raises(ValueError):
        HELPER.check(record)
    assert calls == []


def test_bind_probe_detects_busy_and_nonlocal_addresses():
    for reuse in (0, 1):  # Python sockets default to 0; Go net.Listen sets 1.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, reuse)
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            with pytest.raises(OSError) as busy:
                HELPER.bindable("127.0.0.1", held.getsockname()[1])
            assert busy.value.errno == errno.EADDRINUSE
    with pytest.raises(OSError):
        HELPER.bindable("192.0.2.1", 18080)  # TEST-NET-1 is never assigned locally.


def refused_port():
    """A loopback port left in server-side TIME_WAIT by one proxy refusal."""
    server = PROXY.Server(("127.0.0.1", 0), PROXY.Refusal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        assert exchange(port, b"GET / HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 405")
    finally:
        server.shutdown()
        server.server_close()
    # Without SO_REUSEADDR the old connection still owns the port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain:
        with pytest.raises(OSError) as busy:
            plain.bind(("127.0.0.1", port))
        assert busy.value.errno == errno.EADDRINUSE
    return port


def test_bind_probe_treats_time_wait_as_free_like_go_net_listen():
    HELPER.bindable("127.0.0.1", refused_port())


UNIT_NAMES = [
    f"{name}{suffix}"
    for name in (
        "ditto-coding-hosted-egress-proxy.service",
        "ditto-coding-hosted-egress-.service",
        "ditto-coding-hosted-.service",
        "ditto-coding-.service",
        "ditto-.service",
        "service",
    )
    for suffix in (".d", ".wants", ".requires", ".upholds")
] + [
    "ditto-coding-hosted-egress-proxy.socket",
    "ditto-coding-hosted-egress-proxy.timer",
    "ditto-coding-hosted-egress-proxy.path",
]


def test_unit_scan_covers_systemd_257_load_path_and_drop_in_names():
    # `systemd-analyze unit-paths` on Debian 13 (systemd 257.13-1~deb13u1).
    assert [str(path) for path in HELPER.UNIT_PATHS] == [
        "/etc/systemd/system.control",
        "/run/systemd/system.control",
        "/run/systemd/transient",
        "/run/systemd/generator.early",
        "/etc/systemd/system",
        "/etc/systemd/system.attached",
        "/run/systemd/system",
        "/run/systemd/system.attached",
        "/run/systemd/generator",
        "/usr/local/lib/systemd/system",
        "/usr/lib/systemd/system",
        "/run/systemd/generator.late",
    ]
    assert HELPER.unit_names() == UNIT_NAMES
    fragment = Path("/etc/systemd/system/ditto-coding-hosted-egress-proxy.service")
    assert fragment == HELPER.FRAGMENT


def analyze(monkeypatch, stdout):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(HELPER.subprocess, "run", run)
    return calls


def test_search_path_requires_the_exact_reviewed_list(monkeypatch):
    listing = "".join(f"{path}\n" for path in HELPER.UNIT_PATHS).encode()
    calls = analyze(monkeypatch, listing)
    assert HELPER.search_path() == HELPER.UNIT_PATHS
    argv, kwargs = calls[0]
    assert argv == ("/usr/bin/systemd-analyze", "unit-paths")
    assert kwargs["check"] is True and kwargs["env"]["PATH"] == "/usr/bin:/bin"
    lines = listing.decode().splitlines()
    for changed in (
        lines[:-1],
        [*lines, "/lib/systemd/system"],
        [lines[1], lines[0], *lines[2:]],
        [*lines[:4], "/opt/units", *lines[5:]],
    ):
        analyze(monkeypatch, "".join(f"{line}\n" for line in changed).encode())
        with pytest.raises(ValueError):
            HELPER.search_path()


def unit_tree(tmp_path, monkeypatch):
    paths = [tmp_path / str(path).lstrip("/") for path in HELPER.UNIT_PATHS]
    for path in paths:
        path.mkdir(parents=True)
    fragment = tmp_path / "etc/systemd/system" / HELPER.UNIT
    monkeypatch.setattr(HELPER, "FRAGMENT", fragment)
    protected = []
    monkeypatch.setattr(HELPER, "protected", protected.append)
    return paths, fragment, protected


def test_unit_scan_accepts_a_clean_host_and_the_installed_fragment(
    tmp_path, monkeypatch
):
    paths, fragment, protected = unit_tree(tmp_path, monkeypatch)
    (paths[10] / "multi-user.target.wants").mkdir()
    (paths[10] / "multi-user.target.wants/other.service").symlink_to("/dev/null")
    (paths[4] / "other.service.d").mkdir()
    HELPER.unit_overrides(paths)
    assert protected == []
    fragment.write_text("[Service]\n")
    HELPER.unit_overrides(paths)
    assert protected == [fragment]


@pytest.mark.parametrize("name", UNIT_NAMES)
def test_unit_scan_refuses_every_drop_in_dependency_or_trigger_name(
    tmp_path, monkeypatch, name
):
    paths, _fragment, _protected = unit_tree(tmp_path, monkeypatch)
    (paths[10] / name).mkdir()
    with pytest.raises(ValueError):
        HELPER.unit_overrides(paths)


@pytest.mark.parametrize("index", range(12))
def test_unit_scan_refuses_overrides_in_every_load_path(tmp_path, monkeypatch, index):
    paths, _fragment, _protected = unit_tree(tmp_path, monkeypatch)
    (paths[index] / "ditto-coding-hosted-.service.d").mkdir()
    (paths[index] / "ditto-coding-hosted-.service.d/weaken.conf").write_text("")
    with pytest.raises(ValueError):
        HELPER.unit_overrides(paths)


@pytest.mark.parametrize("index", [i for i in range(12) if i != 4])
def test_unit_scan_refuses_another_fragment_alias_or_mask(tmp_path, monkeypatch, index):
    paths, _fragment, _protected = unit_tree(tmp_path, monkeypatch)
    (paths[index] / HELPER.UNIT).symlink_to("/dev/null")
    with pytest.raises(ValueError):
        HELPER.unit_overrides(paths)


@pytest.mark.parametrize("kind", ["wants", "requires", "upholds"])
def test_unit_scan_refuses_dangling_reverse_dependency_links(
    tmp_path, monkeypatch, kind
):
    paths, _fragment, _protected = unit_tree(tmp_path, monkeypatch)
    directory = paths[10] / f"multi-user.target.{kind}"
    directory.mkdir()
    (directory / HELPER.UNIT).symlink_to("/nonexistent/" + HELPER.UNIT)
    with pytest.raises(ValueError):
        HELPER.unit_overrides(paths)


def fake_path(mode, uid=0, nlink=1, parents=()):
    info = SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink)
    return SimpleNamespace(lstat=lambda: info, parents=list(parents))


def test_protected_files_require_one_link_like_connectivity_root_path():
    root = [fake_path(0o040755)]
    HELPER.protected(fake_path(0o100444, parents=root))
    for unsafe in (
        fake_path(0o100444, nlink=2, parents=root),
        fake_path(0o100444, uid=1000, parents=root),
        fake_path(0o100446, parents=root),
        fake_path(0o120777, parents=root),
        fake_path(0o100444, parents=[fake_path(0o040777)]),
    ):
        with pytest.raises(ValueError):
            HELPER.protected(unsafe)


class Stream:
    def __init__(self, body):
        self.body = body

    def read(self, size):
        return self.body[:size]


@pytest.mark.parametrize(
    "argv,body",
    [
        (["check", "extra"], b"{}"),
        (["verify"], b"{}"),
        (["check"], b""),
        (["check"], b" " * 4097),
        (["check"], b'{"schema":"a","schema":"b"}'),
    ],
)
def test_main_accepts_only_one_bounded_unique_record(argv, body):
    with pytest.raises(ValueError):
        HELPER.main(argv, Stream(body))


@pytest.mark.parametrize(
    "argv",
    [
        ["127.0.0.1", "18090"],
        ["0.0.0.0", "18090"],
        ["8.8.8.8", "18090"],
        ["10.33.0.2", "80"],
        ["10.33.0.2", "018090"],
        ["10.33.0.2", "70000"],
        ["10.33.0.2"],
        ["10.33.0.2", "18090", "upstream.example"],
    ],
)
def test_proxy_listens_only_on_one_private_unprivileged_address(argv):
    with pytest.raises(ValueError):
        PROXY.listener(argv)


def test_proxy_accepts_the_record_listener():
    assert PROXY.listener(["10.33.0.2", "18090"]) == ("10.33.0.2", 18090)


def exchange(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(payload)
        chunks = []
        while chunk := client.recv(4096):
            chunks.append(chunk)
        return b"".join(chunks)


@pytest.fixture
def refusing_server(monkeypatch):
    monkeypatch.setattr(PROXY, "DEADLINE_SECONDS", 0.5)
    server = PROXY.Server(("127.0.0.1", 0), PROXY.Refusal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_proxy_restarts_at_once_after_a_refusal_but_never_shares_a_listener():
    port = refused_port()
    with PROXY.Server(("127.0.0.1", port), PROXY.Refusal) as restarted:
        assert restarted.server_address == ("127.0.0.1", port)
        assert restarted.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0
        with pytest.raises(OSError) as second:
            PROXY.Server(("127.0.0.1", port), PROXY.Refusal)
        assert second.value.errno == errno.EADDRINUSE


def test_proxy_refuses_connect_and_every_other_method(refusing_server):
    connect = exchange(
        refusing_server, b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert connect.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    plain = exchange(
        refusing_server, b"GET http://169.254.169.254/ HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert plain.startswith(b"HTTP/1.1 405 Method Not Allowed\r\n")
    # A slow client is closed at the deadline without a tunnel.
    assert exchange(refusing_server, b"CONNECT slow") == b""


@pytest.fixture
def closing_server(monkeypatch):
    """Refusing server that reports when it has closed each connection."""
    monkeypatch.setattr(PROXY, "DEADLINE_SECONDS", 2.0)
    closed = threading.Event()

    class Server(PROXY.Server):
        def shutdown_request(self, request):
            super().shutdown_request(request)
            closed.set()

    server = Server(("127.0.0.1", 0), PROXY.Refusal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], closed
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    "payload,status",
    [
        (
            b"POST http://example.invalid/ HTTP/1.1\r\nContent-Length: 60000\r\n\r\n"
            + b"a" * 60000,
            b"HTTP/1.1 405 Method Not Allowed\r\n",
        ),
        (b"CONNECT " + b"a" * 20000, b"HTTP/1.1 403 Forbidden\r\n"),
        (b"GET http://example.invalid/ HTTP/1.1\r\n\r\n", b"HTTP/1.1 405"),
    ],
)
def test_proxy_refusal_survives_unread_request_bytes(closing_server, payload, status):
    port, closed = closing_server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(payload)
        body = b""
        while chunk := client.recv(4096):
            body += chunk
        assert body.startswith(status)
        # Half-close as a client does after reading EOF; the proxy then closes.
        client.shutdown(socket.SHUT_WR)
        assert closed.wait(5)
        time.sleep(0.05)
        # Closing with unread bytes would have sent RST instead of FIN.
        assert client.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0


def test_proxy_drain_is_bounded_in_bytes_and_time(monkeypatch):
    assert PROXY.DRAIN_BYTES == 65536 and PROXY.DRAIN_SECONDS == 1.0
    monkeypatch.setattr(PROXY, "DRAIN_SECONDS", 0.2)
    received = []

    class Endless:
        def settimeout(self, timeout):
            assert 0 < timeout <= 0.2

        def recv(self, size):
            received.append(size)
            return b"a" * size

    handler = object.__new__(PROXY.Refusal)
    handler.request = Endless()
    handler.drain()
    assert sum(received) == PROXY.DRAIN_BYTES

    started = time.monotonic()

    class Trickle(Endless):
        def recv(self, _size):
            assert time.monotonic() - started < 0.5, "drain outlived its deadline"
            time.sleep(0.02)
            return b"a"

    # One deadline for the whole drain: a trickling client cannot extend it.
    handler.request = Trickle()
    handler.drain()
    assert 0.2 <= time.monotonic() - started < 0.5


def test_proxy_has_no_upstream_connection_path():
    source = (ROLE / "files/egress-proxy.py").read_text()
    for forbidden in (
        "create_connection",
        ".connect(",
        "urllib",
        "http.client",
        "getaddrinfo",
    ):
        assert forbidden not in source


def live_pattern():
    assertion = task(
        "Refuse to converge while a worker, custody instance or egress proxy is live"
    )["ansible.builtin.assert"]["that"][0]
    raw = assertion[
        assertion.index("search('") + len("search('") : assertion.index(
            "', multiline=True)"
        )
    ]
    return re.compile(raw.replace("\\\\", "\\"), re.MULTILINE)


@pytest.mark.parametrize(
    "line",
    [
        "ditto-coding-hosted-worker.service loaded active running Approved",
        f"ditto-coding-custody@{WORKER}.service loaded activating start-pre Native",
        "ditto-coding-hosted-egress-proxy.service loaded deactivating stop-sigterm x",
        "ditto-coding-hosted-egress-proxy.service loaded reloading reload Native",
        "ditto-coding-hosted-worker.service loaded maintenance cleaning Approved",
    ],
)
def test_live_units_are_refused(line):
    units = (
        "ditto-coding-hosted-worker.service not-found inactive dead x\n" + line + "\n"
    )
    assert live_pattern().search(units)


def test_stopped_or_failed_units_are_not_live():
    units = (
        "ditto-coding-hosted-worker.service loaded inactive dead Approved\n"
        f"ditto-coding-custody@{WORKER}.service loaded failed failed Native\n"
        "ditto-coding-hosted-egress-proxy.service not-found inactive dead x\n"
    )
    assert not live_pattern().search(units)
    assert not live_pattern().search("")
    command = task("List worker, custody and egress proxy units")[
        "ansible.builtin.command"
    ]
    assert command["argv"][-3:] == [
        "ditto-coding-hosted-worker.service",
        "ditto-coding-custody@*.service",
        "ditto-coding-hosted-egress-proxy.service",
    ]


INSTALL = "Install fixed prerequisite files without replacing existing bytes"
RECEIPT = "Record the source revision that installed these bytes"


def test_all_checks_precede_writes_and_nothing_is_started_enabled_or_created():
    names = [item["name"] for item in block()]
    first_write = names.index(INSTALL)
    for check in (
        "Refuse check mode for an enabled convergence",
        "Require the existing daemon-identity deny guard",
        "Refuse to converge while a worker, custody instance or egress proxy is live",
        "Require the policy directory and host policy from daemon provisioning",
        "Refuse to rewrite unexpected existing prerequisite state",
        "Refuse an unexpected convergence receipt path",
        "Check the record, unit search path and unit before installing anything",
        "Require the exact redacted check result",
    ):
        assert names.index(check) < first_write
    install = task(INSTALL)
    assert install["ansible.builtin.copy"]["force"] is False
    assert install["loop"] == ["helper", "proxy", "record", "unit"]
    assert install["register"] == "coding_hosted_prerequisites_install"
    reload = task(
        "Reload unit definitions after installing a file, "
        "without enabling or starting the proxy"
    )
    assert reload["ansible.builtin.systemd_service"] == {"daemon_reload": True}
    assert reload["when"] == "coding_hosted_prerequisites_install is changed"
    assert names.index(INSTALL) < names.index(reload["name"])
    state = task("Require the exact static, inactive and unmodified proxy unit")
    show = task("Read the loaded egress proxy unit state")["ansible.builtin.command"]
    properties = show["argv"][-1].removeprefix("--property=").split(",")
    expected = state["ansible.builtin.assert"]["that"][0]
    listed = re.findall(r"'([A-Za-z]+)=", expected)
    assert listed == sorted(listed) and sorted(properties) == listed
    for value in (
        "ActiveState=inactive",
        "DropInPaths=",
        "UnitFileState=static",
        "NeedDaemonReload=no",
        "WantedBy=",
        "RequiredBy=",
        "UpheldBy=",
        "BoundBy=",
        "TriggeredBy=",
        "OnFailureOf=",
        "OnSuccessOf=",
    ):
        assert f"'{value}'" in expected
    source = TASKS_SOURCE + GUARD_SOURCE + FILES_SOURCE
    for forbidden in (
        "state: started",
        "state: restarted",
        "enabled: true",
        "masked:",
        "enable-linger",
        "docker network",
        "ansible.builtin.user",
        "ansible.builtin.group",
        "runuser",
        "nft",
        "ansible.builtin.shell",
        "no_log",
    ):
        assert forbidden not in source
    assert "systemctl, start" not in source and "- start" not in source
    # The only removal is the role's own scratch directory.
    assert source.count("state: absent") == 1
    scratch = 'path: "{{ coding_hosted_prerequisites_scratch.path }}"'
    assert f"{scratch}\n            state: absent" in source


def test_pre_write_and_post_install_file_checks_share_one_task_file():
    imports = [item for item in block() if "ansible.builtin.import_tasks" in item]
    by_name = {item["name"]: item for item in imports}
    before = by_name["Refuse to rewrite unexpected existing prerequisite state"]
    after = by_name["Require exact root-owned installed bytes and modes"]
    names = [item["name"] for item in block()]
    assert (
        names.index(before["name"]) < names.index(INSTALL) < names.index(after["name"])
    )
    for item, installed in ((before, False), (after, True)):
        assert item["ansible.builtin.import_tasks"] == "files.yml"
        assert item["vars"] == {
            "coding_hosted_prerequisites_require_installed": installed
        }
    stat, assertion = yaml.safe_load(FILES_SOURCE)
    assert stat["ansible.builtin.stat"]["follow"] is False
    assert stat["ansible.builtin.stat"]["checksum_algorithm"] == "sha256"
    condition = " ".join(assertion["ansible.builtin.assert"]["that"][0].split())
    assert condition == (
        "(not item.stat.exists and not coding_hosted_prerequisites_require_installed) "
        "or (item.stat.isreg | default(false) and item.stat.uid == 0 and "
        "item.stat.nlink == 1 and item.stat.mode == item.item.value.mode and "
        "item.stat.checksum == item.item.value.content | hash('sha256'))"
    )


def test_receipt_records_the_installing_revision_outside_the_byte_compared_set():
    receipt_path = "/usr/local/lib/ditto-coding-hosted/host-prerequisites-receipt.json"
    render_files = task("Render every fixed prerequisite file in memory")
    files = render_files["ansible.builtin.set_fact"][
        "coding_hosted_prerequisites_files"
    ]
    assert receipt_path not in [item["path"] for item in files.values()]
    inspect = task("Inspect the convergence receipt without following links")
    assert inspect["ansible.builtin.stat"] == {"path": receipt_path, "follow": False}
    refuse = " ".join(
        task("Refuse an unexpected convergence receipt path")["ansible.builtin.assert"][
            "that"
        ][0].split()
    )
    for condition in ("isreg", "uid == 0", "nlink == 1", "mode == '0444'"):
        assert condition in refuse
    record = task(RECEIPT)
    copy = record["ansible.builtin.copy"]
    assert (copy["dest"], copy["owner"], copy["mode"]) == (receipt_path, "root", "0444")
    content = " ".join(copy["content"].split())
    for field in (
        "'schema': 'dittobench-coding-hosted-host-prerequisites-receipt-v2'",
        "'source_revision': coding_hosted_prerequisites_source_revision",
        "'record_sha256': coding_hosted_prerequisites_files.record.content "
        "| hash('sha256')",
        "'applied_at': now(utc=true, fmt='%Y-%m-%dT%H:%M:%SZ')",
        "to_json(sort_keys=true, separators=[',', ':'])",
    ):
        assert field in content
    assert " ".join(record["when"].split()) == (
        "coding_hosted_prerequisites_install is changed or "
        "not coding_hosted_prerequisites_receipt_state.stat.exists"
    )
    names = [item["name"] for item in block()]
    assert names.index("Require exact root-owned installed bytes and modes") < (
        names.index(RECEIPT)
    )


def test_unit_verify_reports_only_this_unit_and_fails_on_any_output():
    checks = task(
        "Check the record, unit search path and unit before installing anything"
    )["block"]
    verify = next(
        item for item in checks if item["name"].startswith("Require systemd to accept")
    )
    argv = verify["ansible.builtin.command"]["argv"]
    assert argv[:4] == [
        "/usr/bin/systemd-analyze",
        "verify",
        "--recursive-errors=no",
        "--man=no",
    ]
    assert " ".join(verify["failed_when"].split()) == (
        "coding_hosted_prerequisites_verify.rc != 0 or "
        "coding_hosted_prerequisites_verify.stdout | length > 0 or "
        "coding_hosted_prerequisites_verify.stderr | length > 0"
    )
    helper = next(item for item in checks if item["name"].startswith("Check address"))
    assert helper["ansible.builtin.command"]["argv"][-1] == "check"
    assert checks.index(helper) < checks.index(verify)


def test_one_router_port_serializes_rollout_attempts_on_this_host():
    # Bounded rollout admits max_parallel up to 4 but never runs two attempts with
    # the same router_listen at once; this record has exactly one.
    rollout = (
        ROOT / "apps/platform/ditto/api_server/coding_bounded_rollout.py"
    ).read_text()
    assert (
        "resources=tuple(config.wire.host.router_listen for config in configs)"
        in rollout
    )
    assert [key for key in render() if key.endswith("_listen")] == ["router_listen"]


def test_proxy_unit_is_manual_refusing_and_ip_confined():
    assert "[Install]" not in UNIT
    for line in (
        "Type=exec",
        "DynamicUser=yes",
        "ExecStart=/usr/bin/python3 -I -B /usr/local/lib/ditto-coding-hosted/"
        f"egress-proxy.py {PLACEHOLDER} {PROXY_PORT}",
        "Restart=no",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "RestrictAddressFamilies=AF_INET",
        "SocketBindDeny=any",
        f"SocketBindAllow=ipv4:tcp:{PROXY_PORT}",
        "IPAddressDeny=any",
        f"IPAddressAllow={PLACEHOLDER}/32",
        "StandardOutput=null",
        "StandardError=null",
    ):
        assert line in UNIT.splitlines()
    for forbidden in (
        "User=ditto-coding-hosted",
        "WantedBy",
        "Restart=always",
        "AF_UNIX",
        "AF_INET6",
    ):
        assert forbidden not in UNIT


def test_doc_states_the_boundaries():
    doc = " ".join(
        (ROOT / "infra/docs/coding-hosted-prerequisites-v2.md").read_text().split()
    )
    for boundary in (
        "CONVERGE NATIVE CODING HOST PREREQUISITES",
        "does not create a Docker network",
        "ditto-job-",
        "empty allowlist",
        "never enables or starts",
        "10001",
        "candidate_tcp",
        "Rollback",
        "weight_eligible=false",
        "one concurrent attempt",
        "additional router ports",
        "today it is advisory",
        "coding_hosted_attempt_config.py",
        "host-prerequisites-receipt.json",
        "--recursive-errors=no",
        "SO_REUSEADDR",
    ):
        assert boundary in doc
    connectivity = " ".join(
        (ROOT / "infra/docs/coding-hosted-connectivity-v2.md").read_text().split()
    )
    required = connectivity[connectivity.index("## Qualification still required") :]
    assert "qualified rootless candidate network and proxy enforcement" in required
