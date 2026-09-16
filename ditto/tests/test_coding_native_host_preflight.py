"""Synthetic post-import inspection; never contacts a host or daemon."""

import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto.tests.test_coding_hosted_image_import import configuration
from ditto.tests.test_coding_native_enforcement_evidence import EVIDENCE
from ditto.tests.test_coding_native_release import (
    RELEASE,
    REVISION,
    protected_fixture_permissions,  # noqa: F401
    release,  # noqa: F401
)

ROOT = Path(__file__).parents[2]
NFT_FIXTURES = Path(__file__).parent / "fixtures/coding_native_network"
TABLE = "ditto_coding_hosted"
SCRIPT = ROOT / "infra/scripts/inspect-coding-native-host.py"
DAEMON_VECTOR = json.loads(
    (
        ROOT / "services/dittobench-api/internal/codingenforcement/catalog/testdata/"
        "daemon-identity-vector-v1.json"
    ).read_bytes()
)
spec = importlib.util.spec_from_file_location("native_host_preflight", SCRIPT)
assert spec is not None and spec.loader is not None
HOST = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HOST)


@pytest.fixture
def context(request, monkeypatch):
    directory, pin = request.getfixturevalue("release")
    value = RELEASE.verify(directory, REVISION, pin)
    config = {
        "schema": "dittobench-coding-native-host-preflight-config-v2",
        "source_revision": REVISION,
        "release_directory": str(directory),
        "release_manifest_sha256": pin,
        "runtime_archive_sha256": value["runtime"]["archive_sha256"],
        "image_approval_sha256": {
            lang: image["approval_sha256"] for lang, image in value["images"].items()
        },
        "machine_id_sha256": "f" * 64,
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "shadow_only": True,
        "weight_eligible": False,
    }
    info = {
        **copy.deepcopy(DAEMON_VECTOR["info"]),
        "OSType": "linux",
        "Architecture": "x86_64",
        "DockerRootDir": str(RELEASE.IMAGE.HOME_DIR / "docker"),
        "Labels": ["io.heyditto.dittobench.isolated=true"],
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
        "Containers": 0,
        "Images": 4,
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
    }
    empty_home = directory / "fake-home"
    (empty_home / "empty-client").mkdir(parents=True)
    identity = SimpleNamespace(pw_uid=10001, pw_gid=10002)
    native = SimpleNamespace(
        identity=lambda: identity,
        private_path=lambda *_args, **_kwargs: None,
        DAEMON_HOME=empty_home,
        SOCKET=Path(HOST.SOCKET),
        TABLE="ditto_coding_hosted",
    )
    calls, installed = [], []
    monkeypatch.setattr(
        HOST, "load", lambda name, _path: RELEASE if name == "host_release" else native
    )
    monkeypatch.setattr(HOST, "protected", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        HOST, "host_binding", lambda _config: {"boot_id": config["boot_id"]}
    )
    monkeypatch.setattr(RELEASE.RUNTIME, "check_base", lambda _manifest: None)
    monkeypatch.setattr(
        RELEASE.RUNTIME, "verify_tree", lambda *args: installed.append(args)
    )

    def command(args, **identity_args):
        calls.append((args, identity_args))
        if args[0] == "/usr/bin/systemctl":
            if "--property=LoadState" in args:
                return b"masked\n"
            return (
                b"active\n"
                if "ditto-coding-hosted-egress.service" in args
                else b"inactive\n"
            )
        if args[0] == "/usr/sbin/nft":
            return json.dumps(
                {
                    "nftables": [
                        {
                            "table": {
                                "family": "inet",
                                "name": "ditto_coding_hosted",
                            }
                        }
                    ]
                }
            ).encode()
        if args[1] == "info":
            return json.dumps(info).encode()
        if args[1] == "ps":
            return b""
        assert args[1:3] == ["image", "inspect"]
        image = next(
            image for image in value["images"].values() if image["image_ref"] == args[3]
        )
        return json.dumps(
            [
                {
                    "RepoDigests": [image["image_ref"]],
                    "Id": image["config_digest"],
                    "Descriptor": {"digest": image["image_ref"].split("@")[1]},
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": configuration(image["driver_profile"]),
                }
            ]
        ).encode()

    monkeypatch.setattr(HOST, "command", command)
    return SimpleNamespace(
        config=config,
        release=value,
        info=info,
        calls=calls,
        installed=installed,
        command=command,
        native=native,
    )


