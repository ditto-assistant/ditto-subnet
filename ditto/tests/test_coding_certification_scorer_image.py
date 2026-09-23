"""Static checks that the scorer image carries the certification pack.

The default-off certification canary loads a repo-shaped root from the sandbox
scorer image. Pack integrity is enforced once, by the Go loader
(``codingcanary.LoadPublicPack``), against the manifest Platform binds into
every lease. These checks bind the Dockerfile's copy, the build-context
exclusion rule, and the runtime path to the committed files without building
an image, and keep a second hand-maintained digest list from reappearing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "services/dittobench-api/Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
PACK_LOADER = ROOT / "services/dittobench-api/internal/codingcanary/pack.go"

PACK_DIR = "research/dittobench-coding-datagen/certification/v1"
POLICY = (
    "packages/dittobench-coding-contract/testdata/"
    "coding_inference_policy_locked_v1.json"
)
STAGE_ROOT = "/pack/certification-root"
IMAGE_ROOT = "/opt/ditto/coding/certification-root"
# The identity Platform binds into every certification lease and the Go loader
# test pins; see services/dittobench-api/internal/codingcanary/pack_test.go.
MANIFEST_SHA256 = "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903"
POLICY_FILE_SHA256 = "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
# The build context and the loader share one exclusion rule
# (codingcanary.ignoredPackEntry).
IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__"})
IGNORED_FILE_SUFFIXES = (".pyc",)
IGNORED_FILE_NAMES = frozenset({".DS_Store"})


def _instructions() -> list[str]:
    logical: list[str] = []
    current = ""
    for line in DOCKERFILE.read_text().splitlines():
        if not current and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        if current.strip():
            logical.append(" ".join(current.split()))
        current = ""
    assert not current
    return logical


def _stages() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    name = ""
    for instruction in _instructions():
        if instruction.startswith("FROM "):
            name = instruction.rsplit(" AS ", 1)[1]
            assert name not in stages
            stages[name] = []
        if name:
            stages[name].append(instruction)
    return stages


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts[:-1])
        or relative.name in IGNORED_FILE_NAMES
        or relative.name.endswith(IGNORED_FILE_SUFFIXES)
        or (path.is_dir() and relative.name in IGNORED_DIRECTORY_NAMES)
    )


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not _ignored(path, root)
    }


def _listing_sha256(tree: dict[str, str]) -> str:
    listing = "".join(f"{tree[path]}  {path}\n" for path in sorted(tree))
    return hashlib.sha256(listing.encode()).hexdigest()


def test_pack_stage_copies_only_the_certification_capsule_and_locked_policy() -> None:
    stage = _stages()["coding-certification-pack"]
    assert stage[0] == "FROM alpine:3.22 AS coding-certification-pack"
    copies = [item for item in stage if item.startswith(("COPY ", "ADD "))]
    assert copies == [
        f"COPY {PACK_DIR}/ {STAGE_ROOT}/{PACK_DIR}/",
        f"COPY {POLICY} {STAGE_ROOT}/{POLICY}",
    ]


def test_pack_stage_is_read_only_and_cannot_fail_ordinary_scorer_builds() -> None:
    stage = _stages()["coding-certification-pack"]
    (run,) = [item for item in stage if item.startswith("RUN ")]
    assert run == (
        "RUN find . -type d -exec chmod 0555 {} + && "
        "find . -type f -exec chmod 0444 {} +"
    )
    assert stage.index(run) == len(stage) - 1
    # Integrity lives in the loader. A duplicated digest list here would drift
    # and turn a stray local file into a failed scorer build.
    text = DOCKERFILE.read_text()
    pack_stage = text.split(" AS coding-certification-pack\n", 1)[1].split(
        "\nFROM ", 1
    )[0]
    for forbidden in ("sha256sum", "diff ", "test -z"):
        assert forbidden not in pack_stage
    assert MANIFEST_SHA256 not in text


def test_build_context_and_loader_share_one_exclusion_rule() -> None:
    patterns = {line.strip() for line in DOCKERIGNORE.read_text().splitlines()}
    assert {"**/__pycache__", "**/*.pyc", "**/.DS_Store"} <= patterns
    loader = PACK_LOADER.read_text()
    assert 'return name == "__pycache__"' in loader
    assert 'return name == ".DS_Store" || strings.HasSuffix(name, ".pyc")' in loader


def test_loader_pins_bind_every_committed_capsule_file() -> None:
    pack = ROOT / PACK_DIR
    manifest_body = (pack / "manifest.json").read_bytes()
    manifest = json.loads(manifest_body)
    assert hashlib.sha256(manifest_body).hexdigest() == MANIFEST_SHA256
    assert manifest["inference_policy"] == {
        "path": POLICY,
        "sha256": POLICY_FILE_SHA256,
    }
    assert _sha256(ROOT / POLICY) == POLICY_FILE_SHA256

    task = manifest["grader_plan"]["task_id"]
    capsule = pack / "capsules" / task
    grader = _tree(capsule / "grader")
    assert grader == {
        item["path"]: item["sha256"] for item in manifest["grader_plan"]["grader_files"]
    }
    for item in manifest["grader_plan"]["grader_files"]:
        assert (capsule / "grader" / item["path"]).stat().st_size == item["size_bytes"]

    visible = _tree(capsule / "visible" / "workspace")
    digest = _listing_sha256(visible)
    assert f'publicCanaryVisibleWorkspaceSHA256 = "{digest}"' in PACK_LOADER.read_text()
    # Nothing else under certification/v1 carries execution bytes.
    everything = _tree(pack)
    assert set(everything) == {"manifest.json"} | {
        f"capsules/{task}/grader/{path}" for path in grader
    } | {f"capsules/{task}/visible/workspace/{path}" for path in visible}


def test_exclusion_rule_ignores_cache_junk_only(tmp_path: Path) -> None:
    (tmp_path / "tests" / "__pycache__").mkdir(parents=True)
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_app.py").write_text("pass\n")
    (tmp_path / "tests" / "__pycache__" / "test_app.cpython-312.pyc").write_bytes(b"j")
    (tmp_path / "app.pyc").write_bytes(b"j")
    (tmp_path / ".DS_Store").write_bytes(b"j")
    assert set(_tree(tmp_path)) == {"app.py", "tests/test_app.py"}


def test_only_the_sandbox_scorer_carries_the_pack_at_a_traversable_fixed_root() -> None:
    stages = _stages()
    carriers = {
        name
        for name, stage in stages.items()
        if any("--from=coding-certification-pack" in item for item in stage)
    }
    assert carriers == {"sandbox"}
    sandbox = stages["sandbox"]
    copy = "COPY --from=coding-certification-pack /pack/ /opt/ditto/coding/"
    assert sandbox.count(copy) == 1
    # BuildKit gives the implicitly created /opt/ditto/coding the policy COPY's
    # --chmod=0444, which the non-root scorer cannot traverse.
    policy_copy = (
        f"COPY --chown=65532:65532 --chmod=0444 {POLICY} "
        "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
    )
    chmod = "RUN chmod 0555 /opt/ditto/coding"
    user = "USER 65532:65532"
    assert sandbox.index(policy_copy) < sandbox.index(copy) < sandbox.index(chmod)
    assert sandbox.index(chmod) < sandbox.index(user)
    # /pack/certification-root lands at the fixed runtime root.
    assert STAGE_ROOT.removeprefix("/pack/") == IMAGE_ROOT.removeprefix(
        "/opt/ditto/coding/"
    )


def test_compose_points_the_gated_canary_at_the_baked_root_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    scorer = compose["services"]["dittobench-api"]
    assert scorer["build"]["target"] == "sandbox"
    environment = scorer["environment"]
    assert environment["DITTOBENCH_CODING_CANARY_ENABLED"] == (
        "${DITTOBENCH_CODING_CANARY_ENABLED:-false}"
    )
    assert environment["DITTOBENCH_CODING_CERTIFICATION_ROOT"] == IMAGE_ROOT
    assert scorer["read_only"] is True
    for volume in scorer.get("volumes", []):
        target = volume.split(":")[1] if isinstance(volume, str) else volume["target"]
        assert not target.startswith("/opt/ditto")
