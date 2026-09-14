"""Guard: every layer's bench-version pin agrees with the shared derivation.

A ``bench_version`` is an immutable cross-layer contract, and every bump so far
stranded the new version at exactly one layer that kept an old pin: v10 ran
ungated on ``== 9`` checks, v11 shipped with the validator's own
``SUPPORTED_BENCH_VERSIONS`` lagging the scorer (zero capable validators), v12
was selected for confirmation and then rejected at the transport's
``Literal[9]``. The rule is floors and one derived constant, never retyped
enumerations -- but Go, Rust and TypeScript cannot import the Python constant,
so this test diffs their pins against the committed ``bench_versions.json``
golden (written by ``scripts/gen_validator_contract.py`` from
``ditto_screening_protocol.bench_v9``, the one hand-typed alias).

It deliberately reads source text, not build output: the point is to fail on
the pull request that forgets a layer, before anything is built or deployed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ditto.validator.dittobench import SUPPORTED_BENCH_VERSIONS
from ditto.validator.weights import RECEIPT_CONTRACT_VERSIONS
from ditto_screening_protocol import bench_v9

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "ditto/tests/contract/bench_versions.json"

DATAGEN_EPOCH = ROOT / "research/dittobench-datagen/protocol/epoch.go"
SCOREGATES = ROOT / "services/dittobench-api/internal/scoregates/scoregates.go"
EFFICIENCY = ROOT / "services/dittobench-api/internal/efficiency/efficiency.go"
SCORER_MAIN = ROOT / "services/dittobench-api/cmd/dittobench-api/main.go"
CONFIRMATION_EXECUTOR = (
    ROOT / "services/dittobench-api/cmd/dittobench-api/confirmation_executor.go"
)
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
PLATFORM_CONTRACTS = ROOT / "apps/platform/ditto/api_models/benchmark_contract.py"
STARTER_KIT_PROTOCOL = ROOT / "miners/dittobench-starter-kit/src/protocol.rs"
LOCAL_REHEARSAL = ROOT / "miners/dittobench-starter-kit/scripts/local-rehearsal.py"
BACKROOM_SCHEMAS = ROOT / "apps/backroom/src/lib/admin.schemas.ts"
BACKROOM_GENERATED = ROOT / "apps/backroom/src/generated/platform-api.ts"
DASHBOARD_TYPES = (
    ROOT / "apps/platform/dashboard/src/types/leaderboard.ts",
    ROOT / "apps/platform/dashboard/src/types/fleet.ts",
)


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def _max_go_version_constant(source: str) -> int:
    """Highest ``BenchVersionVN = N`` constant declared in a Go file."""
    pairs = re.findall(r"\bBenchVersionV(\d+)\s*=\s*(\d+)\b", source)
    assert pairs, "no BenchVersionVN constants found"
    for suffix, value in pairs:
        assert suffix == value, f"BenchVersionV{suffix} is defined as {value}"
    return max(int(value) for _, value in pairs)


def _int_set(text: str) -> set[int]:
    return {int(v) for v in re.findall(r"\d+", text)}


# --- the golden itself -------------------------------------------------------


def test_golden_is_the_shared_alias_serialized() -> None:
    golden = _golden()
    assert golden["confirmation_bench_versions"] == list(
        bench_v9.CONFIRMATION_BENCH_VERSIONS
    )
    assert golden["supported_bench_versions"] == list(bench_v9.SUPPORTED_BENCH_VERSIONS)
    assert golden["max_supported_bench_version"] == bench_v9.MAX_SUPPORTED_BENCH_VERSION
    assert (
        golden["min_executable_bench_version"] == bench_v9.MIN_EXECUTABLE_BENCH_VERSION
    )


def test_supported_set_is_contiguous_and_contains_every_confirmable_epoch() -> None:
    golden = _golden()
    supported = golden["supported_bench_versions"]
    lo, hi = (
        golden["min_executable_bench_version"],
        golden["max_supported_bench_version"],
    )
    assert supported == list(range(lo, hi + 1))
    confirmable = golden["confirmation_bench_versions"]
    assert confirmable == list(range(min(confirmable), hi + 1))
    assert set(confirmable) <= set(supported)


# --- Python layers (imported, so a retyped copy cannot hide) ------------------


def test_validator_executable_set_is_the_shared_derivation() -> None:
    assert SUPPORTED_BENCH_VERSIONS is bench_v9.SUPPORTED_BENCH_VERSIONS
    assert list(SUPPORTED_BENCH_VERSIONS) == _golden()["supported_bench_versions"]


def test_weight_fold_receipt_set_is_the_confirmation_lane_set() -> None:
    assert set(_golden()["confirmation_bench_versions"]) == RECEIPT_CONTRACT_VERSIONS


def test_platform_ships_a_contract_for_exactly_the_supported_ceiling() -> None:
    source = PLATFORM_CONTRACTS.read_text()
    shipped = {
        int(v) for v in re.findall(r"^\s*(\d+): BenchmarkContract\(", source, re.M)
    }
    golden = _golden()
    assert max(shipped) == golden["max_supported_bench_version"], (
        "apps/platform benchmark_contract.py does not ship the newest supported "
        "version; the Platform cannot count capable validators for it"
    )
    assert set(golden["supported_bench_versions"]) <= shipped


# --- Go layers (datagen + scorer) ---------------------------------------------


def test_datagen_protocol_declares_the_supported_ceiling() -> None:
    source = DATAGEN_EPOCH.read_text()
    ceiling = _golden()["max_supported_bench_version"]
    assert _max_go_version_constant(source) == ceiling, (
        "research/dittobench-datagen/protocol/epoch.go lags the shared ceiling; "
        "the generator cannot reproduce the newest supported version"
    )
    assert f"datasetEpochV{ceiling}" in source, "newest version has no dataset epoch"


def test_scoregates_upper_bound_is_the_supported_ceiling() -> None:
    source = SCOREGATES.read_text()
    ceiling = _golden()["max_supported_bench_version"]
    assert _max_go_version_constant(source) == ceiling
    bound = re.search(r"benchVersion\s*<=\s*BenchVersionV(\d+)", source)
    assert bound is not None, "scoregates.SupportedBenchVersion has no <= upper bound"
    assert int(bound.group(1)) == ceiling, (
        "scoregates.SupportedBenchVersion caps below the shared ceiling; the "
        "scorer would silently reject the newest version's evidence"
    )


def test_efficiency_readiness_lists_the_supported_ceiling() -> None:
    source = EFFICIENCY.read_text()
    ceiling = _golden()["max_supported_bench_version"]
    start = source.index("func ProductionReadyForVersion(")
    body = source[start : source.index("\n}\n", start)]
    assert f"protocol.BenchVersionV{ceiling}" in body, (
        "efficiency.ProductionReadyForVersion does not list the newest supported "
        "version, so the scorer would never advertise it"
    )


def _scorer_advertised_versions() -> set[int]:
    source = SCORER_MAIN.read_text()
    start = source.index("func supportedBenchVersions(")
    body = source[start : source.index("\n}\n", start)]
    literal = re.search(r"\[\]int\{([^}]*)\}", body)
    assert literal is not None, "supportedBenchVersions() has no []int literal"
    return {int(v) for v in re.findall(r"BenchVersionV(\d+)", literal.group(1))}


def test_scorer_advertises_within_the_validator_executable_set() -> None:
    """The scorer may lag the ceiling while a contract is unfinished; it must
    never advertise a version the validator's executable set omits -- that is
    the exact shape of the v11 zero-capable-validators outage."""
    advertised = _scorer_advertised_versions()
    golden = _golden()
    assert advertised, "scorer advertises nothing"
    assert min(advertised) == golden["min_executable_bench_version"]
    assert advertised <= set(golden["supported_bench_versions"]), (
        f"scorer advertises {sorted(advertised)} but the validator executes only "
        f"{golden['supported_bench_versions']}"
    )


def test_release_identity_gate_matches_the_scorer_advertisement() -> None:
    """The deploy identity gate fails closed on the advertised set; it must move
    with cmd/dittobench-api, not be bumped from memory."""
    gate = re.search(
        r"\.supported_bench_versions \| sort == \[([\d, ]+)\]",
        RELEASE_WORKFLOW.read_text(),
    )
    assert gate is not None, "release.yml identity gate not found"
    assert _int_set(gate.group(1)) == _scorer_advertised_versions()


def test_scorer_error_strings_name_the_advertised_set() -> None:
    """``requestedBenchVersion`` hard-codes the advertised set into its two
    ``(supported: ...)`` error strings; a bump that grows ``supportedBenchVersions``
    and forgets them tells a validator the wrong negotiation set."""
    source = SCORER_MAIN.read_text()
    start = source.index("func requestedBenchVersion(")
    body = source[start : source.index("\n}\n", start)]
    strings = re.findall(r"\(supported: ([\d, ]+)\)", body)
    assert len(strings) == 2, "requestedBenchVersion lost a (supported: ...) string"
    for text in strings:
        assert _int_set(text) == _scorer_advertised_versions(), (
            f"main.go error string names {text!r} but the scorer advertises "
            f"{sorted(_scorer_advertised_versions())}"
        )


def test_confirmation_subject_epoch_is_a_floor_over_the_scorer_ceiling() -> None:
    """The Go confirmation lane's bundle/job allow-list must accept every epoch
    Python ``supports_confirmation`` can issue, or a v13 bundle is selected on
    the Platform and rejected at ``running_confirmation`` -- the v12 strand.

    The allow-list is a floor (``>= V9 && scoregates.SupportedBenchVersion``),
    never a ``case 9, 10, 11, 12:`` enumeration. ``ablation.
    ConfirmationBenchVersionSupported`` ({9, 12}) is deliberately NOT checked
    here: it is the installed-instrument list (which profiles are shipped), a
    legitimately enumerated depth upgrade, not the subject-epoch allow-list.
    """
    source = CONFIRMATION_EXECUTOR.read_text()
    start = source.index("func confirmationSubjectEpochSupported(")
    body = source[start : source.index("\n}\n", start)]
    assert re.search(r"\bcase\s+\d+(?:\s*,\s*\d+)*\s*:", body) is None, (
        "confirmationSubjectEpochSupported enumerates subject epochs; it must be "
        "a floor so the newest confirmable version is not stranded"
    )
    assert "scoregates.SupportedBenchVersion(benchVersion)" in body
    assert re.search(r"benchVersion\s*>=\s*ablation\.BenchVersionV9", body), (
        "confirmation subject floor is not v9"
    )
    golden = _golden()
    confirmable = golden["confirmation_bench_versions"]
    # The floor's upper edge is the scorer ceiling; the golden's confirmable set
    # must sit inside [9, ceiling] or Platform issues what Go refuses.
    assert min(confirmable) == 9
    assert max(confirmable) == _max_go_version_constant(SCOREGATES.read_text())


# --- starter kit (Rust + rehearsal script) ------------------------------------


def test_starter_kit_accepts_exactly_the_supported_range() -> None:
    source = STARTER_KIT_PROTOCOL.read_text()
    golden = _golden()
    ceiling = re.search(r"MAX_SUPPORTED_BENCH_VERSION: u32 = (\d+);", source)
    floor = re.search(r"MIN_SUPPORTED_BENCH_VERSION: u32 = (\d+);", source)
    assert ceiling and floor
    assert int(ceiling.group(1)) == golden["max_supported_bench_version"], (
        "starter kit protocol.rs would 400 /run for the newest supported version"
    )
    assert int(floor.group(1)) == golden["min_executable_bench_version"]


def test_local_rehearsal_range_tracks_the_supported_set() -> None:
    source = LOCAL_REHEARSAL.read_text()
    golden = _golden()
    values = {
        name: int(re.search(rf"^{name} = (\d+)$", source, re.M).group(1))  # type: ignore[union-attr]
        for name in (
            "LIVE_SCORING_BENCH_VERSION",
            "MIN_BENCH_VERSION",
            "MAX_BENCH_VERSION",
        )
    }
    assert values["MAX_BENCH_VERSION"] == golden["max_supported_bench_version"]
    assert values["MIN_BENCH_VERSION"] == golden["min_executable_bench_version"]
    assert values["LIVE_SCORING_BENCH_VERSION"] in golden["supported_bench_versions"]


# --- TypeScript mirrors (hand-written and generated) --------------------------


def test_backroom_confirmation_schema_enumerates_the_confirmable_epochs() -> None:
    source = BACKROOM_SCHEMAS.read_text()
    start = source.index("export const confirmationBenchVersionSchema = z.union([")
    body = source[start : source.index("])", start)]
    literals = {int(v) for v in re.findall(r"z\.literal\((\d+)\)", body)}
    assert literals == set(_golden()["confirmation_bench_versions"])


def test_generated_backroom_client_carries_every_confirmable_epoch() -> None:
    unions = re.findall(
        r"bench_version: ((?:\d+ \| )+\d+);", BACKROOM_GENERATED.read_text()
    )
    assert unions, "generated client has no bench_version unions; regenerate it"
    expected = set(_golden()["confirmation_bench_versions"])
    for union in unions:
        assert _int_set(union) == expected, (
            f"platform-api.ts union {union!r} is stale; run "
            "apps/backroom/scripts/platform-contract/generate.sh"
        )


def test_dashboard_types_mirror_the_confirmable_epochs() -> None:
    expected = set(_golden()["confirmation_bench_versions"])
    for path in DASHBOARD_TYPES:
        unions = re.findall(r"bench_version: ((?:\d+ \| )+\d+);", path.read_text())
        assert unions, f"{path.name} has no bench_version union"
        for union in unions:
            assert _int_set(union) == expected, f"{path.name}: {union!r} is stale"