def test_post_import_inspects_all_pins_but_does_not_claim_qualification(context):
    result = HOST.inspect_host(context.config)
    assert result["host_preflight_passed"] is True
    assert result["pending_host_qualification"] == HOST.PENDING
    for field in (
        "runtime_qualification",
        "private_execution_ready",
        "weight_eligible",
    ):
        assert result[field] is False
    assert result["image_approval_sha256"] == context.config["image_approval_sha256"]
    assert context.installed[0][1] == RELEASE.RUNTIME.BASE / REVISION
    assert context.installed[0][2] == context.config["runtime_archive_sha256"]
    docker = [
        (args, identity)
        for args, identity in context.calls
        if args[0].endswith("docker")
    ]
    assert (
        len([args for args, _identity in docker if args[1:3] == ["image", "inspect"]])
        == 4
    )
    assert all(identity == {"uid": 10001, "gid": 10002} for _args, identity in docker)
    assert all(args[1] in {"info", "ps", "image"} for args, _identity in docker)
    assert all(
        args[1] == "show"
        for args, _identity in context.calls
        if args[0].endswith("systemctl")
    )
    assert str(context.config["release_directory"]) not in json.dumps(result)
    assert result["schema"] == "dittobench-coding-native-host-preflight-v4"
    assert result["schema"] == EVIDENCE.PREFLIGHT_SCHEMA
    assert set(result) == EVIDENCE.PREFLIGHT_KEYS
    listing = json.dumps(
        {"nftables": [{"table": {"family": "inet", "name": "ditto_coding_hosted"}}]}
    ).encode()
    assert result["nft_ruleset_semantic_sha256"] == HOST.nft_ruleset_semantic_sha256(
        listing, "ditto_coding_hosted"
    )
    assert result["daemon_identity"] == DAEMON_VECTOR["identity"]
    assert result["daemon_identity_sha256"] == DAEMON_VECTOR["identity_sha256"]


def test_daemon_identity_matches_the_shared_go_vector():
    info, socket = DAEMON_VECTOR["info"], DAEMON_VECTOR["socket_path"]
    identity = HOST.daemon_identity(copy.deepcopy(info), socket)
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    assert raw == DAEMON_VECTOR["identity_canonical"]
    assert HOST.checksum(raw.encode()) == DAEMON_VECTOR["identity_sha256"]
    for key, value in DAEMON_VECTOR["volatile"].items():
        assert HOST.daemon_identity({**info, key: value}, socket) == identity, key
    for key, value in DAEMON_VECTOR["binding"].items():
        assert HOST.daemon_identity({**info, key: value}, socket) != identity, key
    for key, value in DAEMON_VECTOR["refused"].items():
        with pytest.raises(ValueError):
            HOST.daemon_identity({**info, key: value}, socket)
        with pytest.raises(ValueError):
            HOST.daemon_identity({k: v for k, v in info.items() if k != key}, socket)
    for socket in ("", "relative.sock", "//run/docker.sock", "/run/../x.sock"):
        with pytest.raises(ValueError):
            HOST.daemon_identity(copy.deepcopy(info), socket)


@pytest.mark.parametrize(
    "field",
    [
        "release_manifest_sha256",
        "runtime_archive_sha256",
        "source_revision",
        "image_approval_sha256",
    ],
)
def test_approval_mismatch_refused_before_daemon_inspection(context, field):
    config = copy.deepcopy(context.config)
    if field == "image_approval_sha256":
        config[field]["rust"] = "0" * 64
    else:
        config[field] = "0" * (40 if field == "source_revision" else 64)
    with pytest.raises(ValueError):
        HOST.inspect_host(config)
    assert not context.calls and not context.installed


