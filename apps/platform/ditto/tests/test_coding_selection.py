"""Tests for the shadow private coding-catalog selector."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)
from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    coding_catalog_commitment_digest,
)
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogManifestTask,
    CodingCatalogMembershipProof,
    CodingCatalogRuntimePolicy,
    CodingCatalogTaskPayload,
    CodingCatalogTaskVersion,
    CodingSelectionAssignment,
    CodingSelectionRunManifest,
    bind_coding_selection_assignment,
    coding_catalog_budgets_digest,
    coding_catalog_issue_digest,
    coding_catalog_membership_proof_digest,
    coding_catalog_runtime_policy_digest,
    coding_catalog_task_commitment_digest,
    coding_selection_assignment_digest,
    coding_selection_run_manifest_digest,
)
from ditto.coding_selection import (
    CodingSelectionAuthorityError,
    CodingSelectionCatalogError,
    CodingSelectionCatalogUnavailableError,
    CodingSelectionChainError,
    CodingSelectionChainUnavailableError,
    CodingSelectionExhaustedError,
    coding_catalog_empty_leaf_hash,
    coding_catalog_leaf_hash,
    coding_catalog_node_hash,
    coding_selection_catalog_index,
    coding_selection_seed_sha256,
    normalize_coding_block_hash,
    select_shadow_coding_run,
    verify_coding_catalog_membership,
    verify_coding_selection_proof,
)

_MAX_JSON_BYTES = 4 << 20
_CURATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT_ID = UUID("00000000-0000-4000-8000-000000000001")


class FakeBlocks:
    def __init__(self, *, genesis: str, selected: str) -> None:
        self._genesis = genesis
        self._selected = selected
        self.calls: list[int] = []

    async def get_finalized_block_hash(self, block_number: int) -> str:
        self.calls.append(block_number)
        return self._genesis if block_number == 0 else self._selected


class FakeCatalog:
    def __init__(
        self,
        records: dict[
            int, tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]
        ],
    ) -> None:
        self._records = records
        self.calls: list[tuple[str, int]] = []

    async def get_task_version(
        self,
        *,
        commitment: CodingCatalogCommitment,
        catalog_index: int,
    ) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
        self.calls.append((commitment.corpus_release_id, catalog_index))
        return self._records[catalog_index]


class FailingCatalog:
    async def get_task_version(
        self,
        *,
        commitment: CodingCatalogCommitment,
        catalog_index: int,
    ) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
        del commitment, catalog_index
        raise TimeoutError("private catalog timeout")


class FailingBlocks:
    async def get_finalized_block_hash(self, block_number: int) -> str:
        raise TimeoutError(f"block {block_number} timeout")


def _digest(values: dict[str, object], *, label: str) -> str:
    return coding_canonical_sha256(
        values,
        maximum_bytes=_MAX_JSON_BYTES,
        label=label,
    )


def _issue(index: int) -> CodingCatalogIssue:
    return CodingCatalogIssue(
        title=f"Repair synthetic private task {index}",
        description=f"Correct the selected behavior for private task {index}.",
        constraints=["Do not add a runtime dependency."],
    )


def _runtime_policy(index: int) -> CodingCatalogRuntimePolicy:
    return CodingCatalogRuntimePolicy(
        editable_paths=["src/app.py"],
        test_command_ids=[f"visible-tests-{index}"],
        build_command_ids=[f"build-check-{index}"],
    )


def _budgets(index: int) -> CodingCatalogBudgets:
    return CodingCatalogBudgets(
        model_input_tokens=200_000 + index,
        model_output_tokens=30_000,
        workspace_tool_calls=150,
        wall_time_seconds=1_800,
    )


def _task(index: int) -> CodingCatalogTaskVersion:
    byte = f"{index + 1:02x}"
    task = CodingCatalogManifestTask(
        case_id=f"private-case-{index:03d}",
        variant_id="v1",
        profile_capability_id=f"private-profile-{index % 3}",
        visible_bundle_sha256=byte * 32,
        base_tree_sha256=f"{index + 17:02x}" * 32,
        memory_bundle_sha256=f"{index + 33:02x}" * 32,
        environment_image_digest="sha256:" + f"{index + 49:02x}" * 32,
        environment_platform="linux/amd64",
        resource_profile_sha256=f"{index + 65:02x}" * 32,
        grader_bundle_sha256=f"{index + 81:02x}" * 32,
        grader_image_digest="sha256:" + f"{index + 97:02x}" * 32,
        grader_platform="linux/amd64",
        test_manifest_sha256=f"{index + 113:02x}" * 32,
        grader_plan_sha256=f"{index + 129:02x}" * 32,
    )
    issue = _issue(index)
    runtime_policy = _runtime_policy(index)
    budgets = _budgets(index)
    payload = CodingCatalogTaskPayload(
        schema="dittobench-coding-catalog-task-v1",
        coding_contract_version=1,
        weight_eligible=False,
        corpus_release_id="private-coding-corpus-v1",
        catalog_index=index,
        task_version_id=f"private-task-v{index:03d}",
        repository_epoch=f"repository-epoch-{index:03d}",
        issue_sha256=coding_catalog_issue_digest(issue),
        runtime_policy_sha256=coding_catalog_runtime_policy_digest(runtime_policy),
        budgets_sha256=coding_catalog_budgets_digest(budgets),
        task=task,
    )
    return CodingCatalogTaskVersion(
        payload=payload,
        task_commitment_sha256=coding_catalog_task_commitment_digest(payload),
    )


def _catalog(
    count: int = 5,
) -> tuple[
    str,
    dict[int, tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]],
]:
    tasks = [_task(index) for index in range(count)]
    depth = (count - 1).bit_length()
    padded_count = 1 << depth
    layer = [
        coding_catalog_leaf_hash(
            catalog_index=index,
            task_commitment_sha256=task.task_commitment_sha256,
        )
        for index, task in enumerate(tasks)
    ]
    layer.extend(
        coding_catalog_empty_leaf_hash(catalog_index=index)
        for index in range(count, padded_count)
    )
    layers = [layer]
    for level in range(depth):
        previous = layers[-1]
        layers.append(
            [
                coding_catalog_node_hash(
                    level=level,
                    left_sha256=previous[index],
                    right_sha256=previous[index + 1],
                )
                for index in range(0, len(previous), 2)
            ]
        )
    root = layers[-1][0]
    records = {}
    for index, task in enumerate(tasks):
        position = index
        siblings = []
        for level in range(depth):
            siblings.append(layers[level][position ^ 1])
            position //= 2
        values: dict[str, object] = {
            "schema": "dittobench-coding-catalog-membership-proof-v1",
            "coding_contract_version": 1,
            "corpus_release_id": "private-coding-corpus-v1",
            "catalog_merkle_root": root,
            "task_version_count": count,
            "catalog_index": index,
            "task_commitment_sha256": task.task_commitment_sha256,
            "sibling_sha256": siblings,
        }
        values["catalog_membership_proof_sha256"] = _digest(
            values, label="coding catalog membership proof"
        )
        proof = CodingCatalogMembershipProof.model_validate(values)
        records[index] = (task, proof)
    return root, records


def _commitment(*, root: str, count: int = 5) -> CodingCatalogCommitment:
    values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-commitment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "corpus_release_id": "private-coding-corpus-v1",
        "catalog_merkle_root": root,
        "selection_derivation_id": "coding-selection-v1",
        "selection_chain_genesis_hash": "0x" + "22" * 32,
        "grader_contract_sha256": "33" * 32,
        "inference_grant_sha256": "44" * 32,
        "task_version_count": count,
        "curator_hotkey": _CURATOR,
        "committed_at_unix": 1_787_310_000,
    }
    values["commitment_sha256"] = _digest(values, label="catalog commitment")
    return CodingCatalogCommitment.model_validate(values)


def _assignment(commitment: CodingCatalogCommitment) -> CodingSelectionAssignment:
    values: dict[str, object] = {
        "schema": "dittobench-coding-selection-assignment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "bench_version": 12,
        "coding_run_id": "coding-run-private-001",
        "agent_id": str(_AGENT_ID),
        "agent_artifact_sha256": "55" * 32,
        "screened_image_sha256": "66" * 32,
        "corpus_release_id": commitment.corpus_release_id,
        "catalog_commitment_sha256": commitment.commitment_sha256,
        "anchor_block_number": 123_436,
        "anchor_block_hash": "0x" + "70" * 32,
        "selection_delay_blocks": 20,
        "selection_block_number": 123_456,
        "assigned_at": datetime(2026, 8, 21, 0, 0, tzinfo=UTC).isoformat(),
        "task_count": 1,
    }
    return bind_coding_selection_assignment(values)


def _fixture() -> tuple[
    CodingCatalogCommitment,
    CodingSelectionAssignment,
    FakeBlocks,
    FakeCatalog,
]:
    root, records = _catalog()
    commitment = _commitment(root=root)
    assignment = _assignment(commitment)
    blocks = FakeBlocks(genesis="0x" + "22" * 32, selected="0x" + "77" * 32)
    return commitment, assignment, blocks, FakeCatalog(records)


def _selection_vectors() -> dict[str, object]:
    return json.loads(
        (
            Path(__file__).parents[4]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_selection_v1.json"
        ).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("count", [1, 2, 5, 16, 97, 100])
def test_selection_probe_order_is_a_complete_permutation(count: int) -> None:
    indexes = [
        coding_selection_catalog_index(
            selection_seed_sha256="ab" * 32,
            task_version_count=count,
            probe=probe,
        )
        for probe in range(count)
    ]
    assert sorted(indexes) == list(range(count))


def test_block_hash_normalization_accepts_chain_client_forms() -> None:
    assert normalize_coding_block_hash("AB" * 32) == "0x" + "ab" * 32
    assert normalize_coding_block_hash("  0x" + "CD" * 32 + "  ") == ("0x" + "cd" * 32)
    with pytest.raises(ValueError, match="leaf inputs"):
        coding_catalog_leaf_hash(
            catalog_index=1_000_000,
            task_commitment_sha256="11" * 32,
        )
    with pytest.raises(ValueError, match="level"):
        coding_catalog_node_hash(
            level=20,
            left_sha256="11" * 32,
            right_sha256="22" * 32,
        )


async def test_selector_builds_one_reproducible_shared_manifest() -> None:
    commitment, assignment, blocks, catalog = _fixture()

    result = await select_shadow_coding_run(
        assignment=assignment,
        commitment=commitment,
        finalized_block_source=blocks,
        catalog_source=catalog,
        consumed_task_version_ids=frozenset(),
    )

    seed = coding_selection_seed_sha256(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash="0x" + "77" * 32,
    )
    expected_index = coding_selection_catalog_index(
        selection_seed_sha256=seed,
        task_version_count=commitment.task_version_count,
        probe=0,
    )
    assert blocks.calls == [0, assignment.selection_block_number]
    assert catalog.calls == [(commitment.corpus_release_id, expected_index)]
    assert result.selection_proof.catalog_index == expected_index
    assert result.authority.run_manifest_sha256 == (
        coding_selection_run_manifest_digest(result.run_manifest)
    )
    assert result.authority.task_set_manifest_sha256 == (
        result.run_manifest.task_set_manifest_sha256
    )
    assert result.exposure.task_version_id == (
        result.task_set_manifest.tasks[0].task_version_id
    )
    assert result.authority.weight_eligible is False


async def test_shared_selection_vector_replays_exactly() -> None:
    vector = _selection_vectors()
    commitment = CodingCatalogCommitment.model_validate(vector["commitment"])
    assignment = CodingSelectionAssignment.model_validate(vector["assignment"])
    issue = CodingCatalogIssue.model_validate(vector["issue"])
    runtime_policy = CodingCatalogRuntimePolicy.model_validate(vector["runtime_policy"])
    budgets = CodingCatalogBudgets.model_validate(vector["budgets"])
    task = CodingCatalogTaskVersion.model_validate(vector["task_version"])
    proof = CodingCatalogMembershipProof.model_validate(vector["membership_proof"])
    result = await select_shadow_coding_run(
        assignment=assignment,
        commitment=commitment,
        finalized_block_source=FakeBlocks(
            genesis="0x" + "22" * 32,
            selected="0x" + "77" * 32,
        ),
        catalog_source=FakeCatalog({proof.catalog_index: (task, proof)}),
        consumed_task_version_ids=frozenset(),
    )

    assert coding_catalog_issue_digest(issue) == task.payload.issue_sha256
    assert (
        coding_catalog_runtime_policy_digest(runtime_policy)
        == task.payload.runtime_policy_sha256
    )
    assert coding_catalog_budgets_digest(budgets) == task.payload.budgets_sha256
    assert (
        coding_selection_seed_sha256(
            assignment=assignment,
            commitment=commitment,
            selection_block_hash=result.selection_proof.selection_block_hash,
        )
        == vector["selection_seed_sha256"]
    )
    assert verify_coding_selection_proof(
        proof=result.selection_proof,
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=result.selection_proof.selection_block_hash,
        task_version=task,
        membership=proof,
    )
    assert not verify_coding_selection_proof(
        proof=result.selection_proof.model_copy(update={"candidate_probe": 1}),
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=result.selection_proof.selection_block_hash,
        task_version=task,
        membership=proof,
    )
    assert not verify_coding_selection_proof(
        proof=result.selection_proof,
        assignment=assignment,
        commitment=commitment,
        selection_block_hash="0x" + "88" * 32,
        task_version=task,
        membership=proof,
    )
    assert (
        result.selection_proof.model_dump(mode="json", by_alias=True)
        == vector["selection_proof"]
    )
    assert (
        result.task_set_manifest.model_dump(mode="json", by_alias=True)
        == vector["task_set_manifest"]
    )
    assert (
        result.run_manifest.model_dump(mode="json", by_alias=True)
        == vector["run_manifest"]
    )
    assert (
        result.authority.model_dump(mode="json", by_alias=True)
        == vector["run_authority"]
    )
    assert result.exposure.model_dump(mode="json", by_alias=True) == vector["exposure"]


def test_issue_maximum_safe_json_escaping_fits_private_record_budget() -> None:
    issue = CodingCatalogIssue(
        title="\\" * 1024,
        description="\\" * (64 * 1024),
        constraints=["\\" * 4096] * 64,
    )
    body = coding_canonical_json_bytes(
        issue.model_dump(mode="json"),
        maximum_bytes=1 << 20,
        label="coding catalog issue",
    )

    assert len(body) < 700 << 10
    with pytest.raises(ValidationError, match="unsafe controls"):
        CodingCatalogIssue(
            title="Unsafe NUL",
            description="not transportable\x00",
            constraints=[],
        )
    with pytest.raises(ValidationError, match="unsafe controls"):
        CodingCatalogIssue(
            title="Unsafe constraint",
            description="still transportable",
            constraints=["not transportable\x00"],
        )


async def test_consumed_first_probe_advances_without_repeating_an_index() -> None:
    commitment, assignment, blocks, catalog = _fixture()
    seed = coding_selection_seed_sha256(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash="0x" + "77" * 32,
    )
    first_index = coding_selection_catalog_index(
        selection_seed_sha256=seed,
        task_version_count=commitment.task_version_count,
        probe=0,
    )
    second_index = coding_selection_catalog_index(
        selection_seed_sha256=seed,
        task_version_count=commitment.task_version_count,
        probe=1,
    )
    first_task = catalog._records[first_index][0]

    result = await select_shadow_coding_run(
        assignment=assignment,
        commitment=commitment,
        finalized_block_source=blocks,
        catalog_source=catalog,
        consumed_task_version_ids={first_task.payload.task_version_id},
    )

    assert first_index != second_index
    assert [index for _, index in catalog.calls] == [first_index, second_index]
    assert result.selection_proof.catalog_index == second_index
    assert result.selection_proof.candidate_probe == 1


async def test_exhausted_catalog_fails_without_reusing_a_task() -> None:
    commitment, assignment, blocks, catalog = _fixture()
    consumed = {
        task.payload.task_version_id for task, _proof in catalog._records.values()
    }

    with pytest.raises(CodingSelectionExhaustedError):
        await select_shadow_coding_run(
            assignment=assignment,
            commitment=commitment,
            finalized_block_source=blocks,
            catalog_source=catalog,
            consumed_task_version_ids=consumed,
        )

    assert len({index for _, index in catalog.calls}) == commitment.task_version_count


async def test_selector_rejects_wrong_chain_and_malformed_hashes() -> None:
    commitment, assignment, _blocks, catalog = _fixture()
    with pytest.raises(CodingSelectionChainError, match="genesis"):
        await select_shadow_coding_run(
            assignment=assignment,
            commitment=commitment,
            finalized_block_source=FakeBlocks(
                genesis="0x" + "99" * 32,
                selected="0x" + "77" * 32,
            ),
            catalog_source=catalog,
            consumed_task_version_ids=frozenset(),
        )
    with pytest.raises(CodingSelectionChainError, match="malformed"):
        await select_shadow_coding_run(
            assignment=assignment,
            commitment=commitment,
            finalized_block_source=FakeBlocks(
                genesis="0x" + "22" * 32,
                selected="not-a-block",
            ),
            catalog_source=catalog,
            consumed_task_version_ids=frozenset(),
        )


async def test_selector_separates_chain_and_catalog_unavailability() -> None:
    commitment, assignment, blocks, _catalog_source = _fixture()
    with pytest.raises(CodingSelectionChainUnavailableError):
        await select_shadow_coding_run(
            assignment=assignment,
            commitment=commitment,
            finalized_block_source=FailingBlocks(),
            catalog_source=FailingCatalog(),
            consumed_task_version_ids=frozenset(),
        )
    with pytest.raises(CodingSelectionCatalogUnavailableError):
        await select_shadow_coding_run(
            assignment=assignment,
            commitment=commitment,
            finalized_block_source=blocks,
            catalog_source=FailingCatalog(),
            consumed_task_version_ids=frozenset(),
        )


async def test_selector_rejects_assignment_and_membership_drift() -> None:
    commitment, assignment, blocks, catalog = _fixture()
    drifted_assignment = assignment.model_copy(
        update={"selection_block_number": assignment.selection_block_number + 1}
    )
    with pytest.raises(CodingSelectionAuthorityError, match="revalidation"):
        await select_shadow_coding_run(
            assignment=drifted_assignment,
            commitment=commitment,
            finalized_block_source=blocks,
            catalog_source=catalog,
            consumed_task_version_ids=frozenset(),
        )

    seed = coding_selection_seed_sha256(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash="0x" + "77" * 32,
    )
    index = coding_selection_catalog_index(
        selection_seed_sha256=seed,
        task_version_count=commitment.task_version_count,
        probe=0,
    )
    wrong_index = (index + 1) % commitment.task_version_count
    catalog._records[index] = catalog._records[wrong_index]
    with pytest.raises(CodingSelectionCatalogError, match="committed catalog"):
        await select_shadow_coding_run(
            assignment=assignment,
            commitment=commitment,
            finalized_block_source=blocks,
            catalog_source=catalog,
            consumed_task_version_ids=frozenset(),
        )


def test_task_assignment_and_membership_digests_fail_closed() -> None:
    root, records = _catalog()
    commitment = _commitment(root=root)
    assignment = _assignment(commitment)
    task, proof = records[0]
    assert coding_catalog_membership_proof_digest(proof) == (
        proof.catalog_membership_proof_sha256
    )
    assert verify_coding_catalog_membership(proof)
    assert not verify_coding_catalog_membership(
        proof.model_copy(update={"catalog_merkle_root": "ff" * 32})
    )
    assert (
        coding_selection_assignment_digest(assignment) == assignment.assignment_sha256
    )
    equivalent_assignment = assignment.model_dump(mode="json", by_alias=True)
    equivalent_assignment.pop("assignment_sha256")
    equivalent_assignment["assigned_at"] = "2026-08-21T02:00:00+02:00"
    assert (
        bind_coding_selection_assignment(equivalent_assignment).assignment_sha256
        == assignment.assignment_sha256
    )
    invalid_height = equivalent_assignment.copy()
    invalid_height["selection_block_number"] = 123_457
    with pytest.raises(ValidationError, match="anchor plus fixed delay"):
        bind_coding_selection_assignment(invalid_height)
    assert coding_catalog_commitment_digest(commitment) == commitment.commitment_sha256

    changed_task = task.model_dump(mode="json")
    changed_task["payload"]["task"]["case_id"] = "changed-case"
    with pytest.raises(ValidationError, match="task_commitment_sha256"):
        CodingCatalogTaskVersion.model_validate(changed_task)
    changed_proof = proof.model_dump(mode="json", by_alias=True)
    changed_proof["sibling_sha256"][0] = "ff" * 32
    with pytest.raises(ValidationError, match="catalog_membership_proof_sha256"):
        CodingCatalogMembershipProof.model_validate(changed_proof)


def test_platform_run_manifest_matches_shared_python_go_vector() -> None:
    vector = json.loads(
        (
            Path(__file__).parents[4]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_contract_v1.json"
        ).read_text(encoding="utf-8")
    )
    manifest = CodingSelectionRunManifest.model_validate(vector["manifest"])
    assert (
        coding_selection_run_manifest_digest(manifest) == vector["digests"]["manifest"]
    )
