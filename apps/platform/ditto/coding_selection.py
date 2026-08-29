"""Shadow-only private catalog selection for DittoBench Coding."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Set
from dataclasses import dataclass
from typing import Protocol

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    CodingCatalogTaskExposure,
)
from ditto.api_models.coding_evaluation import CodingShadowRunAuthority
from ditto.api_models.coding_selection import (
    CodingCatalogMembershipProof,
    CodingCatalogTaskVersion,
    CodingSelectedTask,
    CodingSelectionAssignment,
    CodingSelectionProof,
    CodingSelectionRunManifest,
    CodingTaskSetManifest,
    coding_selection_run_manifest_digest,
    coding_task_set_manifest_digest,
)

_BLOCK_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_MAX_CANONICAL_JSON_BYTES = 4 << 20
_LEAF_DOMAIN = b"dittobench-coding-catalog-leaf:v1\x00"
_EMPTY_DOMAIN = b"dittobench-coding-catalog-empty:v1\x00"
_NODE_DOMAIN = b"dittobench-coding-catalog-node:v1\x00"
_PERMUTATION_DOMAIN = b"dittobench-coding-selection-permutation:v1\x00"
_MAX_CATALOG_INDEX = 999_999
_MAX_MERKLE_LEVEL = 19


class CodingSelectionError(Exception):
    """Base class for fail-closed shadow selection errors."""


class CodingSelectionAuthorityError(CodingSelectionError):
    """The assignment and registered catalog authority disagree."""


class CodingSelectionChainError(CodingSelectionError):
    """Base class for canonical-chain lookup and integrity errors."""


class CodingSelectionChainUnavailableError(CodingSelectionChainError):
    """The canonical chain source could not resolve the assigned height."""


class CodingSelectionChainIntegrityError(CodingSelectionChainError):
    """The fetched chain identity is malformed or belongs to another chain."""


class CodingSelectionCatalogError(CodingSelectionError):
    """Base class for private-catalog transport and integrity errors."""


class CodingSelectionCatalogUnavailableError(CodingSelectionCatalogError):
    """The private catalog source could not return the selected record."""


class CodingSelectionCatalogIntegrityError(CodingSelectionCatalogError):
    """The private catalog record or membership proof is invalid."""


class CodingSelectionExhaustedError(CodingSelectionError):
    """Every task version in the committed catalog was already consumed."""


class CodingFinalizedBlockSource(Protocol):
    async def get_finalized_block_hash(self, block_number: int) -> str:
        """Return the finalized canonical chain hash at one exact height."""


class CodingPrivateCatalogSource(Protocol):
    async def get_task_version(
        self,
        *,
        corpus_release_id: str,
        catalog_index: int,
    ) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
        """Return one digest-only private task record and its Merkle proof."""


@dataclass(frozen=True)
class CodingSelectionResult:
    assignment: CodingSelectionAssignment
    selection_proof: CodingSelectionProof
    task_set_manifest: CodingTaskSetManifest
    run_manifest: CodingSelectionRunManifest
    authority: CodingShadowRunAuthority
    exposure: CodingCatalogTaskExposure


def normalize_coding_block_hash(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized.startswith("0x"):
        normalized = f"0x{normalized}"
    if _BLOCK_HASH.fullmatch(normalized) is None:
        raise CodingSelectionChainIntegrityError(
            "chain returned a malformed block hash"
        )
    return normalized


def coding_catalog_leaf_hash(*, catalog_index: int, task_commitment_sha256: str) -> str:
    if (
        catalog_index < 0
        or catalog_index > _MAX_CATALOG_INDEX
        or len(task_commitment_sha256) != 64
    ):
        raise ValueError("catalog leaf inputs are invalid")
    try:
        commitment = bytes.fromhex(task_commitment_sha256)
    except ValueError as error:
        raise ValueError("catalog task commitment is not hexadecimal") from error
    return hashlib.sha256(
        _LEAF_DOMAIN + catalog_index.to_bytes(8, "big") + commitment
    ).hexdigest()


def coding_catalog_empty_leaf_hash(*, catalog_index: int) -> str:
    if catalog_index < 0 or catalog_index > _MAX_CATALOG_INDEX:
        raise ValueError("catalog empty-leaf index is invalid")
    return hashlib.sha256(_EMPTY_DOMAIN + catalog_index.to_bytes(8, "big")).hexdigest()


def coding_catalog_node_hash(
    *,
    level: int,
    left_sha256: str,
    right_sha256: str,
) -> str:
    if level < 0 or level > _MAX_MERKLE_LEVEL:
        raise ValueError("catalog Merkle level is invalid")
    try:
        left = bytes.fromhex(left_sha256)
        right = bytes.fromhex(right_sha256)
    except ValueError as error:
        raise ValueError("catalog Merkle node is not hexadecimal") from error
    if len(left) != 32 or len(right) != 32:
        raise ValueError("catalog Merkle node must contain SHA-256 children")
    return hashlib.sha256(
        _NODE_DOMAIN + level.to_bytes(4, "big") + left + right
    ).hexdigest()


def verify_coding_catalog_membership(proof: CodingCatalogMembershipProof) -> bool:
    try:
        proof = CodingCatalogMembershipProof.model_validate_json(
            proof.model_dump_json(by_alias=True)
        )
    except ValueError:
        return False
    node = coding_catalog_leaf_hash(
        catalog_index=proof.catalog_index,
        task_commitment_sha256=proof.task_commitment_sha256,
    )
    for level, sibling in enumerate(proof.sibling_sha256):
        if (proof.catalog_index >> level) & 1:
            node = coding_catalog_node_hash(
                level=level,
                left_sha256=sibling,
                right_sha256=node,
            )
        else:
            node = coding_catalog_node_hash(
                level=level,
                left_sha256=node,
                right_sha256=sibling,
            )
    return node == proof.catalog_merkle_root


def verify_coding_selection_proof(
    *,
    proof: CodingSelectionProof,
    assignment: CodingSelectionAssignment,
    commitment: CodingCatalogCommitment,
    selection_block_hash: str,
    task_version: CodingCatalogTaskVersion,
    membership: CodingCatalogMembershipProof,
) -> bool:
    """Recompute one selected probe against its complete public authority."""

    try:
        proof = CodingSelectionProof.model_validate_json(
            proof.model_dump_json(by_alias=True)
        )
        assignment = CodingSelectionAssignment.model_validate_json(
            assignment.model_dump_json(by_alias=True)
        )
        commitment = CodingCatalogCommitment.model_validate_json(
            commitment.model_dump_json(by_alias=True)
        )
        task_version = CodingCatalogTaskVersion.model_validate_json(
            task_version.model_dump_json(by_alias=True)
        )
        membership = CodingCatalogMembershipProof.model_validate_json(
            membership.model_dump_json(by_alias=True)
        )
        _validate_authority(assignment=assignment, commitment=commitment)
        selection_block_hash = normalize_coding_block_hash(selection_block_hash)
        seed_sha256 = coding_selection_seed_sha256(
            assignment=assignment,
            commitment=commitment,
            selection_block_hash=selection_block_hash,
        )
        expected_index = coding_selection_catalog_index(
            selection_seed_sha256=seed_sha256,
            task_version_count=commitment.task_version_count,
            probe=proof.candidate_probe,
        )
    except (ValueError, CodingSelectionError):
        return False
    return (
        proof.assignment_sha256 == assignment.assignment_sha256
        and proof.selection_block_hash == selection_block_hash
        and task_version.payload.corpus_release_id == commitment.corpus_release_id
        and membership.corpus_release_id == commitment.corpus_release_id
        and membership.catalog_merkle_root == commitment.catalog_merkle_root
        and membership.task_version_count == commitment.task_version_count
        and proof.catalog_index == expected_index
        and proof.catalog_index == task_version.payload.catalog_index
        and proof.catalog_index == membership.catalog_index
        and proof.task_version_id == task_version.payload.task_version_id
        and proof.task_commitment_sha256 == task_version.task_commitment_sha256
        and proof.task_commitment_sha256 == membership.task_commitment_sha256
        and proof.catalog_membership_proof_sha256
        == membership.catalog_membership_proof_sha256
        and verify_coding_catalog_membership(membership)
    )


def coding_selection_seed_sha256(
    *,
    assignment: CodingSelectionAssignment,
    commitment: CodingCatalogCommitment,
    selection_block_hash: str,
) -> str:
    projection = {
        "agent_artifact_sha256": assignment.agent_artifact_sha256,
        "assignment_sha256": assignment.assignment_sha256,
        "catalog_merkle_root": commitment.catalog_merkle_root,
        "coding_run_id": assignment.coding_run_id,
        "corpus_release_id": commitment.corpus_release_id,
        "schema": "dittobench-coding-selection-seed-v1",
        "selection_block_hash": selection_block_hash,
        "selection_block_number": assignment.selection_block_number,
        "selection_derivation_id": commitment.selection_derivation_id,
    }
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding selection seed",
    )


def coding_selection_catalog_index(
    *,
    selection_seed_sha256: str,
    task_version_count: int,
    probe: int,
) -> int:
    """Return one unique affine-permutation index for ``probe``."""

    if task_version_count < 1 or task_version_count > 1_000_000:
        raise ValueError("task_version_count is outside contract bounds")
    if probe < 0 or probe >= task_version_count:
        raise ValueError("selection probe is outside catalog bounds")
    if task_version_count == 1:
        return 0
    try:
        seed = bytes.fromhex(selection_seed_sha256)
    except ValueError as error:
        raise ValueError("selection seed is not hexadecimal") from error
    if len(seed) != 32:
        raise ValueError("selection seed must be SHA-256")
    start = _sample_modulo(seed=seed, label=b"start", modulus=task_version_count)
    step = _coprime_step(seed=seed, modulus=task_version_count)
    return (start + probe * step) % task_version_count


async def select_shadow_coding_run(
    *,
    assignment: CodingSelectionAssignment,
    commitment: CodingCatalogCommitment,
    finalized_block_source: CodingFinalizedBlockSource,
    catalog_source: CodingPrivateCatalogSource,
    consumed_task_version_ids: Set[str],
) -> CodingSelectionResult:
    """Select one private task after independently resolving chain authority."""

    try:
        assignment = CodingSelectionAssignment.model_validate_json(
            assignment.model_dump_json(by_alias=True)
        )
        commitment = CodingCatalogCommitment.model_validate_json(
            commitment.model_dump_json(by_alias=True)
        )
    except ValueError as error:
        raise CodingSelectionAuthorityError(
            "selection authority failed known-field revalidation"
        ) from error
    consumed_task_version_ids = frozenset(consumed_task_version_ids)
    _validate_authority(assignment=assignment, commitment=commitment)
    try:
        genesis_hash = normalize_coding_block_hash(
            await finalized_block_source.get_finalized_block_hash(0)
        )
        selection_block_hash = normalize_coding_block_hash(
            await finalized_block_source.get_finalized_block_hash(
                assignment.selection_block_number
            )
        )
    except CodingSelectionChainError:
        raise
    except Exception as error:
        raise CodingSelectionChainUnavailableError(
            "finalized canonical selection block lookup failed"
        ) from error
    if genesis_hash != commitment.selection_chain_genesis_hash:
        raise CodingSelectionChainIntegrityError(
            "selection chain genesis does not match catalog commitment"
        )

    seed_sha256 = coding_selection_seed_sha256(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=selection_block_hash,
    )
    for probe in range(commitment.task_version_count):
        catalog_index = coding_selection_catalog_index(
            selection_seed_sha256=seed_sha256,
            task_version_count=commitment.task_version_count,
            probe=probe,
        )
        try:
            task_version, membership = await catalog_source.get_task_version(
                corpus_release_id=commitment.corpus_release_id,
                catalog_index=catalog_index,
            )
        except CodingSelectionCatalogError:
            raise
        except Exception as error:
            raise CodingSelectionCatalogUnavailableError(
                "private catalog lookup failed"
            ) from error
        task_version, membership = _validate_catalog_record(
            commitment=commitment,
            catalog_index=catalog_index,
            task_version=task_version,
            membership=membership,
        )
        if task_version.payload.task_version_id in consumed_task_version_ids:
            continue
        return _build_selection_result(
            assignment=assignment,
            commitment=commitment,
            selection_block_hash=selection_block_hash,
            probe=probe,
            task_version=task_version,
            membership=membership,
        )
    raise CodingSelectionExhaustedError(
        "private coding catalog has no unconsumed task version"
    )


def _validate_authority(
    *,
    assignment: CodingSelectionAssignment,
    commitment: CodingCatalogCommitment,
) -> None:
    if (
        assignment.coding_contract_version != commitment.coding_contract_version
        or assignment.corpus_release_id != commitment.corpus_release_id
        or assignment.catalog_commitment_sha256 != commitment.commitment_sha256
        or commitment.selection_derivation_id != "coding-selection-v1"
        or assignment.weight_eligible
        or commitment.weight_eligible
    ):
        raise CodingSelectionAuthorityError(
            "selection assignment does not match catalog commitment"
        )


def _validate_catalog_record(
    *,
    commitment: CodingCatalogCommitment,
    catalog_index: int,
    task_version: CodingCatalogTaskVersion,
    membership: CodingCatalogMembershipProof,
) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
    try:
        task_version = CodingCatalogTaskVersion.model_validate_json(
            task_version.model_dump_json(by_alias=True)
        )
        membership = CodingCatalogMembershipProof.model_validate_json(
            membership.model_dump_json(by_alias=True)
        )
    except ValueError as error:
        raise CodingSelectionCatalogIntegrityError(
            "private task record failed known-field revalidation"
        ) from error
    payload = task_version.payload
    if (
        payload.corpus_release_id != commitment.corpus_release_id
        or payload.catalog_index != catalog_index
        or payload.coding_contract_version != commitment.coding_contract_version
        or payload.weight_eligible
        or membership.corpus_release_id != commitment.corpus_release_id
        or membership.catalog_merkle_root != commitment.catalog_merkle_root
        or membership.task_version_count != commitment.task_version_count
        or membership.catalog_index != catalog_index
        or membership.task_commitment_sha256 != task_version.task_commitment_sha256
        or not verify_coding_catalog_membership(membership)
    ):
        raise CodingSelectionCatalogIntegrityError(
            "private task record does not match committed catalog membership"
        )
    return task_version, membership


def _build_selection_result(
    *,
    assignment: CodingSelectionAssignment,
    commitment: CodingCatalogCommitment,
    selection_block_hash: str,
    probe: int,
    task_version: CodingCatalogTaskVersion,
    membership: CodingCatalogMembershipProof,
) -> CodingSelectionResult:
    selection_values = {
        "schema": "dittobench-coding-selection-proof-v1",
        "coding_contract_version": 1,
        "assignment_sha256": assignment.assignment_sha256,
        "selection_block_hash": selection_block_hash,
        "candidate_probe": probe,
        "catalog_index": task_version.payload.catalog_index,
        "task_version_id": task_version.payload.task_version_id,
        "task_commitment_sha256": task_version.task_commitment_sha256,
        "catalog_membership_proof_sha256": (membership.catalog_membership_proof_sha256),
    }
    selection_values["selection_proof_sha256"] = coding_canonical_sha256(
        selection_values,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding selection proof",
    )
    selection_proof = CodingSelectionProof.model_validate(selection_values)
    if not verify_coding_selection_proof(
        proof=selection_proof,
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=selection_block_hash,
        task_version=task_version,
        membership=membership,
    ):  # pragma: no cover - construction and model validation prove this
        raise CodingSelectionCatalogIntegrityError(
            "selection proof is not reproducible"
        )

    selected_task = CodingSelectedTask(
        manifest_index=0,
        task_version_id=task_version.payload.task_version_id,
        task_commitment_sha256=task_version.task_commitment_sha256,
        selection_proof_sha256=selection_proof.selection_proof_sha256,
        catalog_membership_proof_sha256=(membership.catalog_membership_proof_sha256),
        repository_epoch=task_version.payload.repository_epoch,
        issue_sha256=task_version.payload.issue_sha256,
        runtime_policy_sha256=task_version.payload.runtime_policy_sha256,
        budgets_sha256=task_version.payload.budgets_sha256,
        task=task_version.payload.task,
    )
    task_set = CodingTaskSetManifest(
        schema="dittobench-coding-task-set-v1",
        coding_contract_version=1,
        weight_eligible=False,
        coding_run_id=assignment.coding_run_id,
        assignment_sha256=assignment.assignment_sha256,
        selection_block_number=assignment.selection_block_number,
        selection_block_hash=selection_block_hash,
        tasks=[selected_task],
    )
    task_set_sha256 = coding_task_set_manifest_digest(task_set)
    task_set_id = f"coding-task-set-v1-{task_set_sha256}"
    run_manifest = CodingSelectionRunManifest(
        schema="dittobench-coding-run-manifest-v1",
        coding_contract_version=1,
        bench_family="coding",
        weight_eligible=False,
        coding_run_id=assignment.coding_run_id,
        agent_id=str(assignment.agent_id),
        agent_artifact_sha256=assignment.agent_artifact_sha256,
        corpus_release_id=commitment.corpus_release_id,
        catalog_merkle_root=commitment.catalog_merkle_root,
        selection_derivation_id=commitment.selection_derivation_id,
        selection_chain_genesis_hash=commitment.selection_chain_genesis_hash,
        selection_block_number=assignment.selection_block_number,
        selection_block_hash=selection_block_hash,
        inference_grant_sha256=commitment.inference_grant_sha256,
        grader_contract_sha256=commitment.grader_contract_sha256,
        task_set_id=task_set_id,
        task_set_manifest_sha256=task_set_sha256,
        tasks=[task_version.payload.task],
    )
    run_manifest_sha256 = coding_selection_run_manifest_digest(run_manifest)
    authority = CodingShadowRunAuthority(
        schema="dittobench-coding-shadow-run-authority-v1",
        bench_family="coding",
        coding_contract_version=1,
        weight_eligible=False,
        bench_version=assignment.bench_version,
        coding_run_id=assignment.coding_run_id,
        agent_id=assignment.agent_id,
        agent_artifact_sha256=assignment.agent_artifact_sha256,
        screened_image_sha256=assignment.screened_image_sha256,
        corpus_release_id=commitment.corpus_release_id,
        catalog_merkle_root=commitment.catalog_merkle_root,
        selection_derivation_id=commitment.selection_derivation_id,
        selection_chain_genesis_hash=commitment.selection_chain_genesis_hash,
        selection_block_number=assignment.selection_block_number,
        selection_block_hash=selection_block_hash,
        inference_grant_sha256=commitment.inference_grant_sha256,
        grader_contract_sha256=commitment.grader_contract_sha256,
        task_set_id=task_set_id,
        task_set_manifest_sha256=task_set_sha256,
        run_manifest_sha256=run_manifest_sha256,
        task_count=1,
    )
    task = task_version.payload.task
    exposure = CodingCatalogTaskExposure(
        manifest_index=0,
        task_version_id=task_version.payload.task_version_id,
        task_commitment_sha256=task_version.task_commitment_sha256,
        selection_proof_sha256=selection_proof.selection_proof_sha256,
        catalog_membership_proof_sha256=(membership.catalog_membership_proof_sha256),
        visible_bundle_sha256=task.visible_bundle_sha256,
        base_tree_sha256=task.base_tree_sha256,
        memory_bundle_sha256=task.memory_bundle_sha256,
        environment_image_digest=task.environment_image_digest,
        resource_profile_sha256=task.resource_profile_sha256,
        grader_bundle_sha256=task.grader_bundle_sha256,
        grader_image_digest=task.grader_image_digest,
        test_manifest_sha256=task.test_manifest_sha256,
        grader_plan_sha256=task.grader_plan_sha256,
    )
    return CodingSelectionResult(
        assignment=assignment,
        selection_proof=selection_proof,
        task_set_manifest=task_set,
        run_manifest=run_manifest,
        authority=authority,
        exposure=exposure,
    )


def _sample_modulo(*, seed: bytes, label: bytes, modulus: int) -> int:
    if modulus < 1:
        raise ValueError("selection modulus must be positive")
    limit = (1 << 256) - ((1 << 256) % modulus)
    for counter in range(1024):
        digest = hashlib.sha256(
            _PERMUTATION_DOMAIN + seed + label + counter.to_bytes(4, "big")
        ).digest()
        value = int.from_bytes(digest, "big")
        if value < limit:
            return value % modulus
    raise RuntimeError("selection rejection sampler did not converge")


def _coprime_step(*, seed: bytes, modulus: int) -> int:
    if modulus == 1:
        return 0
    for attempt in range(1024):
        step = (
            _sample_modulo(
                seed=seed,
                label=b"step" + attempt.to_bytes(4, "big"),
                modulus=modulus - 1,
            )
            + 1
        )
        if math.gcd(step, modulus) == 1:
            return step
    raise RuntimeError("selection could not derive a coprime step")