@pytest.mark.parametrize(
    "field,value",
    [
        ("Containers", 1),
        ("CgroupVersion", "1"),
        ("MemoryLimit", False),
        ("SecurityOptions", []),
        ("DriverStatus", []),
        ("Labels", []),
    ],
)
def test_daemon_mismatch_refused(context, field, value):
    context.info[field] = value
    with pytest.raises(ValueError):
        HOST.inspect_host(context.config)


@pytest.mark.parametrize(
    "fault",
    [
        "worker-active",
        "rootful-active",
        "container",
        "image",
        "daemon-drift",
        "daemon-version-drift",
        "nft-table",
        "boot-drift",
    ],
)
def test_live_snapshot_faults_never_produce_success(context, monkeypatch, fault):
    seen = 0

    def command(args, **kwargs):
        nonlocal seen
        raw = context.command(args, **kwargs)
        if fault == "worker-active" and "ditto-coding-hosted-worker.service" in args:
            return b"active\n"
        if (
            fault == "rootful-active"
            and "docker.service" in args
            and "--property=ActiveState" in args
        ):
            return b"active\n"
        if fault == "container" and args[1] == "ps":
            return b"retained-container\n"
        if fault == "image" and args[1:3] == ["image", "inspect"]:
            return b"[]"
        if fault == "nft-table" and args[0].endswith("nft"):
            return b'{"nftables":[{"table":{"family":"inet","name":"unrelated"}}]}'
        if fault == "daemon-drift" and args[1] == "info":
            seen += 1
            if seen == 2:
                return json.dumps({**context.info, "ID": "changed"}).encode()
        if fault == "daemon-version-drift" and args[1] == "info":
            seen += 1
            if seen == 2:
                return json.dumps({**context.info, "ServerVersion": "29.9.9"}).encode()
        return raw

    monkeypatch.setattr(HOST, "command", command)
    if fault == "boot-drift":

        def host(_config):
            nonlocal seen
            seen += 1
            return {"boot_id": str(seen)}

        monkeypatch.setattr(HOST, "host_binding", host)
    with pytest.raises(ValueError):
        HOST.inspect_host(context.config)


def test_duplicate_keys_and_known_field_types_are_strict(context):
    with pytest.raises(ValueError):
        HOST.object_json(b'{"schema":1,"schema":2}')
    for field, value in [
        ("shadow_only", 1),
        ("weight_eligible", 0),
        ("boot_id", []),
        ("release_directory", "relative"),
        ("image_approval_sha256", {}),
    ]:
        with pytest.raises(ValueError):
            HOST.config_policy({**context.config, field: value})
    assert (
        HOST.config_policy({**context.config, "command": "ignore-me"}) == context.config
    )


def test_cli_refuses_nonroot_without_accessing_any_host():
    if os.geteuid() == 0:
        pytest.skip("non-root refusal test")
    result = subprocess.run(
        ["python3", "-B", "-I", str(SCRIPT), "--config", "/not-a-real-private-config"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and not result.stdout
    assert "private-config" not in result.stderr and "Traceback" not in result.stderr


def test_protected_paths_reject_nonroot_owner_and_symlinks(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"public")
    if os.geteuid() != 0:
        with pytest.raises(ValueError):
            HOST.protected(path)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ValueError):
        HOST.protected(alias)


def test_inspection_disables_python_bytecode_writes():
    assert HOST.sys.dont_write_bytecode is True


def test_command_clears_environment_and_drops_docker_identity(monkeypatch):
    real_popen = subprocess.Popen
    observed = []
    monkeypatch.setattr(HOST, "protected", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("PREFLIGHT_TEST_PRIVATE_TOKEN", "must-not-inherit")

    def popen(args, **kwargs):
        observed.append((args, dict(kwargs)))
        # Exercise bounded pipes with a synthetic child, not Docker or root.
        for key in ("user", "group", "extra_groups"):
            kwargs.pop(key)
        return real_popen([HOST.sys.executable, "-c", "print('public')"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert (
        HOST.command(["/usr/bin/docker", "info"], uid=10001, gid=10002) == b"public\n"
    )
    _args, kwargs = observed[0]
    assert kwargs["user"] == 10001 and kwargs["group"] == 10002
    assert kwargs["extra_groups"] == []
    assert set(kwargs["env"]) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
    }
    assert kwargs["env"]["DOCKER_HOST"] == "unix:///run/ditto-coding-hosted/docker.sock"
    assert kwargs["cwd"] == "/" and kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("fault", ["oversized", "nonzero", "timeout"])
def test_inspection_subprocess_is_bounded_and_reaped(monkeypatch, fault):
    real_popen = subprocess.Popen
    children = []
    monkeypatch.setattr(HOST, "protected", lambda *_args, **_kwargs: None)
    code = {
        "oversized": "import os; os.write(1,b'x' * (2 << 20))",
        "nonzero": "raise SystemExit(1)",
        "timeout": "import time; time.sleep(60)",
    }[fault]

    def popen(_args, **kwargs):
        child = real_popen([HOST.sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)
    if fault == "timeout":
        ticks = iter([0, 31])
        monkeypatch.setattr(HOST.time, "monotonic", lambda: next(ticks, 31))
    with pytest.raises(ValueError):
        HOST.command(["/usr/bin/systemctl", "show", "synthetic"])
    assert all(child.poll() is not None for child in children)


def test_host_binding_refuses_wrong_host_platform_machine_and_boot(
    context, monkeypatch
):
    # Restore the real binding function, which context normally replaces.
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    machine = b"a" * 32
    config = {**context.config, "machine_id_sha256": HOST.checksum(machine)}
    monkeypatch.setattr(real.os, "geteuid", lambda: 0)
    monkeypatch.setattr(real.platform, "node", lambda: "ditto-coding-hosted-v2")
    monkeypatch.setattr(real.platform, "system", lambda: "Linux")
    monkeypatch.setattr(real.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        real.platform,
        "freedesktop_os_release",
        lambda: {"ID": "debian", "VERSION_ID": "13"},
    )
    monkeypatch.setattr(Path, "read_bytes", lambda _self: machine + b"\n")
    monkeypatch.setattr(Path, "read_text", lambda _self: config["boot_id"])
    assert real.host_binding(config)["machine_id_sha256"] == config["machine_id_sha256"]
    for changed in ({"machine_id_sha256": "0" * 64}, {"boot_id": "0" * 36}):
        with pytest.raises(ValueError):
            real.host_binding({**config, **changed})
    monkeypatch.setattr(real.platform, "node", lambda: "another-host")
    with pytest.raises(ValueError):
        real.host_binding(config)


# ---------------------------------------------------------------------------
# Semantic nft ruleset digest (preflight v4) on recorded kernel listings


def nft_fixture(name):
    return (NFT_FIXTURES / name).read_bytes()


def nft_digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HOST.nft_ruleset_semantic_sha256(raw, TABLE)


def nft_listing(name):
    return json.loads(nft_fixture(name))


def entries_of(listing, kind):
    return [entry[kind] for entry in listing["nftables"] if kind in entry]


def one(listing, kind, name):
    (body,) = [
        body
        for body in entries_of(listing, kind)
        if body.get("name", body.get("chain")) == name
    ]
    return body


def deny_rule(listing):
    (rule,) = [
        rule for rule in entries_of(listing, "rule") if rule["chain"] == "output"
    ]
    return rule


def append(listing, kind, body):
    listing["nftables"].append({kind: body})
    return listing


def chain(name, **base):
    return {"family": "inet", "table": TABLE, "name": name, "handle": 90, **base}


def empty_set(name, **extra):
    return {
        "family": "inet",
        "name": name,
        "table": TABLE,
        "type": "ipv4_addr",
        "handle": 91,
        **extra,
    }


def rule(chain_name, expr):
    return {"family": "inet", "table": TABLE, "chain": chain_name, "expr": expr}


ACCEPT = [
    {"match": {"op": "==", "left": {"meta": {"key": "skuid"}}, "right": 1001}},
    {"counter": {"packets": 0, "bytes": 0}},
    {"accept": None},
]


def test_semantic_digest_is_stable_across_a_recorded_worker_cycle():
    initial = nft_fixture("nft-deny-initial.json")
    # Deny guard, scoped policy, deny guard, element expiry; recorded in a
    # throwaway namespace. Handles differ and the scoped base chains and sets
    # survive the flush, so the raw listings differ.
    after = nft_fixture("nft-deny-after-expiry.json")
    assert initial != after
    assert HOST.checksum(initial) != HOST.checksum(after)
    assert nft_digest(initial) == nft_digest(after)
    # Before expiry the unreferenced sets still hold elements; they are kept,
    # so a preflight taken then (the verifier requires it after the expiry
    # phase) refuses rather than passes.
    assert nft_digest(nft_fixture("nft-deny-after-scoped.json")) != nft_digest(initial)
    assert nft_digest(nft_fixture("nft-scoped.json")) != nft_digest(initial)


def test_semantic_digest_ignores_only_non_semantic_listing_data():
    base = nft_listing("nft-deny-after-expiry.json")
    expected = nft_digest(base)

    def same(change, name="nft-deny-after-expiry.json"):
        listing = nft_listing(name)
        change(listing)
        assert nft_digest(listing) == nft_digest(nft_listing(name))

    same(lambda v: v["nftables"].pop(0))  # metainfo
    same(lambda v: deny_rule(v).update(handle=7))
    same(lambda v: one(v, "table", TABLE).update(handle=9))
    same(lambda v: deny_rule(v)["expr"][1]["counter"].update(packets=5, bytes=900))
    same(lambda v: v["nftables"].reverse())
    same(lambda v: append(v, "chain", chain("unused")))
    same(lambda v: append(v, "set", empty_set("unused")))
    same(
        lambda v: one(v, "set", "worker")["elem"][0]["elem"].update(
            timeout=60, expires=1
        ),
        "nft-scoped.json",
    )
    same(
        lambda v: one(v, "set", "lease")["elem"][0]["elem"].pop("expires"),
        "nft-scoped.json",
    )
    assert nft_digest(base) == expected


def test_semantic_digest_changes_on_any_enforcement_difference():
    initial = nft_digest(nft_fixture("nft-deny-after-expiry.json"))
    scoped = nft_digest(nft_fixture("nft-scoped.json"))

    def differs(change, name="nft-deny-after-expiry.json", reference=None):
        listing = nft_listing(name)
        change(listing)
        assert nft_digest(listing) != (reference or nft_digest(nft_fixture(name)))

    # A changed rule, verdict or rule order.
    differs(lambda v: deny_rule(v)["expr"][0]["match"].update(right=1002))
    differs(lambda v: deny_rule(v)["expr"].__setitem__(2, {"drop": None}))
    differs(
        lambda v: v["nftables"].__setitem__(
            slice(None),
            [e for e in v["nftables"] if "rule" not in e]
            + list(reversed([e for e in v["nftables"] if "rule" in e])),
        ),
        "nft-scoped.json",
    )
    # A changed base-chain policy, with and without rules.
    differs(lambda v: one(v, "chain", "output").update(policy="drop"))
    differs(lambda v: one(v, "chain", "scoped_input").update(policy="drop"))
    differs(lambda v: one(v, "chain", "scoped_input").update(prio=-400, type="nat"))
    differs(lambda v: one(v, "chain", "scoped_output").update(dev="lo"))
    # A new element in a referenced set, or a changed element.
    differs(
        lambda v: one(v, "set", "lease")["elem"].append(
            {"elem": {"val": 1002, "timeout": 240, "expires": 239}}
        ),
        "nft-scoped.json",
    )
    differs(
        lambda v: one(v, "set", "worker")["elem"][0]["elem"].update(
            val="system.slice/other.service"
        ),
        "nft-scoped.json",
    )
    # A removed deny rule, or an added accept rule in any chain.
    differs(lambda v: v["nftables"].remove({"rule": deny_rule(v)}))
    differs(lambda v: append(v, "rule", rule("output", ACCEPT)))
    differs(lambda v: append(v, "rule", rule("scoped_output", ACCEPT)))
    differs(
        lambda v: v["nftables"].insert(
            [i for i, e in enumerate(v["nftables"]) if "rule" in e][0],
            {"rule": rule("output", ACCEPT)},
        )
    )
    # A new drop base chain, even with no rules.
    differs(
        lambda v: append(
            v,
            "chain",
            chain("late", type="filter", hook="input", prio=0, policy="drop"),
        )
    )
    assert scoped != initial


def test_referenced_empty_chains_and_sets_are_kept():
    def with_reference(expr_target, objects):
        listing = nft_listing("nft-deny-after-expiry.json")
        for kind, body in objects:
            append(listing, kind, body)
        append(listing, "rule", rule("output", expr_target))
        return listing

    lookup = [
        {"match": {"op": "==", "left": {"meta": {"key": "skuid"}}, "right": "@spare"}},
        {"accept": None},
    ]
    referenced = with_reference(lookup, [("set", empty_set("spare", type="uid"))])
    entries = HOST.nft_semantic_entries(HOST.nft_entries(referenced))
    assert {"set": HOST.nft_strip(empty_set("spare", type="uid"))} in entries
    other = with_reference(
        lookup, [("set", empty_set("spare", type="uid", flags=["interval"]))]
    )
    assert nft_digest(referenced) != nft_digest(other)
    missing = with_reference(lookup, [])
    assert nft_digest(referenced) != nft_digest(missing)

    jump = [{"jump": {"target": "sub"}}]
    jumped = with_reference(jump, [("chain", chain("sub"))])
    entries = HOST.nft_semantic_entries(HOST.nft_entries(jumped))
    assert {"chain": HOST.nft_strip(chain("sub"))} in entries
    assert nft_digest(jumped) != nft_digest(with_reference(jump, []))
    # A set that a map's verdict element names keeps its target chain too.
    vmap = with_reference(
        [{"vmap": {"key": {"meta": {"key": "skuid"}}, "data": "@verdicts"}}],
        [
            ("chain", chain("sub")),
            (
                "map",
                {
                    **empty_set("verdicts", type="uid", map="verdict"),
                    "elem": [[1001, {"goto": {"target": "sub"}}]],
                },
            ),
        ],
    )
    entries = HOST.nft_semantic_entries(HOST.nft_entries(vmap))
    assert {"chain": HOST.nft_strip(chain("sub"))} in entries


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"nftables": {}}',
        b'{"nftables": [], "extra": 1}',
        b'{"nftables": [{"table": {"family": "inet", "name": "x"}}]}',
        b'{"nftables": [{"table": {"family": "ip", "name": "ditto_coding_hosted"}}]}',
        b'{"nftables": []}',
        b'{"nftables": [{"table": 1}]}',
        b'{"nftables": [{"table": {}, "chain": {}}]}',
        b'{"nftables": [{"set": {"elem": {}}}, '
        b'{"table": {"family": "inet", "name": "ditto_coding_hosted"}}]}',
        b'{"nftables": [{"table": {"family": "inet", "name": "ditto_coding_hosted"}},'
        b' {"table": {"family": "inet", "name": "ditto_coding_hosted"}}]}',
        b'{"nftables": [{"table": {"family": "inet", "name": "ditto_coding_hosted",'
        b' "name": "x"}}]}',
    ],
)
def test_semantic_digest_refuses_malformed_listings(raw):
    with pytest.raises(ValueError):
        HOST.nft_ruleset_semantic_sha256(raw, TABLE)
